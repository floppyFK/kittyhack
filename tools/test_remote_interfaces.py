#!/usr/bin/env python3
"""Exercise a live Kittyhack REST API and MQTT interface.

This is a development smoke/integration test, not a pytest test.  By default it
uses ``.local/remote-interfaces.json``; create that gitignored file with:

    .venv/bin/python tools/test_remote_interfaces.py --init

Normal runs test REST and MQTT and temporarily change entry/exit modes and one
cat setting.  Every changed value is restored in a ``finally`` block.  Door
movement is never tested unless ``--with-door`` is supplied.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CREDENTIALS = ROOT / ".local" / "remote-interfaces.json"
ENTRY_VALUES = ("all", "all_rfids", "known", "none", "configure_per_cat")
EXIT_VALUES = ("allow", "deny", "configure_per_cat")
ENTRY_LABELS = {
    "all": {"all", "All cats", "Alle Katzen"},
    "all_rfids": {"all_rfids", "All cats with RFID", "Alle Katzen mit RFID-Chip"},
    "known": {"known", "Only registered cats", "Nur registrierte Katzen"},
    "none": {"none", "No cats", "Keine Katzen"},
    "configure_per_cat": {
        "configure_per_cat",
        "Separate configuration per cat",
        "Separate Konfiguration pro Katze",
    },
}
EXIT_LABELS = {
    "allow": {"allow", "Allow exit", "Ausgang erlauben"},
    "deny": {"deny", "Do not allow exit", "Ausgang verbieten"},
    "configure_per_cat": {
        "configure_per_cat",
        "Per-cat configuration",
        "Separate Konfiguration pro Katze",
    },
}


class TestError(Exception):
    """Expected configuration, transport, or assertion failure."""


@dataclass
class HttpResponse:
    status: int
    body: Any


@dataclass
class Check:
    state: str
    name: str
    detail: str = ""


class Results:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def pass_(self, name: str, detail: str = "") -> None:
        self.checks.append(Check("PASS", name, detail))
        print(f"PASS  {name}" + (f" — {detail}" if detail else ""))

    def fail(self, name: str, detail: str) -> None:
        self.checks.append(Check("FAIL", name, detail))
        print(f"FAIL  {name} — {detail}", file=sys.stderr)

    def skip(self, name: str, detail: str) -> None:
        self.checks.append(Check("SKIP", name, detail))
        print(f"SKIP  {name} — {detail}")

    def run(self, name: str, check: Callable[[], str | None]) -> bool:
        try:
            detail = check()
        except Exception as exc:
            self.fail(name, str(exc))
            return False
        self.pass_(name, detail or "")
        return True

    def summary(self) -> int:
        counts = {
            state: sum(item.state == state for item in self.checks)
            for state in ("PASS", "FAIL", "SKIP")
        }
        print(
            f"\nSummary: {counts['PASS']} passed, "
            f"{counts['FAIL']} failed, {counts['SKIP']} skipped"
        )
        return 1 if counts["FAIL"] else 0


class ApiClient:
    def __init__(self, base_url: str, token: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        auth: str = "bearer",
        token: str | None = None,
    ) -> HttpResponse:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        selected_token = self.token if token is None else token
        if auth == "bearer":
            headers["Authorization"] = f"Bearer {selected_token}"
        elif auth == "x-api-key":
            headers["X-API-Key"] = selected_token
        elif auth == "query":
            separator = "&" if "?" in url else "?"
            url += separator + urllib.parse.urlencode({"token": selected_token})
        elif auth != "none":
            raise TestError(f"unknown authentication method: {auth}")

        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TestError(f"{method} {path}: {exc}") from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TestError(f"{method} {path}: response is not JSON (HTTP {status})") from exc
        return HttpResponse(status, parsed)

    def ok(
        self, method: str, path: str, *, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = self.request(method, path, body=body)
        if response.status != 200:
            raise TestError(f"{method} {path}: expected HTTP 200, got {response.status}: {response.body}")
        if not isinstance(response.body, dict) or response.body.get("ok") is not True:
            raise TestError(f"{method} {path}: missing ok=true: {response.body}")
        return response.body

    def mode(self) -> dict[str, str]:
        mode = self.ok("GET", "/api/v1/mode").get("mode")
        if not isinstance(mode, dict) or mode.get("entry") not in ENTRY_VALUES or mode.get("exit") not in EXIT_VALUES:
            raise TestError(f"invalid mode response: {mode}")
        return {"entry": str(mode["entry"]), "exit": str(mode["exit"])}

    def set_mode(self, entry: str, exit_: str) -> dict[str, str]:
        result = self.ok("PUT", "/api/v1/mode", body={"entry": entry, "exit": exit_})
        mode = result.get("mode")
        if not isinstance(mode, dict):
            raise TestError(f"invalid mode update response: {result}")
        return {"entry": str(mode.get("entry")), "exit": str(mode.get("exit"))}


class MqttProbe:
    def __init__(self, config: dict[str, Any], timeout: float) -> None:
        try:
            from paho.mqtt import client as mqtt
        except ImportError as exc:
            raise TestError(
                "paho-mqtt is not installed; run this script with the Kittyhack venv"
            ) from exc

        self.mqtt = mqtt
        self.config = config
        self.timeout = timeout
        self.device_id = str(config["device_id"])
        self.prefix = f"kittyhack/{self.device_id}"
        self.messages: dict[str, str] = {}
        self._versions: dict[str, int] = {}
        self._condition = threading.Condition()
        self._connected = threading.Event()
        self._connect_error: str | None = None
        client_id = f"kittyhack-interface-test-{secrets.token_hex(4)}"
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=client_id,
        )
        username = config.get("username")
        if username:
            self.client.username_pw_set(str(username), str(config.get("password") or ""))
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc != 0:
            self._connect_error = f"broker rejected connection (rc={rc})"
            self._connected.set()
            return
        client.subscribe(f"{self.prefix}/#")
        self._connected.set()

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            payload = "<binary>"
        with self._condition:
            self.messages[message.topic] = payload
            self._versions[message.topic] = self._versions.get(message.topic, 0) + 1
            self._condition.notify_all()

    def connect(self) -> None:
        try:
            self.client.connect(
                str(self.config["host"]),
                int(self.config.get("port", 1883)),
                keepalive=30,
            )
            self.client.loop_start()
        except Exception as exc:
            raise TestError(f"MQTT connect failed: {exc}") from exc
        if not self._connected.wait(self.timeout):
            self.close()
            raise TestError("timed out waiting for MQTT connection")
        if self._connect_error:
            self.close()
            raise TestError(self._connect_error)

    def close(self) -> None:
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()

    def topic(self, suffix: str) -> str:
        return f"{self.prefix}/{suffix}"

    def version(self, suffix: str) -> int:
        with self._condition:
            return self._versions.get(self.topic(suffix), 0)

    def wait_for(
        self,
        suffix: str,
        predicate: Callable[[str], bool],
        *,
        after_version: int | None = None,
    ) -> str:
        topic = self.topic(suffix)
        deadline = time.monotonic() + self.timeout
        with self._condition:
            while True:
                payload = self.messages.get(topic)
                version = self._versions.get(topic, 0)
                new_enough = after_version is None or version > after_version
                if payload is not None and new_enough and predicate(payload):
                    return payload
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    current = repr(payload) if payload is not None else "no message"
                    raise TestError(f"timed out waiting for {topic} (last: {current})")
                self._condition.wait(remaining)

    def publish(self, suffix: str, payload: str) -> None:
        info = self.client.publish(self.topic(suffix), payload)
        if info.rc != self.mqtt.MQTT_ERR_SUCCESS:
            raise TestError(f"publish to {self.topic(suffix)} failed (rc={info.rc})")
        info.wait_for_publish(timeout=self.timeout)
        if not info.is_published():
            raise TestError(f"publish to {self.topic(suffix)} timed out")


def require_status(response: HttpResponse, expected: int, label: str) -> str:
    if response.status != expected:
        raise TestError(f"{label}: expected HTTP {expected}, got {response.status}: {response.body}")
    return f"HTTP {expected}"


def different(current: str, values: tuple[str, ...]) -> str:
    return next(value for value in values if value != current)


def validate_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise TestError("credential file must contain a JSON object")
    base_url = config.get("base_url")
    token = config.get("api_token")
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        raise TestError("base_url must start with http:// or https://")
    if not isinstance(token, str) or not token:
        raise TestError("api_token must be a non-empty string")
    mqtt_config = config.get("mqtt")
    if not isinstance(mqtt_config, dict) or not isinstance(mqtt_config.get("enabled"), bool):
        raise TestError("mqtt.enabled must be true or false")
    if mqtt_config["enabled"]:
        if not mqtt_config.get("host") or not mqtt_config.get("device_id"):
            raise TestError("mqtt.host and mqtt.device_id are required when MQTT is enabled")
        try:
            port = int(mqtt_config.get("port", 1883))
        except (TypeError, ValueError) as exc:
            raise TestError("mqtt.port must be an integer") from exc
        if not 1 <= port <= 65535:
            raise TestError("mqtt.port must be between 1 and 65535")
        mqtt_config["port"] = port
    return config


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return validate_config(json.load(handle))
    except FileNotFoundError as exc:
        raise TestError(f"credential file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TestError(f"invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise TestError(f"cannot read {path}: {exc}") from exc


def prompt(text: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    reader = getpass.getpass if secret else input
    value = reader(f"{text}{suffix}: ").strip()
    return value if value else default


def prompt_bool(text: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{text} [{hint}]: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please enter y or n.")


def initialise(args: argparse.Namespace, path: Path) -> dict[str, Any]:
    if not sys.stdin.isatty():
        raise TestError("--init requires an interactive terminal")
    old: dict[str, Any] = {}
    if path.exists():
        try:
            old = load_config(path)
        except TestError:
            old = {}
    old_mqtt = old.get("mqtt") if isinstance(old.get("mqtt"), dict) else {}
    print(f"Creating Kittyhack interface credentials at {path}")
    base_url = prompt("Kittyhack base URL", args.base_url or old.get("base_url", "http://127.0.0.1:8080"))
    api_token = args.api_token or prompt(
        "REST API token (input hidden)",
        str(old.get("api_token", "")),
        secret=True,
    )
    mqtt_enabled = prompt_bool("Test MQTT", bool(old_mqtt.get("enabled", True)))
    mqtt_config: dict[str, Any] = {"enabled": mqtt_enabled}
    if mqtt_enabled:
        mqtt_config.update(
            {
                "host": prompt("MQTT broker host", args.mqtt_host or str(old_mqtt.get("host", ""))),
                "port": int(prompt("MQTT broker port", str(args.mqtt_port or old_mqtt.get("port", 1883)))),
                "username": prompt("MQTT username (empty for anonymous)", str(old_mqtt.get("username", ""))),
                "password": prompt(
                    "MQTT password (input hidden; empty for anonymous)",
                    str(old_mqtt.get("password", "")),
                    secret=True,
                ),
                "device_id": prompt("Kittyhack MQTT device id", args.device_id or str(old_mqtt.get("device_id", ""))),
            }
        )
    config = validate_config({"base_url": base_url, "api_token": api_token, "mqtt": mqtt_config})
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise TestError(f"cannot write {path}: {exc}") from exc
    print(f"Saved {path} with permissions 0600.")
    return config


def check_api_reads(api: ApiClient, results: Results) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
    results.run(
        "REST rejects missing token",
        lambda: require_status(api.request("GET", "/api/v1/status", auth="none"), 401, "missing token"),
    )
    results.run(
        "REST rejects invalid token",
        lambda: require_status(
            api.request("GET", "/api/v1/status", token=f"invalid-{secrets.token_hex(8)}"),
            401,
            "invalid token",
        ),
    )
    for auth, label in (
        ("bearer", "Bearer token"),
        ("x-api-key", "X-API-Key"),
        ("query", "query token"),
    ):
        results.run(
            f"REST authenticates with {label}",
            lambda auth=auth: require_status(
                api.request("GET", "/api/v1/status", auth=auth), 200, auth
            ),
        )

    mode: dict[str, str] | None = None
    cats: list[dict[str, Any]] = []

    def status_check() -> str:
        body = api.ok("GET", "/api/v1/status")
        if not isinstance(body.get("door"), dict) or not isinstance(body.get("mode"), dict):
            raise TestError(f"missing door or mode object: {body}")
        return f"door available={body['door'].get('available')}"

    results.run("REST reads status", status_check)

    def mode_check() -> str:
        nonlocal mode
        mode = api.mode()
        return f"entry={mode['entry']}, exit={mode['exit']}"

    results.run("REST reads mode", mode_check)

    def cats_check() -> str:
        nonlocal cats
        body = api.ok("GET", "/api/v1/cats")
        if not isinstance(body.get("cats"), list) or body.get("count") != len(body["cats"]):
            raise TestError(f"invalid cats response: {body}")
        cats = body["cats"]
        return f"{len(cats)} cat(s)"

    results.run("REST reads cats", cats_check)

    def events_check() -> str:
        body = api.ok("GET", "/api/v1/events?limit=1")
        if not isinstance(body.get("events"), list) or len(body["events"]) > 1:
            raise TestError(f"invalid events response: {body}")
        return f"{len(body['events'])} event(s)"

    results.run("REST reads events", events_check)
    results.run(
        "REST rejects invalid event limit",
        lambda: require_status(
            api.request("GET", "/api/v1/events?limit=invalid"), 400, "invalid limit"
        ),
    )

    def invalid_mode_check() -> str:
        before = api.mode()
        response = api.request("GET", "/api/v1/mode/entry/not-a-mode")
        require_status(response, 400, "invalid mode")
        if api.mode() != before:
            raise TestError("mode changed after rejected value")
        return "HTTP 400, mode unchanged"

    results.run("REST rejects invalid mode", invalid_mode_check)
    return mode, cats


def check_api_mutations(
    api: ApiClient,
    results: Results,
    mode: dict[str, str] | None,
    cats: list[dict[str, Any]],
) -> None:
    if mode is None:
        results.skip("REST mode roundtrip", "mode read failed")
    else:
        original = dict(mode)

        def mode_roundtrip() -> str:
            changed = {
                "entry": different(original["entry"], ENTRY_VALUES),
                "exit": different(original["exit"], EXIT_VALUES),
            }
            try:
                if api.set_mode(changed["entry"], changed["exit"]) != changed:
                    raise TestError("PUT response did not contain requested mode")
                if api.mode() != changed:
                    raise TestError("GET did not confirm PUT mode")
                path_value = different(changed["entry"], ENTRY_VALUES)
                path_result = api.ok("POST", f"/api/v1/mode/entry/{path_value}")
                if path_result.get("mode", {}).get("entry") != path_value:
                    raise TestError("path endpoint did not change entry mode")
            finally:
                api.set_mode(original["entry"], original["exit"])
            if api.mode() != original:
                raise TestError("original mode was not restored")
            return "PUT and path endpoint; original restored"

        results.run("REST mode roundtrip", mode_roundtrip)

    if not cats:
        results.skip("REST cat setting roundtrip", "no configured cats")
        return
    cat = cats[0]
    ident = cat.get("rfid") or cat.get("name")
    field = next((key for key in ("allow_entry", "allow_exit", "enable_prey_detection") if key in cat), None)
    if not ident or field is None:
        results.skip("REST cat setting roundtrip", "first cat lacks identifier or mutable flags")
        return

    def cat_roundtrip() -> str:
        original = bool(cat[field])
        encoded_ident = urllib.parse.quote(str(ident), safe="")
        try:
            api.ok("PUT", f"/api/v1/cats/{encoded_ident}", body={field: not original})
            updated = api.ok("GET", "/api/v1/cats").get("cats", [])
            match = next(
                (
                    item
                    for item in updated
                    if str(item.get("rfid")) == str(cat.get("rfid"))
                    or (
                        cat.get("name")
                        and str(item.get("name", "")).lower() == str(cat["name"]).lower()
                    )
                ),
                None,
            )
            if match is None or bool(match.get(field)) is original:
                raise TestError(f"{field} change was not visible")
        finally:
            api.ok("PUT", f"/api/v1/cats/{encoded_ident}", body={field: original})
        return f"{ident}: {field}; original restored"

    results.run("REST cat setting roundtrip", cat_roundtrip)


def check_mqtt_state(probe: MqttProbe, results: Results) -> None:
    checks: tuple[tuple[str, set[str]], ...] = (
        ("status", {"online"}),
        ("locks/inside", {"locked", "unlocked"}),
        ("locks/outside", {"locked", "unlocked"}),
        ("motion/outside", {"detected", "not_detected"}),
        ("motion/inside", {"detected", "not_detected"}),
        ("prey/detected", {"detected", "not_detected"}),
        ("config/allowed_to_enter", set().union(*ENTRY_LABELS.values())),
        ("config/allowed_to_exit", set().union(*EXIT_LABELS.values())),
    )
    for suffix, allowed in checks:
        results.run(
            f"MQTT state {suffix}",
            lambda suffix=suffix, allowed=allowed: probe.wait_for(
                suffix, lambda payload: payload in allowed
            ),
        )
    camera_topic = probe.topic("camera/image")
    if camera_topic in probe.messages:
        payload = probe.messages[camera_topic]
        if payload and payload != "<binary>":
            results.pass_("MQTT optional camera image", f"{len(payload)} encoded bytes")
        else:
            results.fail("MQTT optional camera image", "empty or non-UTF-8 payload")
    else:
        results.skip("MQTT optional camera image", "no retained frame")


def check_cross_interface(api: ApiClient, probe: MqttProbe, results: Results) -> None:
    def api_to_mqtt() -> str:
        original = api.mode()
        changed_entry = different(original["entry"], ENTRY_VALUES)
        version = probe.version("config/allowed_to_enter")
        try:
            api.set_mode(changed_entry, original["exit"])
            probe.wait_for(
                "config/allowed_to_enter",
                lambda value: value in ENTRY_LABELS[changed_entry],
                after_version=version,
            )
        finally:
            api.set_mode(original["entry"], original["exit"])
            probe.wait_for(
                "config/allowed_to_enter",
                lambda value: value in ENTRY_LABELS[original["entry"]],
            )
        return f"entry={changed_entry}; original restored"

    results.run("REST mode change is mirrored to MQTT", api_to_mqtt)

    def mqtt_to_api() -> str:
        original = api.mode()
        new_entry = different(original["entry"], ENTRY_VALUES)
        new_exit = different(original["exit"], EXIT_VALUES)
        try:
            entry_version = probe.version("config/allowed_to_enter")
            exit_version = probe.version("config/allowed_to_exit")
            probe.publish("config/allowed_to_enter/set", new_entry)
            probe.publish("config/allowed_to_exit/set", new_exit)
            probe.wait_for(
                "config/allowed_to_enter",
                lambda value: value in ENTRY_LABELS[new_entry],
                after_version=entry_version,
            )
            probe.wait_for(
                "config/allowed_to_exit",
                lambda value: value in EXIT_LABELS[new_exit],
                after_version=exit_version,
            )
            deadline = time.monotonic() + probe.timeout
            while api.mode() != {"entry": new_entry, "exit": new_exit}:
                if time.monotonic() >= deadline:
                    raise TestError("REST did not expose modes set over MQTT")
                time.sleep(0.1)
        finally:
            api.set_mode(original["entry"], original["exit"])
        return f"entry={new_entry}, exit={new_exit}; original restored"

    results.run("MQTT mode change is visible through REST", mqtt_to_api)


def check_mqtt_roundtrip_without_api(probe: MqttProbe, results: Results) -> None:
    def roundtrip() -> str:
        current_payload = probe.wait_for(
            "config/allowed_to_enter",
            lambda value: value in set().union(*ENTRY_LABELS.values()),
        )
        original = next(raw for raw, labels in ENTRY_LABELS.items() if current_payload in labels)
        changed = different(original, ENTRY_VALUES)
        try:
            version = probe.version("config/allowed_to_enter")
            probe.publish("config/allowed_to_enter/set", changed)
            probe.wait_for(
                "config/allowed_to_enter",
                lambda value: value in ENTRY_LABELS[changed],
                after_version=version,
            )
        finally:
            version = probe.version("config/allowed_to_enter")
            probe.publish("config/allowed_to_enter/set", original)
            probe.wait_for(
                "config/allowed_to_enter",
                lambda value: value in ENTRY_LABELS[original],
                after_version=version,
            )
        return f"entry={changed}; original restored"

    results.run("MQTT config roundtrip", roundtrip)


def check_doors(api: ApiClient, results: Results, timeout: float) -> None:
    def door_roundtrip(side: str) -> str:
        status = api.ok("GET", "/api/v1/status")
        door = status.get("door", {})
        key = f"{side}_unlocked"
        original = door.get(key)
        if door.get("available") is not True or not isinstance(original, bool):
            raise TestError("door hardware is unavailable")
        desired = not original

        def set_and_wait(unlocked: bool) -> None:
            action = f"{'unlock' if unlocked else 'lock'}_{side}"
            api.ok("POST", f"/api/v1/door/{action}")
            deadline = time.monotonic() + timeout
            while True:
                current = api.ok("GET", "/api/v1/status").get("door", {}).get(key)
                if current is unlocked:
                    return
                if time.monotonic() >= deadline:
                    raise TestError(f"timed out waiting for {action}")
                time.sleep(0.1)

        try:
            set_and_wait(desired)
        finally:
            set_and_wait(original)
        return f"{side} toggled; original restored"

    results.run("REST inside door roundtrip", lambda: door_roundtrip("inside"))
    results.run("REST outside door roundtrip", lambda: door_roundtrip("outside"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--init", action="store_true", help="interactively create/update the credential file")
    parser.add_argument("--base-url", help="base URL default for --init")
    parser.add_argument("--api-token", help="API token value for --init")
    parser.add_argument("--mqtt-host", help="MQTT host default for --init")
    parser.add_argument("--mqtt-port", type=int, help="MQTT port default for --init")
    parser.add_argument("--device-id", help="MQTT device id default for --init")
    parser.add_argument("--read-only", action="store_true", help="skip all mutating checks")
    parser.add_argument("--with-door", action="store_true", help="move both door magnets and restore their state")
    parser.add_argument("--only", choices=("api", "mqtt"), help="test only one interface")
    parser.add_argument("--timeout", type=float, default=10.0, help="network/state timeout in seconds (default: 10)")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.read_only and args.with_door:
        parser.error("--with-door cannot be combined with --read-only")
    if args.only == "mqtt" and args.with_door:
        parser.error("--with-door requires REST API tests")
    return args


def main() -> int:
    args = parse_args()
    path = args.credentials.expanduser().resolve()
    try:
        if args.init:
            initialise(args, path)
            return 0
        if not path.exists() and sys.stdin.isatty():
            if prompt_bool(f"{path} does not exist. Create it now", True):
                config = initialise(args, path)
            else:
                raise TestError(f"credential file not found; run with --init: {path}")
        else:
            config = load_config(path)
    except (TestError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    run_api = args.only != "mqtt"
    run_mqtt = args.only != "api"
    mqtt_config = config["mqtt"]
    print(f"REST target: {config['base_url']}")
    if run_mqtt:
        if mqtt_config["enabled"]:
            print(
                f"MQTT target: {mqtt_config['host']}:{mqtt_config['port']} "
                f"(device {mqtt_config['device_id']})"
            )
        else:
            print("MQTT target: disabled in credential file")
    print("Mutations: " + ("disabled" if args.read_only else "enabled"))
    print("Door movement: " + ("enabled" if args.with_door else "disabled"))
    print()

    results = Results()
    api = ApiClient(config["base_url"], config["api_token"], args.timeout)
    mode: dict[str, str] | None = None
    cats: list[dict[str, Any]] = []
    if run_api:
        mode, cats = check_api_reads(api, results)
        if args.read_only:
            results.skip("REST mode roundtrip", "--read-only")
            results.skip("REST cat setting roundtrip", "--read-only")
        else:
            check_api_mutations(api, results, mode, cats)
        if args.with_door:
            check_doors(api, results, args.timeout)

    probe: MqttProbe | None = None
    if run_mqtt and not mqtt_config["enabled"]:
        results.skip("MQTT suite", "disabled in credential file")
    elif run_mqtt:
        try:
            probe = MqttProbe(mqtt_config, args.timeout)
            probe.connect()
        except Exception as exc:
            results.fail("MQTT connection", str(exc))
        else:
            results.pass_("MQTT connection")
            try:
                check_mqtt_state(probe, results)
                if args.read_only:
                    results.skip("MQTT config roundtrip", "--read-only")
                elif run_api:
                    check_cross_interface(api, probe, results)
                else:
                    check_mqtt_roundtrip_without_api(probe, results)
            finally:
                probe.close()

    return results.summary()


if __name__ == "__main__":
    raise SystemExit(main())
