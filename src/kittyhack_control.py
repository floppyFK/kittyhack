import asyncio
import html
import json
import logging
import os
import threading
import time
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol

from src.baseconfig import CONFIG, configure_logging, set_language
from src.helper import sigterm_monitor
from src.magnets_rfid import Magnets, Rfid
from src.pir import Pir
from src.system import systemctl
from src.system import (
    get_wlan_connections,
    is_gateway_reachable,
    switch_wlan_connection,
    systemcmd,
    is_service_running,
)
from src.paths import install_base, kittyhack_root, pictures_root, models_yolo_root
from src.mode import is_remote_mode

from src.camera import VideoStream


# Prepare gettext for translations based on the configured language (mainly for consistent logs)
_ = set_language(CONFIG.get("LANGUAGE", "en"))


class ControlState:
    def __init__(self):
        self.controller: WebSocketServerProtocol | None = None
        self.controller_id: str | None = None
        self.controller_host: str | None = None
        self.control_timeout_s: float = float(CONFIG.get("REMOTE_CONTROL_TIMEOUT") or 10.0)
        # Monotonic timestamp of the last message seen from controller.
        # IMPORTANT: Use monotonic clock so NTP/RTC wall-clock jumps cannot
        # trigger false controller timeouts after cold boot.
        self.last_seen: float = 0.0

        # If the controller disconnects, we delay restarting kittyhack on the target device.
        # This prevents unwanted start/stop cycles when the remote UI service restarts.
        # Monotonic deadline when kittyhack should be started again after
        # controller disconnect timeout window.
        self.pending_kittyhack_start_at: float = 0.0
        self.enforce_stop_interval_s: float = float(CONFIG.get("REMOTE_ENFORCE_STOP_INTERVAL") or 5.0)
        self.next_enforce_stop_at: float = 0.0

        self.pir: Pir | None = None
        self.magnets: Magnets | None = None
        self.rfid: Rfid | None = None

        self._pir_stop_event: Any = None

        self.pir_thread: asyncio.Task | None = None
        self.state_task: asyncio.Task | None = None

        self.http_server: asyncio.base_events.Server | None = None
        self.sync_in_progress: bool = False

        # Boot wait behavior (target-mode only)
        self.boot_wait_active: bool = False
        self.boot_wait_deadline_ts: float = 0.0
        self.boot_wait_timeout_s: float = float(CONFIG.get("REMOTE_WAIT_AFTER_REBOOT_TIMEOUT") or 30.0)
        self.boot_wait_takeover_attempted: bool = False
        self.boot_wait_started_at: float = 0.0

    def is_controlled(self) -> bool:
        return self.controller is not None


STATE = ControlState()


_internal_cam_lock = threading.Lock()
_internal_cam_stream: VideoStream | None = None
_internal_cam_last_error: str = ""


def _ensure_internal_camera_stream() -> tuple[bool, str]:
    """Start (if needed) the target's internal camera stream for MJPEG relay.

    This is only used while `kittyhack_control` is active on the target device.
    The remote device will consume the stream via HTTP MJPEG (e.g. http://<target>/video).
    """
    global _internal_cam_stream, _internal_cam_last_error

    if str(CONFIG.get("CAMERA_SOURCE") or "").strip().lower() != "internal":
        return False, "internal camera relay disabled (CAMERA_SOURCE != internal)"

    with _internal_cam_lock:
        if _internal_cam_stream is not None and not getattr(_internal_cam_stream, "stopped", False):
            return True, ""

        try:
            # Keep it conservative: small resolution + modest FPS.
            # Remote inference can still upscale/downscale on its side if needed.
            _internal_cam_stream = VideoStream(
                resolution=(640, 360),
                framerate=10,
                jpeg_quality=75,
                source="internal",
            ).start()
            _internal_cam_last_error = ""
            logging.info("[CONTROL] Internal camera relay started for /video.")
            return True, ""
        except Exception as e:
            _internal_cam_stream = None
            _internal_cam_last_error = str(e)
            logging.warning(f"[CONTROL] Failed to start internal camera relay: {e}")
            return False, _internal_cam_last_error


def _internal_camera_latest_frame() -> Any:
    """Return the latest decoded BGR frame from the internal camera stream (or None)."""
    with _internal_cam_lock:
        stream = _internal_cam_stream
    if stream is None:
        return None
    try:
        return stream.read()
    except Exception:
        return None


def _remote_control_marker_path() -> str:
    # Marker: if it exists, kittyhack_control will wait for a remote control attempt after reboot.
    return os.path.join(kittyhack_root(), ".remote-control-session")


def _write_remote_control_marker() -> None:
    try:
        with open(_remote_control_marker_path(), "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _delete_remote_control_marker() -> bool:
    try:
        p = _remote_control_marker_path()
        if os.path.exists(p):
            os.remove(p)
        return True
    except Exception:
        return False


def _remote_control_marker_exists() -> bool:
    try:
        return os.path.exists(_remote_control_marker_path())
    except Exception:
        return False


def _wlan_action_marker_path() -> str:
    # Marker written by server.py while user-triggered WLAN actions are in progress.
    return os.path.join(kittyhack_root(), ".wlan-action-in-progress")


def _is_wlan_action_in_progress(max_age_s: float = 180.0) -> bool:
    """Return True if an intentional WLAN reconfiguration is currently ongoing.

    Stale markers are auto-cleaned to avoid permanently suppressing the watchdog
    after crashes.
    """
    marker = _wlan_action_marker_path()
    try:
        if not os.path.exists(marker):
            return False

        now = time.time()
        ts = 0.0
        try:
            with open(marker, "r", encoding="utf-8") as f:
                ts = float((f.read() or "").strip() or 0.0)
        except Exception:
            ts = 0.0

        if ts <= 0.0:
            try:
                ts = float(os.path.getmtime(marker))
            except Exception:
                ts = now

        age = max(0.0, now - ts)
        if age > float(max_age_s):
            try:
                os.remove(marker)
                logging.warning(f"[WLAN WATCHDOG] Removed stale WLAN action marker (age={age:.1f}s).")
            except Exception:
                pass
            return False

        return True
    except Exception:
        return False


def _ensure_dirs() -> None:
    os.makedirs(pictures_root(), exist_ok=True)
    os.makedirs(models_yolo_root(), exist_ok=True)


def _remote_ui_url() -> str | None:
    host = STATE.controller_host
    if not host:
        return None
    # remote-mode UI listens on port 80 by default
    return f"http://{host}/"


def _ui_language() -> str:
    """Return UI language for the standalone info pages.

    Driven by config.ini [Settings] language via CONFIG['LANGUAGE'].
    Supported: de/en. Fallback: en.
    """
    lang = str(CONFIG.get("LANGUAGE") or "en").strip().lower()
    if lang.startswith("de"):
        return "de"
    if lang.startswith("en"):
        return "en"
    return "en"


def _page_text() -> dict[str, str]:
    lang = _ui_language()
    texts: dict[str, dict[str, str]] = {
        "en": {
            "title_remote": "Kittyhack Remote Control",
            "title_startup": "Kittyhack Startup",
            "h_remote": "Kittyhack is in remote-control mode",
            "h_wait": "Waiting for remote connection",
            "remote_active": "Remote control active",
            "remote_ui": "Remote UI",
            "remote_ui_unknown": "unknown (controller IP not available yet)",
            "p_remote": (
                "This device is currently controlled via <code>kittyhack_control</code>. "
                "The normal Kittyhack UI on this device is stopped while control is active."
            ),
            "p_wait": "This Kittyflap is configured to wait for a remote-control connection after reboot.",
            "autostart_in": "Autostart Kittyhack in",
            "btn_skip": "Skip wait (start Kittyhack now)",
            "btn_disable": "Disable wait after reboot",
            "yes": "YES",
            "no": "NO",
            "st_remote_active": "remote control active",
            "st_remote_connected": "A remote controller is connected.",
            "st_starting": "starting...",
            "st_starting_msg": "Kittyhack is starting.",
            "st_remote_attempt": "remote attempt detected",
            "st_remote_attempt_msg": "Remote control attempt detected. Waiting for controller...",
            "seconds_suffix": " s",
        },
        "de": {
            "title_remote": "Kittyhack Fernsteuerung",
            "title_startup": "Kittyhack Start",
            "h_remote": "Kittyhack läuft im Fernsteuerungsmodus",
            "h_wait": "Warten auf Fernverbindung",
            "remote_active": "Fernsteuerung aktiv",
            "remote_ui": "Remote-UI",
            "remote_ui_unknown": "unbekannt (Controller-IP noch nicht verfügbar)",
            "p_remote": (
                "Dieses Gerät wird aktuell über <code>kittyhack_control</code> ferngesteuert. "
                "Die normale Kittyhack-UI auf diesem Gerät ist während der Fernsteuerung gestoppt."
            ),
            "p_wait": "Diese Kittyflap ist so konfiguriert, dass sie nach einem Neustart auf eine Fernsteuerungsverbindung wartet.",
            "autostart_in": "Kittyhack automatisch starten in",
            "btn_skip": "Wartezeit überspringen (Kittyhack jetzt starten)",
            "btn_disable": "Warten nach Neustart deaktivieren",
            "yes": "JA",
            "no": "NEIN",
            "st_remote_active": "Fernsteuerung aktiv",
            "st_remote_connected": "Ein Remote-Controller ist verbunden.",
            "st_starting": "wird gestartet...",
            "st_starting_msg": "Kittyhack startet.",
            "st_remote_attempt": "Remote-Versuch erkannt",
            "st_remote_attempt_msg": "Fernsteuerungsversuch erkannt. Warte auf Controller...",
            "seconds_suffix": " s",
        },
    }
    return texts.get(lang, texts["en"])


def _build_info_page_html() -> str:
    t = _page_text()
    lang = _ui_language()
    remote_url = _remote_ui_url()
    if remote_url:
        safe_url = html.escape(remote_url, quote=True)
        remote_link = (
            f"<div class=\"kv\"><div class=\"k\">{t['remote_ui']}</div>"
            f"<div class=\"v\"><a class=\"link\" href=\"{safe_url}\">{safe_url}</a></div></div>"
        )
    else:
        remote_link = (
            f"<div class=\"kv\"><div class=\"k\">{t['remote_ui']}</div>"
            f"<div class=\"v muted\">{t['remote_ui_unknown']}</div></div>"
        )

    controlled = t["yes"] if STATE.is_controlled() else t["no"]

    return (
        "<!doctype html>\n"
        f"<html lang=\"{lang}\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"  <title>{t['title_remote']}</title>\n"
        "  <meta name=\"color-scheme\" content=\"light dark\">\n"
        "  <style>\n"
        "    :root{--bg:#f6f7fb;--bg2:#eef2ff;--card:#ffffff;--text:#0b1220;--muted:#5b6475;"
        "          --border:rgba(15,23,42,.14);--shadow:0 12px 30px rgba(15,23,42,.10);--accent:#2563eb;}\n"
        "    @media (prefers-color-scheme: dark){\n"
        "      :root{--bg:#0b1020;--bg2:#111a33;--card:#0f172a;--text:#e5e7eb;--muted:#9aa3b2;"
        "            --border:rgba(226,232,240,.14);--shadow:0 18px 40px rgba(0,0,0,.35);--accent:#60a5fa;}\n"
        "    }\n"
        "    *{box-sizing:border-box;}\n"
        "    body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,sans-serif;"
        "         line-height:1.55;background:radial-gradient(1200px 700px at 15% 0%,var(--bg2),var(--bg));color:var(--text);}\n"
        "    .wrap{max-width:860px;margin:0 auto;padding:28px 18px 44px;}\n"
        "    .card{background:var(--card);border:1px solid var(--border);border-radius:18px;box-shadow:var(--shadow);padding:22px 20px;}\n"
        "    h1{font-size:1.55rem;letter-spacing:-.02em;margin:0 0 14px;}\n"
        "    p{margin:.45rem 0;}\n"
        "    code{background:rgba(148,163,184,.18);padding:.12rem .36rem;border-radius:8px;}\n"
        "    .kv{display:flex;gap:14px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--border);}\n"
        "    .kv:first-of-type{border-top:none;padding-top:0;}\n"
        "    .k{min-width:210px;color:var(--muted);font-size:.95rem;}\n"
        "    .v{font-weight:600;}\n"
        "    .muted{color:var(--muted);font-weight:500;}\n"
        "    .link{color:var(--accent);text-decoration:none;}\n"
        "    .link:hover{text-decoration:underline;}\n"
        "    .footer{margin-top:14px;color:var(--muted);font-size:.92rem;}\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <div class=\"wrap\">\n"
        f"    <h1>{t['h_remote']}</h1>\n"
        "    <div class=\"card\">\n"
        f"      <div class=\"kv\"><div class=\"k\">{t['remote_active']}</div><div class=\"v\">{controlled}</div></div>\n"
        f"      {remote_link}\n"
        f"      <p class=\"footer\">{t['p_remote']}</p>\n"
        "    </div>\n"
        "  </div>\n"
        "</body>\n"
        "</html>\n"
    )


def _build_boot_wait_page_html() -> str:
    # Minimal standalone UI: countdown + skip + disable.
    t = _page_text()
    lang = _ui_language()
    js_i18n = {
        "remote_active": t["st_remote_active"],
        "remote_connected": t["st_remote_connected"],
        "starting": t["st_starting"],
        "starting_msg": t["st_starting_msg"],
        "remote_attempt": t["st_remote_attempt"],
        "remote_attempt_msg": t["st_remote_attempt_msg"],
        "seconds_suffix": t["seconds_suffix"],
    }
    return (
        "<!doctype html>\n"
        f"<html lang=\"{lang}\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"  <title>{t['title_startup']}</title>\n"
        "  <meta name=\"color-scheme\" content=\"light dark\">\n"
        "  <style>\n"
        "    :root{--bg:#f6f7fb;--bg2:#eef2ff;--card:#ffffff;--text:#0b1220;--muted:#5b6475;"
        "          --border:rgba(15,23,42,.14);--shadow:0 12px 30px rgba(15,23,42,.10);--accent:#2563eb;}\n"
        "    @media (prefers-color-scheme: dark){\n"
        "      :root{--bg:#0b1020;--bg2:#111a33;--card:#0f172a;--text:#e5e7eb;--muted:#9aa3b2;"
        "            --border:rgba(226,232,240,.14);--shadow:0 18px 40px rgba(0,0,0,.35);--accent:#60a5fa;}\n"
        "    }\n"
        "    *{box-sizing:border-box;}\n"
        "    body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,sans-serif;"
        "         line-height:1.55;background:radial-gradient(1200px 700px at 15% 0%,var(--bg2),var(--bg));color:var(--text);}\n"
        "    .wrap{max-width:860px;margin:0 auto;padding:28px 18px 44px;}\n"
        "    .card{background:var(--card);border:1px solid var(--border);border-radius:18px;box-shadow:var(--shadow);padding:22px 20px;}\n"
        "    h1{font-size:1.55rem;letter-spacing:-.02em;margin:0 0 14px;}\n"
        "    p{margin:.45rem 0;}\n"
        "    .row{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:14px;}\n"
        "    button{padding:.72rem .95rem;border-radius:12px;border:1px solid var(--border);background:rgba(148,163,184,.12);color:var(--text);cursor:pointer;}\n"
        "    button.primary{background:var(--accent);border-color:transparent;color:#fff;font-weight:700;}\n"
        "    button:disabled{opacity:.55;cursor:not-allowed;}\n"
        "    code{background:rgba(148,163,184,.18);padding:.12rem .36rem;border-radius:8px;}\n"
        "    .muted{color:var(--muted);}\n"
        "    .mono{font-variant-numeric:tabular-nums;}\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <div class=\"wrap\">\n"
        f"    <h1>{t['h_wait']}</h1>\n"
        "    <div class=\"card\">\n"
        f"      <p>{t['p_wait']}</p>\n"
        f"      <p><strong>{t['autostart_in']}:</strong> <span class=\"mono\" id=\"countdown\">...</span></p>\n"
        "      <p class=\"muted\" id=\"status\" aria-live=\"polite\"></p>\n"
        "      <div class=\"row\">\n"
        f"        <button class=\"primary\" id=\"btnSkip\">{t['btn_skip']}</button>\n"
        f"        <button id=\"btnDisable\">{t['btn_disable']}</button>\n"
        "      </div>\n"
        "    </div>\n"
        "  </div>\n"
        "  <script>\n"
        f"    const I18N = {json.dumps(js_i18n, ensure_ascii=False)};\n"
        "    async function post(path){\n"
        "      try{ await fetch(path,{method:'POST'});}catch(e){}\n"
        "    }\n"
        "    async function poll(){\n"
        "      try{\n"
        "        const r = await fetch('/api/status',{cache:'no-store'});\n"
        "        const st = await r.json();\n"
        "        const cd = document.getElementById('countdown');\n"
        "        const msg = document.getElementById('status');\n"
        "        if(st.controlled){\n"
        "          cd.textContent = I18N.remote_active;\n"
        "          msg.textContent = I18N.remote_connected;\n"
        "          document.getElementById('btnSkip').disabled = true;\n"
        "          document.getElementById('btnDisable').disabled = true;\n"
        "          return;\n"
        "        }\n"
        "        if(!st.boot_wait_active){\n"
        "          cd.textContent = I18N.starting;\n"
        "          msg.textContent = I18N.starting_msg;\n"
        "          document.getElementById('btnSkip').disabled = true;\n"
        "          document.getElementById('btnDisable').disabled = true;\n"
        "          return;\n"
        "        }\n"
        "        if(st.boot_wait_takeover_attempted){\n"
        "          cd.textContent = I18N.remote_attempt;\n"
        "          msg.textContent = I18N.remote_attempt_msg;\n"
        "        }else{\n"
        "          cd.textContent = Math.max(0, Math.ceil(st.boot_wait_remaining_s)) + I18N.seconds_suffix;\n"
        "          msg.textContent = '';\n"
        "        }\n"
        "      }catch(e){}\n"
        "    }\n"
        "    document.getElementById('btnSkip').addEventListener('click',()=>post('/api/skip'));\n"
        "    document.getElementById('btnDisable').addEventListener('click',()=>post('/api/disable_wait'));\n"
        "    poll();\n"
        "    setInterval(poll,1000);\n"
        "  </script>\n"
        "</body>\n"
        "</html>\n"
    )


async def _http_info_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        # Read request line + headers (best-effort; do not block too long)
        method = "GET"
        path = "/"
        try:
            req_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            try:
                parts = (req_line.decode("utf-8", "ignore") or "").strip().split()
                if len(parts) >= 2:
                    method = parts[0].upper()
                    path = parts[1]
            except Exception:
                pass
        except Exception:
            pass

        # Drain headers
        for __ in range(50):
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break

        # MJPEG relay for the internal camera (target device)
        if path.startswith("/video"):
            if method != "GET":
                body = b"Method Not Allowed"
                headers = (
                    "HTTP/1.1 405 Method Not Allowed\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Cache-Control: no-store\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("utf-8")
                writer.write(headers)
                writer.write(body)
                await writer.drain()
                return

            ok, reason = _ensure_internal_camera_stream()
            if not ok:
                body = (reason or "Internal camera relay unavailable").encode("utf-8")
                headers = (
                    "HTTP/1.1 404 Not Found\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Cache-Control: no-store\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("utf-8")
                writer.write(headers)
                writer.write(body)
                await writer.drain()
                return

            boundary = "frame"
            headers = (
                "HTTP/1.1 200 OK\r\n"
                f"Content-Type: multipart/x-mixed-replace; boundary={boundary}\r\n"
                "Cache-Control: no-store\r\n"
                "Pragma: no-cache\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("utf-8")
            writer.write(headers)
            await writer.drain()

            # Stream forever (until the client disconnects or process exits).
            # Keep encoding work local to this handler so multiple clients can connect.
            import cv2  # local import to keep startup lightweight

            last_sent_at = 0.0
            min_interval_s = 0.09  # ~11 fps cap for safety
            while not sigterm_monitor.stop_now:
                try:
                    now = time.time()
                    if now - last_sent_at < min_interval_s:
                        await asyncio.sleep(0.02)
                        continue

                    frame = _internal_camera_latest_frame()
                    if frame is None:
                        await asyncio.sleep(0.05)
                        continue

                    # Offload encoding so we don't block the info-page HTTP server.
                    ok_enc, buf = await asyncio.to_thread(
                        cv2.imencode,
                        ".jpg",
                        frame,
                        [cv2.IMWRITE_JPEG_QUALITY, 75],
                    )
                    if not ok_enc:
                        await asyncio.sleep(0.05)
                        continue

                    jpg = buf.tobytes()
                    part = (
                        f"--{boundary}\r\n"
                        "Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpg)}\r\n"
                        "\r\n"
                    ).encode("utf-8") + jpg + b"\r\n"

                    writer.write(part)
                    await writer.drain()
                    last_sent_at = now
                except Exception:
                    # Client likely disconnected.
                    break
            return

        # Simple API for boot-wait UI
        if path.startswith("/api/status"):
            st = {
                "controlled": bool(STATE.is_controlled()),
                "boot_wait_active": bool(STATE.boot_wait_active),
                "boot_wait_takeover_attempted": bool(STATE.boot_wait_takeover_attempted),
                "boot_wait_remaining_s": max(0.0, float(STATE.boot_wait_deadline_ts or 0.0) - time.time()) if STATE.boot_wait_active else 0.0,
                "marker_exists": bool(_remote_control_marker_exists()),
            }
            body = json.dumps(st).encode("utf-8")
            headers = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("utf-8")

        elif path.startswith("/api/skip") and method == "POST":
            # Start kittyhack immediately (keep marker)
            asyncio.create_task(_start_kittyhack_from_control(reason="user skip"))
            body = b"OK"
            headers = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("utf-8")

        elif path.startswith("/api/disable_wait") and method == "POST":
            # Delete marker and start kittyhack immediately
            _delete_remote_control_marker()
            asyncio.create_task(_start_kittyhack_from_control(reason="user disable wait"))
            body = b"OK"
            headers = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("utf-8")

        else:
            # Default pages
            if STATE.boot_wait_active and not STATE.is_controlled():
                body = _build_boot_wait_page_html().encode("utf-8")
            else:
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


async def _start_kittyhack_from_control(reason: str) -> None:
    # Ensure port 80 is free before starting kittyhack.
    try:
        STATE.boot_wait_active = False
        STATE.boot_wait_deadline_ts = 0.0
        await _stop_info_http_server()
    except Exception:
        pass
    logging.info(f"[CONTROL] Starting kittyhack ({reason})")
    try:
        systemctl("start", "kittyhack")
    except Exception:
        pass


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
    STATE.next_enforce_stop_at = 0.0

    # If we are waiting for a possible reconnect, keep the info page on port 80
    # and do not start kittyhack yet.
    if STATE.pending_kittyhack_start_at and time.monotonic() < float(STATE.pending_kittyhack_start_at or 0.0):
        return

    STATE.pending_kittyhack_start_at = 0.0

    # Give port 80 back to kittyhack before restarting it
    await _stop_info_http_server()

    # Start kittyhack again
    systemctl("start", "kittyhack")


async def _take_control(ws: WebSocketServerProtocol, client_id: str, timeout_s: float):
    # Any successful/attempted take_control means remote control was used at least once.
    # This is used for target-mode boot behavior after reboot.
    STATE.boot_wait_takeover_attempted = True
    _write_remote_control_marker()

    if STATE.is_controlled():
        await ws.send(json.dumps({"type": "take_control_ack", "ok": False, "reason": "already_controlled"}))
        return False

    STATE.controller = ws
    STATE.controller_id = client_id
    STATE.control_timeout_s = float(timeout_s or STATE.control_timeout_s)
    STATE.last_seen = time.monotonic()
    STATE.next_enforce_stop_at = 0.0
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


async def _handle_sync_request(ws: WebSocketServerProtocol, include_labelstudio: bool = False):
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

    # Optional: Label Studio user data (only if present on target and requested).
    # These paths must be restored at the identical absolute location on the remote-mode device.
    if include_labelstudio:
        for src in (
            "/root/.config/label-studio",
            "/root/.local/share/label-studio",
        ):
            try:
                if os.path.exists(src):
                    # Encode as path relative to '/', so the receiver can extract to '/'
                    # and end up with /root/... on disk.
                    arc = src[1:] if src.startswith("/") else src
                    items.append((src, arc))
            except Exception:
                pass

    await ws.send(json.dumps({"type": "sync_begin", "ok": True, "items": [a for __, a in items]}))

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
                        STATE.last_seen = time.monotonic()
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
            STATE.last_seen = time.monotonic()

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
            STATE.last_seen = time.monotonic()

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
                include_labelstudio = bool(data.get("include_labelstudio", False))
                await _handle_sync_request(ws, include_labelstudio=include_labelstudio)
                continue

    except Exception:
        pass
    finally:
        if STATE.controller is ws:
            # Do not restart kittyhack immediately on disconnect. Instead, wait for the
            # configured timeout to allow the controller to reconnect (e.g. during
            # remote UI service restart).
            try:
                STATE.pending_kittyhack_start_at = time.monotonic() + float(STATE.control_timeout_s or 10.0)
            except Exception:
                STATE.pending_kittyhack_start_at = time.monotonic() + 10.0
            await _release_control("controller disconnected")


async def _watchdog():
    while not sigterm_monitor.stop_now:
        await asyncio.sleep(0.5)
        now = time.monotonic()

        # While remote-controlled, enforce that kittyhack.service stays stopped.
        if STATE.is_controlled() and now >= float(STATE.next_enforce_stop_at or 0.0):
            try:
                interval_s = max(0.5, min(30.0, float(STATE.enforce_stop_interval_s or 3.0)))
            except Exception:
                interval_s = 3.0
            STATE.next_enforce_stop_at = now + interval_s
            try:
                if is_service_running("kittyhack", log_output=False):
                    logging.warning("[CONTROL] kittyhack.service is active during remote control. Stopping it.")
                    systemctl("stop", "kittyhack")
            except Exception as e:
                logging.error(f"[CONTROL] Failed to enforce kittyhack stop while controlled: {e}")

        # If a controller disconnected recently, start kittyhack only after the timeout.
        if (not STATE.is_controlled()) and STATE.pending_kittyhack_start_at and (now >= float(STATE.pending_kittyhack_start_at or 0.0)):
            await _release_control("controller timeout")
            continue

        if STATE.is_controlled() and not STATE.sync_in_progress:
            if (now - STATE.last_seen) > float(STATE.control_timeout_s or 10.0):
                await _release_control("controller timeout")


async def _boot_wait_supervisor():
    # If the marker exists, we delay starting kittyhack after reboot.
    if not _remote_control_marker_exists():
        return

    try:
        timeout_s = float(CONFIG.get("REMOTE_WAIT_AFTER_REBOOT_TIMEOUT") or 30.0)
    except Exception:
        timeout_s = 30.0
    timeout_s = max(5.0, min(600.0, float(timeout_s)))

    STATE.boot_wait_active = True
    STATE.boot_wait_takeover_attempted = False
    STATE.boot_wait_started_at = time.time()
    STATE.boot_wait_deadline_ts = STATE.boot_wait_started_at + timeout_s

    # Ensure kittyhack is not running while we wait.
    try:
        systemctl("stop", "kittyhack")
    except Exception:
        pass

    # Serve countdown UI on port 80 during wait.
    await _start_info_http_server()

    while not sigterm_monitor.stop_now and STATE.boot_wait_active:
        await asyncio.sleep(0.5)

        # If controller already took over, keep waiting (kittyhack stays stopped).
        if STATE.is_controlled() or STATE.boot_wait_takeover_attempted:
            continue

        # Timeout reached with no remote take_control attempt: start kittyhack.
        if time.time() >= float(STATE.boot_wait_deadline_ts or 0.0):
            await _start_kittyhack_from_control(reason="boot wait timeout")
            return


async def _wlan_watchdog_loop():
    # Run on target device, independent from kittyhack.service.
    wlan_disconnect_counter = 0
    wlan_reconnect_attempted = False
    last_skip_log_ts = 0.0
    while not sigterm_monitor.stop_now:
        await asyncio.sleep(5.0)

        if not bool(CONFIG.get("WLAN_WATCHDOG_ENABLED", True)):
            wlan_disconnect_counter = 0
            wlan_reconnect_attempted = False
            continue

        # Pause watchdog actions during user-triggered WLAN reconfiguration from WebUI.
        if _is_wlan_action_in_progress():
            now = time.time()
            if (now - float(last_skip_log_ts or 0.0)) >= 30.0:
                logging.info("[WLAN WATCHDOG] User WLAN action in progress; skipping watchdog checks.")
                last_skip_log_ts = now
            wlan_disconnect_counter = 0
            wlan_reconnect_attempted = False
            continue

        # Determine WLAN state
        try:
            wlan_connections = get_wlan_connections()
            wlan_connected = any(wlan.get("connected") for wlan in wlan_connections)
            gateway_reachable = bool(is_gateway_reachable())
        except Exception as e:
            logging.error(f"[WLAN WATCHDOG] Failed to get WLAN state: {e}")
            wlan_connections = []
            wlan_connected = False
            gateway_reachable = False

        if wlan_connected and gateway_reachable:
            wlan_disconnect_counter = 0
            wlan_reconnect_attempted = False
            continue

        wlan_disconnect_counter += 1
        if wlan_disconnect_counter <= 5:
            logging.warning(
                f"[WLAN WATCHDOG] WLAN not fully connected (attempt {wlan_disconnect_counter}/5): "
                f"Interface connected: {wlan_connected}, Gateway reachable: {gateway_reachable}"
            )
        elif wlan_disconnect_counter <= 8:
            logging.error(
                f"[WLAN WATCHDOG] WLAN still not connected (attempt {wlan_disconnect_counter}/8)! "
                f"Interface connected: {wlan_connected}, Gateway reachable: {gateway_reachable}"
            )

        # Reconnect attempt after 5 failed checks (~25s)
        if wlan_disconnect_counter == 5 and not wlan_reconnect_attempted:
            logging.warning("[WLAN WATCHDOG] Attempting to reconnect WLAN after 5 failed checks...")
            try:
                sorted_wlans = sorted(wlan_connections, key=lambda w: int(w.get("priority", 0) or 0), reverse=True)[:6]
            except Exception:
                sorted_wlans = []

            for wlan in sorted_wlans:
                ssid = str(wlan.get("ssid") or "")
                if not ssid:
                    continue
                try:
                    systemctl("stop", "NetworkManager")
                    await asyncio.sleep(2.0)
                    systemctl("start", "NetworkManager")
                    await asyncio.sleep(2.0)
                    switch_wlan_connection(ssid)
                except Exception:
                    pass

                # Wait briefly for reconnection
                ok = False
                for __ in range(10):
                    await asyncio.sleep(1.0)
                    try:
                        wc = get_wlan_connections()
                        if any(w.get("connected") for w in wc) and is_gateway_reachable():
                            ok = True
                            break
                    except Exception:
                        pass
                if ok:
                    logging.info(f"[WLAN WATCHDOG] Successfully reconnected to SSID: {ssid}")
                    wlan_disconnect_counter = 0
                    wlan_reconnect_attempted = False
                    break

            wlan_reconnect_attempted = True

        # Reboot after 8 failed checks (~40s)
        if wlan_disconnect_counter >= 8:
            logging.error("[WLAN WATCHDOG] WLAN still not connected after reconnect attempts. Rebooting system...")
            try:
                systemcmd(["/sbin/reboot"], bool(CONFIG.get("SIMULATE_KITTYFLAP")))
            except Exception:
                pass
            return
        

async def main():
    configure_logging(CONFIG.get("LOGLEVEL", "INFO"))

    if is_remote_mode():
        logging.error("[CONTROL] Refusing to start: kittyhack_control must not run in remote-mode.")
        return

    # Align WLAN runtime settings with server.py startup behavior.
    try:
        logging.info(f"Setting WLAN TX Power to {CONFIG['WLAN_TX_POWER']} dBm...")
        systemcmd(["iwconfig", "wlan0", "txpower", f"{CONFIG['WLAN_TX_POWER']}"] , CONFIG['SIMULATE_KITTYFLAP'])
        logging.info("Disabling WiFi power saving mode...")
        systemcmd(["iw", "dev", "wlan0", "set", "power_save", "off"], CONFIG['SIMULATE_KITTYFLAP'])
    except Exception as e:
        logging.warning(f"[CONTROL] Failed to apply WLAN runtime settings: {e}")

    # Enforce target-mode boot semantics: kittyhack_control supervises kittyhack startup.
    # Best-effort: prevent kittyhack.service from auto-starting on subsequent boots.
    try:
        if is_service_running("kittyhack"):
            # Keep it running; we only enforce disable to ensure next boot starts via kittyhack_control.
            pass
        systemctl("disable", "kittyhack")
    except Exception:
        pass

    async with websockets.serve(_handler, host="0.0.0.0", port=8888, ping_interval=None):
        logging.info("[CONTROL] kittyhack_control listening on 0.0.0.0:8888")

        # Start WLAN watchdog (target side)
        asyncio.create_task(_wlan_watchdog_loop())

        # Boot wait supervisor (only if marker exists)
        asyncio.create_task(_boot_wait_supervisor())

        # If we are not in boot-wait mode, start kittyhack immediately.
        if not _remote_control_marker_exists():
            await _start_kittyhack_from_control(reason="boot: no remote marker")

        await _watchdog()


if __name__ == "__main__":
    asyncio.run(main())
