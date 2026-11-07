"""Shared constants and headers for the kittyhack-model-server API."""

from __future__ import annotations

import configparser
import os
import re

BASE_URL = "https://kittyhack-models.fk-cloud.de"
PUBLIC_MODEL_SERVER_URL = BASE_URL

# Shared with the model-server (KITTYHACK_CLIENT_TOKEN). Open-source on purpose:
# it only raises the bar against unauthenticated scanners, not determined attackers.
KITTYHACK_CLIENT_TOKEN = "IWbhqLFO6Tvalh3KpQJMRKYfQTke_FsF7JLXZ8jIWJI"

HEADER_CLIENT = "X-Kittyhack-Client"
HEADER_VERSION = "X-Kittyhack-Version"
HEADER_JOB_TOKEN = "X-Job-Token"

_HOST_CHARS_RE = re.compile(r"^[A-Za-z0-9.-]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?$")
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def model_server_headers(*, job_token: str | None = None, version: str = "") -> dict[str, str]:
    """Return headers sent on every model-server call from kittyhack v3+."""
    headers = {
        HEADER_CLIENT: KITTYHACK_CLIENT_TOKEN,
        HEADER_VERSION: version or "3.0.0",
    }
    if job_token:
        headers[HEADER_JOB_TOKEN] = job_token
    return headers


def normalize_model_server_host(value: str) -> str | None:
    """Return a host, '' when empty, or None when the text cannot be a host.

    Incomplete values such as ``192.168.`` are returned as-is so typing is not
    rejected. Callers that build a URL must also require a finished host.
    """
    host = (value or "").strip()
    if not host:
        return ""
    if not _HOST_CHARS_RE.fullmatch(host) or ".." in host:
        return None
    return host


def _complete_host(host: str) -> bool:
    """True when a host is finished enough to connect to.

    A number such as ``192`` or ``192.168.1`` is an unfinished IPv4 address.
    It must not be treated as a hostname, or Kittyhack connects while the
    address is still being typed.
    """
    if not host or ".." in host or not _HOST_RE.fullmatch(host):
        return False
    if host.replace(".", "").isdigit():
        match = _IPV4_RE.fullmatch(host)
        if match is None:
            return False
        return all(0 <= int(part) <= 255 for part in match.groups())
    return True


def local_model_server_available() -> bool:
    """True when this computer has a model server config next to Kittyhack."""
    from src.paths import model_server_root

    return os.path.isfile(os.path.join(model_server_root(), "config.ini"))


def local_model_server_port() -> int:
    """Port from the local model server config, or 8000."""
    from src.paths import model_server_root

    path = os.path.join(model_server_root(), "config.ini")
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
        raw = parser.get("server", "port", fallback="8000")
        port = int(str(raw).strip())
    except Exception:
        return 8000
    if 1 <= port <= 65535:
        return port
    return 8000


def resolved_model_server_base_url() -> str:
    """Base URL for the model server selected in the Kittyhack settings."""
    from src.baseconfig import CONFIG

    mode = str(CONFIG.get("MODEL_SERVER_MODE") or "public").strip().lower()
    if mode == "local":
        if not local_model_server_available():
            return ""
        return f"http://127.0.0.1:{local_model_server_port()}"
    if mode == "custom":
        host = normalize_model_server_host(str(CONFIG.get("MODEL_SERVER_HOST") or ""))
        try:
            port = int(CONFIG.get("MODEL_SERVER_PORT") or 0)
        except (TypeError, ValueError):
            return ""
        if host is None or not _complete_host(host or "") or not (1 <= port <= 65535):
            return ""
        return f"http://{host}:{port}"
    return PUBLIC_MODEL_SERVER_URL
