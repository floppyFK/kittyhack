"""Camera stream helpers for ModelHandler."""
import logging

from src.baseconfig import CONFIG
from src.mode import is_remote_mode


def _remote_internal_proxy_url() -> str | None:
    """Return implicit target MJPEG URL for remote-mode internal camera selection."""
    if not is_remote_mode():
        return None

    camera_source = str(CONFIG.get("CAMERA_SOURCE") or "").strip().lower()
    if camera_source != "internal":
        return None

    host = str(CONFIG.get("REMOTE_TARGET_HOST") or "").strip()
    if not host:
        return None

    if host.startswith("http://") or host.startswith("https://"):
        base = host
    else:
        base = f"http://{host}"
    return base.rstrip("/") + "/video"


def _remote_control_disconnected() -> bool:
    """True when remote control is intentionally or effectively disconnected."""
    if not is_remote_mode():
        return False

    host = str(CONFIG.get("REMOTE_TARGET_HOST") or "").strip()
    if not host:
        return False

    try:
        from src.remote.control_client import RemoteControlClient

        client = RemoteControlClient.instance()
        client.ensure_started()
        if bool(getattr(client, "is_manual_disconnect", lambda: False)()):
            return True

        ready = bool(client.wait_until_ready(timeout=0))
        if ready:
            return False

        had_connection = bool(getattr(client, "had_successful_connection", lambda: False)())
        return had_connection
    except Exception:
        return False


def _effective_camera_stream_config() -> tuple[str, str]:
    """Resolve runtime stream source/url from current CONFIG.

    In remote-mode with CAMERA_SOURCE=internal, stream from target MJPEG relay
    (`http://REMOTE_TARGET_HOST/video`) while keeping config unchanged.
    """
    proxy_url = _remote_internal_proxy_url()
    if proxy_url:
        source = "ip_camera"
        ip_url = proxy_url
    else:
        source = str(CONFIG.get("CAMERA_SOURCE") or "internal")
        ip_url = str(CONFIG.get("IP_CAMERA_URL") or "")

    # In remote-mode: when the control link is disconnected, close any IP camera
    # stream (including implicit remote /video relay) until reconnected.
    if is_remote_mode() and str(source or "").strip().lower() == "ip_camera":
        if _remote_control_disconnected():
            return "disconnected", ""

    return source, ip_url


def _is_remote_internal_proxy_stream(source: str, url: str) -> bool:
    """True if runtime stream is the implicit remote internal-camera MJPEG relay."""
    if not is_remote_mode():
        return False
    if str(CONFIG.get("CAMERA_SOURCE") or "").strip().lower() != "internal":
        return False
    if str(source or "").strip().lower() != "ip_camera":
        return False
    u = str(url or "").strip().lower()
    return bool(u) and u.endswith("/video")

