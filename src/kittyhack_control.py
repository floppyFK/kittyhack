import asyncio
import json
import logging
import os
import time
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol

from src.baseconfig import CONFIG, configure_logging, set_language
from src.helper import sigterm_monitor
from src.magnets_rfid import Magnets, Rfid
from src.pir import Pir
from src.system import systemctl
from src.paths import install_base, kittyhack_root, pictures_root, models_yolo_root
from src.mode import is_remote_mode


# Prepare gettext for translations based on the configured language (mainly for consistent logs)
_ = set_language(CONFIG.get("LANGUAGE", "en"))


class ControlState:
    def __init__(self):
        self.controller: WebSocketServerProtocol | None = None
        self.controller_id: str | None = None
        self.controller_host: str | None = None
        self.control_timeout_s: float = float(CONFIG.get("REMOTE_CONTROL_TIMEOUT") or 10.0)
        self.last_seen: float = 0.0

        # If the controller disconnects, we delay restarting kittyhack on the target device.
        # This prevents unwanted start/stop cycles when the remote UI service restarts.
        self.pending_kittyhack_start_at: float = 0.0

        self.pir: Pir | None = None
        self.magnets: Magnets | None = None
        self.rfid: Rfid | None = None

        self._pir_stop_event: Any = None

        self.pir_thread: asyncio.Task | None = None
        self.state_task: asyncio.Task | None = None

        self.http_server: asyncio.base_events.Server | None = None
        self.sync_in_progress: bool = False

    def is_controlled(self) -> bool:
        return self.controller is not None


STATE = ControlState()


def _ensure_dirs() -> None:
    os.makedirs(pictures_root(), exist_ok=True)
    os.makedirs(models_yolo_root(), exist_ok=True)


def _remote_ui_url() -> str | None:
    host = STATE.controller_host
    if not host:
        return None
    # remote-mode UI listens on port 80 by default
    return f"http://{host}/"


def _build_info_page_html() -> str:
    remote_url = _remote_ui_url()
    if remote_url:
        remote_link = f"<p>Remote UI: <a href=\"{remote_url}\">{remote_url}</a></p>"
    else:
        remote_link = "<p>Remote UI: unknown (controller IP not available yet)</p>"

    controlled = "YES" if STATE.is_controlled() else "NO"
    controller = STATE.controller_id or "unknown"

    return (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "  <title>Kittyhack Remote Control</title>\n"
        "  <style>\n"
        "    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,sans-serif;"
        "         margin:2rem;max-width:55rem;line-height:1.5;}\n"
        "    .card{border:1px solid #ddd;border-radius:12px;padding:1.25rem;}\n"
        "    code{background:#f6f8fa;padding:.15rem .35rem;border-radius:6px;}\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <h1>Kittyhack is in remote-control mode</h1>\n"
        "  <div class=\"card\">\n"
        f"    <p><strong>Controlled:</strong> {controlled}</p>\n"
        f"    <p><strong>Controller ID:</strong> <code>{controller}</code></p>\n"
        f"    {remote_link}\n"
        "    <p>This device is currently controlled via <code>kittyhack_control</code> (WebSocket port 8888)."
        "       The normal Kittyhack UI on this device is stopped while control is active.</p>\n"
        "  </div>\n"
        "</body>\n"
        "</html>\n"
    )


async def _http_info_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        # Read request line + headers (best-effort; do not block too long)
        try:
            await asyncio.wait_for(reader.readline(), timeout=2.0)
        except Exception:
            pass

        # Drain headers
        for _ in range(50):
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break

        body = _build_info_page_html().encode("utf-8")
        headers = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("utf-8")
        writer.write(headers)
        writer.write(body)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _start_info_http_server() -> None:
    if STATE.http_server is not None:
        return
    try:
        STATE.http_server = await asyncio.start_server(_http_info_handler, host="0.0.0.0", port=80)
        logging.info("[CONTROL] Info page listening on 0.0.0.0:80")
    except Exception as e:
        STATE.http_server = None
        logging.warning(f"[CONTROL] Could not start info page on port 80: {e}")


async def _stop_info_http_server() -> None:
    if STATE.http_server is None:
        return
    try:
        STATE.http_server.close()
        await STATE.http_server.wait_closed()
    except Exception:
        pass
    finally:
        STATE.http_server = None


async def _publisher(ws: WebSocketServerProtocol):
    while not sigterm_monitor.stop_now and STATE.controller is ws:
        try:
            pir_outside = pir_inside = pir_outside_raw = pir_inside_raw = 0
            if STATE.pir:
                pir_outside, pir_inside, pir_outside_raw, pir_inside_raw = STATE.pir.get_states()

            lock_inside_unlocked = False
            lock_outside_unlocked = False
            if STATE.magnets:
                lock_inside_unlocked = bool(STATE.magnets.get_inside_state())
                lock_outside_unlocked = bool(STATE.magnets.get_outside_state())

            rfid_tag = None
            rfid_ts = 0.0
            rfid_field = False
            if STATE.rfid:
                try:
                    rfid_tag, rfid_ts = STATE.rfid.get_tag()
                except Exception:
                    rfid_tag, rfid_ts = None, 0.0
                try:
                    rfid_field = bool(STATE.rfid.get_field())
                except Exception:
                    rfid_field = False

            payload = {
                "type": "state",
                "pir_inside": int(pir_inside),
                "pir_outside": int(pir_outside),
                "pir_inside_raw": int(pir_inside_raw),
                "pir_outside_raw": int(pir_outside_raw),
                "lock_inside_unlocked": lock_inside_unlocked,
                "lock_outside_unlocked": lock_outside_unlocked,
                "rfid_tag": rfid_tag,
                "rfid_timestamp": float(rfid_ts or 0.0),
                "rfid_field": rfid_field,
            }
            await ws.send(json.dumps(payload))
        except Exception:
            break
        await asyncio.sleep(0.1)


async def _release_control(reason: str):
    logging.warning(f"[CONTROL] Releasing control: {reason}")

    try:
        if STATE._pir_stop_event is not None:
            try:
                STATE._pir_stop_event.set()
            except Exception:
                pass

        if STATE.magnets:
            STATE.magnets.empty_queue(shutdown=True)
    except Exception:
        pass

    try:
        if STATE.rfid:
            STATE.rfid.stop_read(wait_for_stop=False)
            STATE.rfid.set_field(False)
            STATE.rfid.set_power(False)
    except Exception:
        pass

    STATE.controller = None
    STATE.controller_id = None
    STATE.controller_host = None
    STATE.last_seen = 0.0
    STATE._pir_stop_event = None
    STATE.sync_in_progress = False

    # If we are waiting for a possible reconnect, keep the info page on port 80
    # and do not start kittyhack yet.
    if STATE.pending_kittyhack_start_at and time.time() < float(STATE.pending_kittyhack_start_at or 0.0):
        return

    STATE.pending_kittyhack_start_at = 0.0

    # Give port 80 back to kittyhack before restarting it
    await _stop_info_http_server()

    # Start kittyhack again
    systemctl("start", "kittyhack")


async def _take_control(ws: WebSocketServerProtocol, client_id: str, timeout_s: float):
    if STATE.is_controlled():
        await ws.send(json.dumps({"type": "take_control_ack", "ok": False, "reason": "already_controlled"}))
        return False

    STATE.controller = ws
    STATE.controller_id = client_id
    STATE.control_timeout_s = float(timeout_s or STATE.control_timeout_s)
    STATE.last_seen = time.time()
    # Cancel any delayed kittyhack start from a previous disconnect.
    STATE.pending_kittyhack_start_at = 0.0

    try:
        ra = getattr(ws, "remote_address", None)
        if isinstance(ra, (list, tuple)) and len(ra) >= 1:
            STATE.controller_host = str(ra[0] or "") or None
    except Exception:
        STATE.controller_host = None

    logging.info(f"[CONTROL] Taking control for client_id={client_id}")

    # Stop kittyhack service, wait 1s
    systemctl("stop", "kittyhack")
    await asyncio.sleep(1.0)

    # Serve an info page on port 80 while kittyhack is stopped
    await _start_info_http_server()

    # Init hardware locally (target device)
    _ensure_dirs()

    import threading
    STATE._pir_stop_event = threading.Event()
    STATE.pir = Pir(simulate_kittyflap=bool(CONFIG.get("SIMULATE_KITTYFLAP")), stop_event=STATE._pir_stop_event)
    STATE.pir.init()
    STATE.magnets = Magnets(simulate_kittyflap=bool(CONFIG.get("SIMULATE_KITTYFLAP")))
    STATE.magnets.init()
    STATE.magnets.start_magnet_control()
    STATE.rfid = Rfid(simulate_kittyflap=bool(CONFIG.get("SIMULATE_KITTYFLAP")))

    # Start PIR read loop in a thread-like task (it is blocking/sleeping)
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, STATE.pir.read)
    loop.run_in_executor(None, STATE.rfid.run)

    # Start state publisher
    STATE.state_task = asyncio.create_task(_publisher(ws))

    await ws.send(json.dumps({"type": "take_control_ack", "ok": True, "timeout": STATE.control_timeout_s}))
    return True


async def _handle_sync_request(ws: WebSocketServerProtocol):
    STATE.sync_in_progress = True
    # Safety: ensure target kittyhack service is stopped before we package/sync files.
    # This avoids copying data while files may still be modified by the running service.
    try:
        systemctl("stop", "kittyhack")
        await asyncio.sleep(1.0)
    except Exception as e:
        logging.error(f"[CONTROL] Failed to stop kittyhack before sync: {e}")
        await ws.send(json.dumps({"type": "sync_end", "ok": False, "reason": f"failed to stop kittyhack: {e}"}))
        STATE.sync_in_progress = False
        return

    base = install_base()

    items: list[tuple[str, str]] = []

    # kittyhack.db + config.ini from repo root
    for name in ("kittyhack.db", "config.ini"):
        src = os.path.join(kittyhack_root(), name)
        if os.path.exists(src):
            arc = os.path.relpath(src, base)
            items.append((src, arc))

    src = pictures_root()
    if os.path.exists(src):
        arc = os.path.relpath(src, base)
        items.append((src, arc))

    src = models_yolo_root()
    if os.path.exists(src):
        arc = os.path.relpath(src, base)
        items.append((src, arc))

    await ws.send(json.dumps({"type": "sync_begin", "ok": True, "items": [a for _, a in items]}))

    try:
        import queue
        import threading
        import tarfile

        # True streaming: create tar.gz directly into websocket-bound chunks.
        # This avoids creating a large temporary file on the source device.
        chunk_queue: "queue.Queue[bytes | Exception | None]" = queue.Queue(maxsize=16)

        class _QueueWriter:
            def __init__(self, q: "queue.Queue[bytes | Exception | None]"):
                self.q = q

            def write(self, data):
                if data:
                    self.q.put(bytes(data))
                return len(data or b"")

            def flush(self):
                return

        def _produce_tar_stream():
            try:
                writer = _QueueWriter(chunk_queue)
                with tarfile.open(mode="w|gz", fileobj=writer) as tf:
                    for src, arc in items:
                        tf.add(src, arcname=arc)
                        # prevent watchdog timeout during potentially long archive creation
                        STATE.last_seen = time.time()
                chunk_queue.put(None)
            except Exception as e:
                chunk_queue.put(e)

        producer = threading.Thread(target=_produce_tar_stream, daemon=True)
        producer.start()

        # Stream chunks as they are produced.
        magic = b"KITTYHACK_SYNC_TAR_GZ\n"
        first = True
        while True:
            q_item = await asyncio.to_thread(chunk_queue.get)
            if q_item is None:
                break
            if isinstance(q_item, Exception):
                raise q_item
            chunk = q_item
            if first:
                await ws.send(magic + chunk)
                first = False
            else:
                await ws.send(chunk)
            # prevent watchdog timeout during potentially long transfer
            STATE.last_seen = time.time()

        # End marker (empty chunk)
        if first:
            # No payload chunks produced (edge case): start sync stream explicitly.
            await ws.send(magic)
        await ws.send(magic)

        await ws.send(json.dumps({"type": "sync_end", "ok": True}))
    except Exception as e:
        logging.error(f"[CONTROL] Sync failed: {e}")
        await ws.send(json.dumps({"type": "sync_end", "ok": False, "reason": str(e)}))
    finally:
        STATE.sync_in_progress = False


async def _handler(ws: WebSocketServerProtocol):
    try:
        async for msg in ws:
            STATE.last_seen = time.time()

            if isinstance(msg, (bytes, bytearray)):
                continue

            try:
                data = json.loads(msg)
            except Exception:
                continue

            t = data.get("type")

            if t == "take_control":
                client_id = str(data.get("client_id") or "")
                timeout_s = float(data.get("timeout") or STATE.control_timeout_s)
                await _take_control(ws, client_id, timeout_s)
                continue

            if STATE.controller is not ws:
                await ws.send(json.dumps({"type": "error", "reason": "not_controller"}))
                continue

            if t == "ping":
                continue

            if t == "magnet":
                cmd = str(data.get("command") or "")
                if STATE.magnets:
                    STATE.magnets.queue_command(cmd)
                continue

            if t == "rfid":
                if STATE.rfid:
                    if "field" in data:
                        STATE.rfid.set_field(bool(data.get("field")))
                    if "power" in data:
                        STATE.rfid.set_power(bool(data.get("power")))
                    if data.get("stop"):
                        STATE.rfid.stop_read(wait_for_stop=False)
                continue

            if t == "sync_request":
                await _handle_sync_request(ws)
                continue

    except Exception:
        pass
    finally:
        if STATE.controller is ws:
            # Do not restart kittyhack immediately on disconnect. Instead, wait for the
            # configured timeout to allow the controller to reconnect (e.g. during
            # remote UI service restart).
            try:
                STATE.pending_kittyhack_start_at = time.time() + float(STATE.control_timeout_s or 10.0)
            except Exception:
                STATE.pending_kittyhack_start_at = time.time() + 10.0
            await _release_control("controller disconnected")


async def _watchdog():
    while not sigterm_monitor.stop_now:
        await asyncio.sleep(0.5)
        now = time.time()

        # If a controller disconnected recently, start kittyhack only after the timeout.
        if (not STATE.is_controlled()) and STATE.pending_kittyhack_start_at and (now >= float(STATE.pending_kittyhack_start_at or 0.0)):
            await _release_control("controller timeout")
            continue

        if STATE.is_controlled() and not STATE.sync_in_progress:
            if (now - STATE.last_seen) > float(STATE.control_timeout_s or 10.0):
                await _release_control("controller timeout")


async def main():
    configure_logging(CONFIG.get("LOGLEVEL", "INFO"))

    if is_remote_mode():
        logging.error("[CONTROL] Refusing to start: kittyhack_control must not run in remote-mode.")
        return

    async with websockets.serve(_handler, host="0.0.0.0", port=8888, ping_interval=None):
        logging.info("[CONTROL] kittyhack_control listening on 0.0.0.0:8888")
        await _watchdog()


if __name__ == "__main__":
    asyncio.run(main())
