"""Outgoing webhook notifications for kittyhack events.

Users configure one or more HTTP endpoints in the Configuration tab. Each
endpoint picks:
- URL
- HTTP method (GET or POST)
- A subset of events to subscribe to (checkboxes in the UI)
- A payload template with `{variable}` placeholders
- Optional custom headers

When the backend records a motion-block event, `dispatch_event()` fans out to
every enabled webhook whose subscribed-events set intersects the motion block.
Each HTTP call runs in its own daemon thread with an explicit timeout so a
slow or hung endpoint cannot block the backend loop.

Storage: `webhooks.json` next to `config.ini`, mode 0o600, atomic write via
`.tmp` + `os.replace` — same pattern as `src/api.py` uses for `api_tokens.json`.
The file is gitignored so user configuration survives `update_kittyhack()`'s
`git clean -fd` step.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

from src.paths import kittyhack_root


WEBHOOKS_FILE = os.path.join(kittyhack_root(), "webhooks.json")

# Per-request timeout; user-configurable per hook but clamped to this maximum
# so a misconfigured value cannot stall the dispatcher indefinitely.
_REQUEST_TIMEOUT_MAX = 30.0
_REQUEST_TIMEOUT_DEFAULT = 5.0

# Keep payloads sane. A template that expands to megabytes would be pathological.
_MAX_PAYLOAD_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# Template variables
# ---------------------------------------------------------------------------

# Canonical list of variables that a user may reference in URL / headers /
# payload templates. Kept in sync with `_build_context()` below. The UI shows
# this list in a "Available variables" help modal so users know what to use.
TEMPLATE_VARIABLES: list[tuple[str, str, str]] = [
    # (name, description, example)
    ("event_type", "Internal event identifier", "cat_went_inside"),
    ("event_pretty", "Human-readable event name (localized)", "Cat went inside"),
    ("all_events", "Comma-separated list of all events in this motion block", "cat_went_inside,entry_per_cat_allowed"),
    ("timestamp", "ISO 8601 timestamp (UTC)", "2026-04-19T14:23:17+00:00"),
    ("timestamp_local", "ISO 8601 timestamp in the configured timezone", "2026-04-19T16:23:17+02:00"),
    ("timestamp_unix", "Unix seconds", "1797347097"),
    ("cat_name", "Detected cat name (empty if unknown)", "Whiskers"),
    ("cat_rfid", "RFID tag id, preferring RFID reader over camera match", "900123456789012"),
    ("cat_rfid_source", "Where cat_rfid came from: 'rfid', 'camera', or 'none'", "rfid"),
    ("prey_detected", "1 if prey was detected in this motion block, 0 otherwise", "1"),
    ("motion_block_id", "Numeric identifier for the motion block", "4281"),
    ("kittyflap_name", "Kittyflap host/friendly name", "kittyflap"),
]

TEMPLATE_VARIABLE_NAMES = [name for (name, _d, _e) in TEMPLATE_VARIABLES]


class _SafeDict(dict):
    """`.format_map` helper that leaves unknown `{foo}` placeholders untouched
    instead of raising `KeyError`. Lets users write templates defensively and
    makes it harmless to reference a variable that doesn't yet exist."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_template(template: str, context: dict[str, Any]) -> str:
    """Expand `{var}` placeholders against `context`. Unknown names pass through."""
    if not template:
        return ""
    try:
        return template.format_map(_SafeDict(context))
    except Exception as e:
        logging.warning(f"[NOTIFY] Template render failed: {e}; using raw template")
        return template


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _normalize_hook(raw: dict[str, Any]) -> dict[str, Any]:
    """Fill in defaults / coerce types for a single hook record.

    Returns a fresh dict so the caller can safely persist it without
    accidentally propagating derived fields back into config.
    """
    hook = {
        "id": str(raw.get("id") or secrets.token_hex(8)),
        "enabled": bool(raw.get("enabled", True)),
        "name": str(raw.get("name") or "").strip() or "Webhook",
        "url": str(raw.get("url") or "").strip(),
        "method": (str(raw.get("method") or "POST").strip().upper()),
        "events": list(raw.get("events") or []),
        "headers": dict(raw.get("headers") or {}),
        "payload_template": str(raw.get("payload_template") or ""),
        "timeout_seconds": float(raw.get("timeout_seconds") or _REQUEST_TIMEOUT_DEFAULT),
    }
    if hook["method"] not in ("GET", "POST"):
        hook["method"] = "POST"
    if hook["timeout_seconds"] <= 0 or hook["timeout_seconds"] > _REQUEST_TIMEOUT_MAX:
        hook["timeout_seconds"] = _REQUEST_TIMEOUT_DEFAULT
    return hook


def load_webhooks() -> list[dict[str, Any]]:
    """Load all configured webhooks. Missing file = no hooks."""
    if not os.path.exists(WEBHOOKS_FILE):
        return []
    try:
        with open(WEBHOOKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return [_normalize_hook(item) for item in data if isinstance(item, dict)]
    except Exception as e:
        logging.warning(f"[NOTIFY] Failed to read {WEBHOOKS_FILE}: {e}")
        return []


def save_webhooks(hooks: Iterable[dict[str, Any]]) -> None:
    """Persist hooks. Atomic write + restrictive permissions."""
    normalized = [_normalize_hook(h) for h in hooks]
    tmp = WEBHOOKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2, ensure_ascii=False)
    os.replace(tmp, WEBHOOKS_FILE)
    try:
        os.chmod(WEBHOOKS_FILE, 0o600)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _build_context(
    event_type: str,
    all_events: str,
    cat_name: str,
    cat_rfid: str | None,
    cat_rfid_source: str,
    motion_block_id: int | str,
    event_time: float,
) -> dict[str, Any]:
    """Build the variable bag that templates are rendered against."""
    try:
        from src.helper import EventType
        pretty = EventType.to_pretty_string(event_type)
    except Exception:
        pretty = str(event_type).replace("_", " ").capitalize()

    try:
        from src.helper import get_timezone
        local_ts = datetime.fromtimestamp(event_time, tz=get_timezone()).isoformat(timespec="seconds")
    except Exception:
        local_ts = datetime.fromtimestamp(event_time, tz=timezone.utc).isoformat(timespec="seconds")

    utc_ts = datetime.fromtimestamp(event_time, tz=timezone.utc).isoformat(timespec="seconds")

    prey = 1 if any(
        e.strip() in ("cat_went_inside_with_mouse", "motion_outside_with_mouse")
        for e in str(all_events).split(",")
    ) else 0

    try:
        from src.system import get_hostname
        host = get_hostname() or "kittyflap"
    except Exception:
        host = "kittyflap"

    return {
        "event_type": str(event_type),
        "event_pretty": str(pretty),
        "all_events": str(all_events),
        "timestamp": utc_ts,
        "timestamp_local": local_ts,
        "timestamp_unix": int(event_time),
        "cat_name": str(cat_name or ""),
        "cat_rfid": str(cat_rfid or ""),
        "cat_rfid_source": str(cat_rfid_source or "none"),
        "prey_detected": prey,
        "motion_block_id": str(motion_block_id),
        "kittyflap_name": host,
    }


def _send_one(hook: dict[str, Any], context: dict[str, Any]) -> None:
    """Fire a single webhook. Runs in its own daemon thread — exceptions are
    swallowed after logging so one bad endpoint cannot crash the dispatcher."""
    name = hook.get("name", "webhook")
    try:
        url = render_template(hook["url"], context)
        if not url:
            logging.warning(f"[NOTIFY] Hook '{name}' skipped — empty URL after rendering.")
            return

        headers = {k: render_template(v, context) for k, v in (hook.get("headers") or {}).items()}
        body = render_template(hook.get("payload_template", ""), context)
        if len(body.encode("utf-8", errors="replace")) > _MAX_PAYLOAD_BYTES:
            logging.warning(f"[NOTIFY] Hook '{name}' payload exceeds {_MAX_PAYLOAD_BYTES} B; skipping.")
            return

        method = hook.get("method", "POST")
        timeout = float(hook.get("timeout_seconds") or _REQUEST_TIMEOUT_DEFAULT)

        if method == "GET":
            r = requests.get(url, headers=headers, timeout=timeout)
        else:
            r = requests.post(url, headers=headers, data=body.encode("utf-8") if body else None, timeout=timeout)
        logging.info(f"[NOTIFY] Hook '{name}' {method} {url} → HTTP {r.status_code}")
    except requests.Timeout:
        logging.warning(f"[NOTIFY] Hook '{name}' timed out.")
    except requests.RequestException as e:
        logging.warning(f"[NOTIFY] Hook '{name}' request failed: {e}")
    except Exception as e:
        logging.warning(f"[NOTIFY] Hook '{name}' unexpected error: {e}")


def dispatch_event(
    event_type: str,
    all_events: str,
    cat_name: str = "",
    cat_rfid: str | None = None,
    cat_rfid_source: str = "none",
    motion_block_id: int | str = 0,
    event_time: float | None = None,
) -> None:
    """Fire every enabled webhook whose subscribed events intersect `all_events`.

    Non-blocking: each hook runs in its own daemon thread. Safe to call from
    the synchronous backend loop.
    """
    try:
        from src.baseconfig import CONFIG
        if not CONFIG.get("WEBHOOKS_ENABLED", True):
            return
    except Exception:
        pass

    if event_time is None:
        from time import time as _now
        event_time = _now()

    try:
        motion_events = {e.strip() for e in str(all_events).split(",") if e.strip()}
    except Exception:
        motion_events = {str(event_type)}
    if not motion_events:
        motion_events = {str(event_type)}

    hooks = load_webhooks()
    if not hooks:
        return

    context = _build_context(
        event_type=event_type,
        all_events=all_events,
        cat_name=cat_name,
        cat_rfid=cat_rfid,
        cat_rfid_source=cat_rfid_source,
        motion_block_id=motion_block_id,
        event_time=event_time,
    )

    for hook in hooks:
        if not hook.get("enabled"):
            continue
        subscribed = set(hook.get("events") or [])
        if not subscribed:
            continue
        if not (subscribed & motion_events):
            continue
        t = threading.Thread(
            target=_send_one,
            args=(hook, context),
            name=f"webhook-{hook.get('id','?')}",
            daemon=True,
        )
        t.start()


def send_test(hook: dict[str, Any]) -> tuple[bool, str]:
    """Synchronously fire a test invocation with sample variables. Used by the
    "Test" button in the Configuration tab. Returns (ok, short_status)."""
    context = _build_context(
        event_type="cat_went_inside",
        all_events="cat_went_inside",
        cat_name="Whiskers",
        cat_rfid="900123456789012",
        cat_rfid_source="rfid",
        motion_block_id=99999,
        event_time=datetime.now(tz=timezone.utc).timestamp(),
    )
    try:
        url = render_template(hook["url"], context)
        if not url:
            return False, "empty URL"
        headers = {k: render_template(v, context) for k, v in (hook.get("headers") or {}).items()}
        body = render_template(hook.get("payload_template", ""), context)
        method = (hook.get("method") or "POST").upper()
        timeout = float(hook.get("timeout_seconds") or _REQUEST_TIMEOUT_DEFAULT)
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=timeout)
        else:
            r = requests.post(url, headers=headers, data=body.encode("utf-8") if body else None, timeout=timeout)
        return (200 <= r.status_code < 300), f"HTTP {r.status_code}"
    except requests.Timeout:
        return False, "timeout"
    except requests.RequestException as e:
        return False, f"request failed: {e}"
    except Exception as e:
        return False, f"error: {e}"
