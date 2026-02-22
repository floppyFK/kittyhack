import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import websockets

from src.baseconfig import CONFIG
from src.clock import monotonic_time
from src.helper import sigterm_monitor


@dataclass
class RemoteStates:
    pir_inside: int = 0
    pir_outside: int = 0
    pir_inside_raw: int = 0
    pir_outside_raw: int = 0
    lock_inside_unlocked: bool = False
    lock_outside_unlocked: bool = False
    rfid_tag: str | None = None
    rfid_timestamp: float = 0.0
    rfid_field: bool = False


class RemoteControlClient:
    _instance: "RemoteControlClient | None" = None

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._connected = threading.Event()
        self._ready_for_use = threading.Event()
        self._ever_connected = threading.Event()
        self._lock = threading.Lock()
        self._states = RemoteStates()
        self._last_rx = 0.0
        self._control_acquired = False
        self._client_id = f"remote_{int(time.time())}"
        self._missing_target_host_logged = False
        self._pending_magnet_commands: set[str] = set()
        self._manual_disconnect = threading.Event()

        self._sync_tmp_path: str | None = None
        self._sync_started_at: float = 0.0
        self._sync_status_lock = threading.Lock()
        self._sync_done_event = threading.Event()
        self._sync_status: dict[str, Any] = {
            "requested": False,
            "in_progress": False,
            "ok": None,
            "reason": "",
            "requested_at": 0.0,
            "requested_at_mono": 0.0,
            "started_at": 0.0,
            "started_at_mono": 0.0,
            "finished_at": 0.0,
            "finished_at_mono": 0.0,
            "bytes_received": 0,
            "items": [],
        }

        self._target_update_done_event = threading.Event()
        self._target_update_status_lock = threading.Lock()
        self._target_update_status: dict[str, Any] = {
            "requested": False,
            "in_progress": False,
            "ok": None,
            "reason": "",
            "requested_at": 0.0,
            "requested_at_mono": 0.0,
            "started_at": 0.0,
            "started_at_mono": 0.0,
            "finished_at": 0.0,
            "finished_at_mono": 0.0,
        }
        self._target_reboot_ack_event = threading.Event()
        self._target_reboot_ack_ok = False
        self._target_version_event = threading.Event()
        self._target_version_lock = threading.Lock()
        self._target_version_info: dict[str, Any] = {
            "git_version": "",
            "latest_version": "",
            "received_at": 0.0,
        }

    @classmethod
    def instance(cls) -> "RemoteControlClient":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def ensure_started(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_thread, name="kittyhack-remote-control", daemon=True)
        self._thread.start()

    def get_states(self) -> RemoteStates:
        with self._lock:
            return RemoteStates(**self._states.__dict__)

    def get_tag(self) -> tuple[str | None, float]:
        s = self.get_states()
        return s.rfid_tag, s.rfid_timestamp

    def get_field(self) -> bool:
        return self.get_states().rfid_field

    def wait_until_ready(self, timeout: float = 30.0) -> bool:
        return self._ready_for_use.wait(timeout=timeout)

    def disconnect(self) -> None:
        """Manually disconnect and pause automatic reconnect attempts."""
        self._manual_disconnect.set()
        self._force_close_ws()

    def reconnect(self) -> None:
        """Resume automatic reconnect attempts immediately."""
        self._manual_disconnect.clear()
        self.ensure_started()
        self._force_close_ws()

    def is_manual_disconnect(self) -> bool:
        return self._manual_disconnect.is_set()

    def had_successful_connection(self) -> bool:
        return self._ever_connected.is_set()

    def _force_close_ws(self) -> None:
        ws = self._ws
        loop = self._loop
        if ws is not None and loop is not None:
            async def _close_socket() -> None:
                try:
                    await ws.close(code=1000, reason="manual_disconnect")
                except Exception:
                    pass

            try:
                asyncio.run_coroutine_threadsafe(_close_socket(), loop)
            except Exception:
                pass
        self._set_disconnected_state()

    def queue_magnet_command(self, command: str) -> None:
        cmd = str(command or "").strip()
        if cmd not in {"unlock_inside", "lock_inside", "unlock_outside", "lock_outside"}:
            return

        with self._lock:
            if cmd in self._pending_magnet_commands:
                return
            self._pending_magnet_commands.add(cmd)

        self._send_async({"type": "magnet", "command": cmd})

    def is_magnet_command_pending(self, command: str) -> bool:
        cmd = str(command or "").strip()
        with self._lock:
            return cmd in self._pending_magnet_commands

    def clear_pending_magnet_commands(self) -> None:
        with self._lock:
            self._pending_magnet_commands.clear()

    def set_rfid_field(self, enabled: bool) -> None:
        self._send_async({"type": "rfid", "field": bool(enabled)})

    def set_rfid_power(self, enabled: bool) -> None:
        self._send_async({"type": "rfid", "power": bool(enabled)})

    def stop_read(self) -> None:
        self._send_async({"type": "rfid", "stop": True})

    def ping(self) -> None:
        self._send_async({"type": "ping", "ts": time.time()})

    def request_sync_if_needed(self) -> None:
        if not CONFIG.get("REMOTE_SYNC_ON_FIRST_CONNECT", True):
            return

        marker = CONFIG.get("KITTYHACK_DATABASE_PATH", "kittyhack.db") + ".remote_synced"
        try:
            if os.path.exists(marker):
                return
        except Exception:
            return

        self.start_initial_sync(force=False)

    def start_initial_sync(self, force: bool = False) -> bool:
        if force:
            marker = CONFIG.get("KITTYHACK_DATABASE_PATH", "kittyhack.db") + ".remote_synced"
            try:
                if os.path.exists(marker):
                    os.remove(marker)
            except Exception:
                pass

        with self._sync_status_lock:
            self._sync_done_event.clear()
            self._sync_status.update(
                {
                    "requested": True,
                    "in_progress": False,
                    "ok": None,
                    "reason": "",
                    "requested_at": time.time(),
                    "requested_at_mono": monotonic_time(),
                    "started_at": 0.0,
                    "started_at_mono": 0.0,
                    "finished_at": 0.0,
                    "finished_at_mono": 0.0,
                    "bytes_received": 0,
                    "items": [],
                }
            )

        self._send_async(
            {
                "type": "sync_request",
                # Optional: include Label Studio user data (if present on target).
                "include_labelstudio": bool(CONFIG.get("REMOTE_SYNC_LABELSTUDIO", True)),
            }
        )
        return True

    def get_sync_status(self) -> dict[str, Any]:
        with self._sync_status_lock:
            return dict(self._sync_status)

    def wait_for_sync_completion(self, timeout: float = 300.0) -> bool:
        return self._sync_done_event.wait(timeout=timeout)

    def start_target_update(self, latest_version: str, current_version: str) -> bool:
        with self._target_update_status_lock:
            self._target_update_done_event.clear()
            self._target_update_status.update(
                {
                    "requested": True,
                    "in_progress": False,
                    "ok": None,
                    "reason": "",
                    "requested_at": time.time(),
                    "requested_at_mono": monotonic_time(),
                    "started_at": 0.0,
                    "started_at_mono": 0.0,
                    "finished_at": 0.0,
                    "finished_at_mono": 0.0,
                }
            )

        self._send_async(
            {
                "type": "update_request",
                "latest_version": str(latest_version or ""),
                "current_version": str(current_version or ""),
            }
        )
        return True

    def get_target_update_status(self) -> dict[str, Any]:
        with self._target_update_status_lock:
            return dict(self._target_update_status)

    def wait_for_target_update_completion(self, timeout: float = 1800.0) -> bool:
        return self._target_update_done_event.wait(timeout=timeout)

    def request_target_reboot(self, timeout: float = 5.0) -> bool:
        self._target_reboot_ack_event.clear()
        self._target_reboot_ack_ok = False
        self._send_async({"type": "reboot_request"})
        got_ack = self._target_reboot_ack_event.wait(timeout=max(0.0, float(timeout or 0.0)))
        return bool(got_ack and self._target_reboot_ack_ok)

    def request_target_version(self, timeout: float = 2.0) -> dict[str, Any] | None:
        """Request target version info via control channel.

        Returns the latest cached version info if a fresh response arrives within timeout.
        """
        if not self.wait_until_ready(timeout=max(0.0, min(3.0, float(timeout or 0.0)))):
            return None

        self._target_version_event.clear()
        self._send_async({"type": "version_request"})
        got = self._target_version_event.wait(timeout=max(0.0, float(timeout or 0.0)))
        if not got:
            return None
        return self.get_target_version_info()

    def get_target_version_info(self) -> dict[str, Any]:
        with self._target_version_lock:
            return dict(self._target_version_info)

    def abort_initial_sync(self, reason: str = "aborted by user") -> None:
        tmp_path = None
        with self._sync_status_lock:
            tmp_path = self._sync_tmp_path
            self._sync_tmp_path = None
            self._sync_status.update(
                {
                    "requested": False,
                    "in_progress": False,
                    "ok": False,
                    "reason": reason,
                    "finished_at": time.time(),
                    "finished_at_mono": monotonic_time(),
                }
            )
            self._sync_done_event.set()

        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    def _ws_url(self) -> str:
        host = (CONFIG.get("REMOTE_TARGET_HOST") or "").strip()
        port = int(CONFIG.get("REMOTE_CONTROL_PORT") or 8888)
        if not host:
            # Without a target host we can still run UI/inference, but sensors will be unavailable.
            return ""
        return f"ws://{host}:{port}"

    def _run_thread(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._main())
        except Exception as e:
            logging.error(f"[REMOTE_CTRL] Client thread crashed: {e}")

    def _set_disconnected_state(self) -> None:
        self._connected.clear()
        self._ready_for_use.clear()
        self._control_acquired = False
        self._ws = None
        self._target_reboot_ack_event.set()
        with self._lock:
            self._pending_magnet_commands.clear()

    async def _main(self) -> None:
        timeout_s = float(CONFIG.get("REMOTE_CONTROL_TIMEOUT") or 10.0)
        backoff = 1.0
        # We expect frequent state frames from target (~10Hz). If no frame arrives
        # for this period, treat the link as stale and reconnect proactively.
        no_rx_timeout_s = max(6.0, float(timeout_s) + 3.0)

        while not sigterm_monitor.stop_now:
            if self._manual_disconnect.is_set():
                self._set_disconnected_state()
                await asyncio.sleep(0.5)
                continue

            url = self._ws_url()
            if not url:
                if not self._missing_target_host_logged:
                    logging.info("[REMOTE_CTRL] REMOTE_TARGET_HOST is not configured yet; remote sensors/actors disabled until it is set.")
                    self._missing_target_host_logged = True
                await asyncio.sleep(1.0)
                continue

            self._missing_target_host_logged = False

            try:
                logging.info(f"[REMOTE_CTRL] Connecting to {url} ...")
                async with websockets.connect(url, ping_interval=None, max_size=2**24) as ws:
                    self._ws = ws
                    self._connected.set()
                    backoff = 1.0

                    await ws.send(
                        json.dumps(
                            {
                                "type": "take_control",
                                "client_id": self._client_id,
                                "timeout": timeout_s,
                            }
                        )
                    )

                    # Wait for ack
                    ack_raw = await asyncio.wait_for(ws.recv(), timeout=20.0)
                    ack = json.loads(ack_raw)
                    if not ack.get("ok"):
                        raise RuntimeError(f"take_control denied: {ack.get('reason')}")

                    self._control_acquired = True
                    self._ready_for_use.set()
                    self._ever_connected.set()
                    self._last_rx = time.time()
                    logging.info("[REMOTE_CTRL] Control acquired.")

                    # trigger sync if needed (best-effort)
                    self.request_sync_if_needed()

                    last_ping = 0.0
                    while not sigterm_monitor.stop_now:
                        # Keepalive
                        now = time.time()
                        if now - last_ping >= max(1.0, timeout_s / 3.0):
                            try:
                                await ws.send(json.dumps({"type": "ping", "ts": now}))
                            except Exception:
                                break
                            last_ping = now

                        # Connection may stay half-open on WLAN loss for several minutes.
                        # If no data arrived for too long, force reconnect so UI status updates.
                        if (now - float(self._last_rx or 0.0)) > no_rx_timeout_s:
                            raise RuntimeError(
                                f"stale remote link (no data for {int(now - float(self._last_rx or 0.0))}s)"
                            )

                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            # allow keepalive loop
                            continue

                        self._last_rx = time.time()
                        if isinstance(msg, (bytes, bytearray)):
                            await self._handle_binary(bytes(msg))
                        else:
                            await self._handle_json(msg)

            except Exception as e:
                self._set_disconnected_state()
                if not self._manual_disconnect.is_set():
                    logging.warning(f"[REMOTE_CTRL] Connection lost: {e}")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, 30.0)
                else:
                    backoff = 1.0
                    await asyncio.sleep(0.5)
            finally:
                # Also clear state on clean websocket close (no exception path).
                self._set_disconnected_state()

    async def _handle_json(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return

        t = data.get("type")
        if t == "state":
            with self._lock:
                self._states.pir_inside = int(data.get("pir_inside", self._states.pir_inside))
                self._states.pir_outside = int(data.get("pir_outside", self._states.pir_outside))
                self._states.pir_inside_raw = int(data.get("pir_inside_raw", self._states.pir_inside_raw))
                self._states.pir_outside_raw = int(data.get("pir_outside_raw", self._states.pir_outside_raw))
                self._states.lock_inside_unlocked = bool(data.get("lock_inside_unlocked", self._states.lock_inside_unlocked))
                self._states.lock_outside_unlocked = bool(data.get("lock_outside_unlocked", self._states.lock_outside_unlocked))
                self._states.rfid_field = bool(data.get("rfid_field", self._states.rfid_field))

                # Resolve local pending magnet commands once target state reflects them.
                if self._states.lock_inside_unlocked:
                    self._pending_magnet_commands.discard("unlock_inside")
                else:
                    self._pending_magnet_commands.discard("lock_inside")

                if self._states.lock_outside_unlocked:
                    self._pending_magnet_commands.discard("unlock_outside")
                else:
                    self._pending_magnet_commands.discard("lock_outside")

                tag = data.get("rfid_tag")
                ts = data.get("rfid_timestamp")
                if tag is None:
                    self._states.rfid_tag = None
                    self._states.rfid_timestamp = 0.0
                else:
                    self._states.rfid_tag = str(tag)
                    try:
                        self._states.rfid_timestamp = float(ts or 0.0)
                    except Exception:
                        self._states.rfid_timestamp = 0.0

        elif t == "sync_begin":
            with self._sync_status_lock:
                self._sync_status["in_progress"] = True
                self._sync_status["ok"] = None
                self._sync_status["reason"] = ""
                self._sync_status["started_at"] = time.time()
                self._sync_status["started_at_mono"] = monotonic_time()
                self._sync_status["finished_at"] = 0.0
                self._sync_status["finished_at_mono"] = 0.0
                self._sync_status["bytes_received"] = 0
                self._sync_status["items"] = list(data.get("items") or [])
            return

        elif t == "sync_end":
            ok = bool(data.get("ok", False))
            reason = str(data.get("reason") or "")
            with self._sync_status_lock:
                if self._sync_status.get("ok") is False:
                    # Keep a local extraction failure state; do not overwrite with remote sync_end success.
                    self._sync_status["in_progress"] = False
                    self._sync_status["finished_at"] = time.time()
                    self._sync_status["finished_at_mono"] = monotonic_time()
                    self._sync_done_event.set()
                    return
                self._sync_status["in_progress"] = False
                self._sync_status["ok"] = ok
                self._sync_status["reason"] = reason
                self._sync_status["finished_at"] = time.time()
                self._sync_status["finished_at_mono"] = monotonic_time()
            self._sync_done_event.set()
            return

        elif t == "update_begin":
            with self._target_update_status_lock:
                self._target_update_status["in_progress"] = True
                self._target_update_status["ok"] = None
                self._target_update_status["reason"] = ""
                self._target_update_status["started_at"] = time.time()
                self._target_update_status["started_at_mono"] = monotonic_time()
                self._target_update_status["finished_at"] = 0.0
                self._target_update_status["finished_at_mono"] = 0.0
            return

        elif t == "update_end":
            ok = bool(data.get("ok", False))
            reason = str(data.get("reason") or "")
            with self._target_update_status_lock:
                self._target_update_status["in_progress"] = False
                self._target_update_status["ok"] = ok
                self._target_update_status["reason"] = reason
                self._target_update_status["finished_at"] = time.time()
                self._target_update_status["finished_at_mono"] = monotonic_time()
            self._target_update_done_event.set()
            return

        elif t == "reboot_ack":
            self._target_reboot_ack_ok = bool(data.get("ok", False))
            self._target_reboot_ack_event.set()
            return

        elif t == "version_info":
            with self._target_version_lock:
                self._target_version_info = {
                    "git_version": str(data.get("git_version") or ""),
                    "latest_version": str(data.get("latest_version") or ""),
                    "received_at": time.time(),
                }
            self._target_version_event.set()
            return

    async def _handle_binary(self, payload: bytes) -> None:
        # Sync stream: tar.gz bytes framed as binary messages.
        # The first binary chunk after a sync_begin opens a local file.
        # For simplicity we detect the sync by a magic prefix.
        magic = b"KITTYHACK_SYNC_TAR_GZ\n"
        try:
            import tarfile
            from src.paths import install_base, models_yolo_root, pictures_root

            is_magic = payload.startswith(magic)
            body = payload[len(magic):] if is_magic else payload

            # End marker: a binary frame that is exactly the magic prefix.
            if is_magic and len(body) == 0 and self._sync_tmp_path:
                tmp_path = self._sync_tmp_path
                self._sync_tmp_path = None

                with tarfile.open(tmp_path, "r:gz") as tf:
                    base = install_base()
                    # Some synced paths must land outside install_base (absolute target paths).
                    # We encode those as paths relative to '/', e.g. 'root/.config/label-studio/...'.
                    ls_prefixes = (
                        "root/.config/label-studio",
                        "root/.local/share/label-studio",
                    )
                    for member in tf:
                        name = str(getattr(member, "name", "") or "")
                        if name.startswith(ls_prefixes):
                            tf.extract(member, path="/")
                        else:
                            tf.extract(member, path=base)

                os.makedirs(models_yolo_root(), exist_ok=True)
                os.makedirs(pictures_root(), exist_ok=True)

                marker = CONFIG.get("KITTYHACK_DATABASE_PATH", "kittyhack.db") + ".remote_synced"
                try:
                    with open(marker, "w", encoding="utf-8") as f:
                        f.write(str(time.time()))
                except Exception:
                    pass

                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

                logging.info("[REMOTE_CTRL] Initial sync extracted.")
                with self._sync_status_lock:
                    self._sync_status["in_progress"] = False
                    self._sync_status["ok"] = True
                    self._sync_status["reason"] = ""
                    self._sync_status["finished_at"] = time.time()
                    self._sync_status["finished_at_mono"] = monotonic_time()
                self._sync_done_event.set()
                return

            # Start new sync if we see a magic-prefixed frame and no active file.
            if is_magic and self._sync_tmp_path is None:
                self._sync_started_at = time.time()
                self._sync_tmp_path = os.path.join("/tmp", f"kittyhack_sync_{int(self._sync_started_at)}.tar.gz")
                try:
                    os.remove(self._sync_tmp_path)
                except Exception:
                    pass

            if self._sync_tmp_path is None:
                # Not in a sync session.
                return

            with open(self._sync_tmp_path, "ab") as f:
                f.write(body)
            with self._sync_status_lock:
                self._sync_status["bytes_received"] = int(self._sync_status.get("bytes_received", 0)) + len(body)
        except Exception as e:
            logging.warning(f"[REMOTE_CTRL] Sync extraction failed: {e}")
            with self._sync_status_lock:
                self._sync_status["in_progress"] = False
                self._sync_status["ok"] = False
                self._sync_status["reason"] = str(e)
                self._sync_status["finished_at"] = time.time()
                self._sync_status["finished_at_mono"] = monotonic_time()
            self._sync_done_event.set()

    def _send_async(self, msg: dict[str, Any]) -> None:
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self._send(msg), self._loop)

    async def _send(self, msg: dict[str, Any]) -> None:
        ws = self._ws
        if not ws:
            return
        try:
            await ws.send(json.dumps(msg))
        except Exception:
            pass
