"""Read the model server config file"""

from __future__ import annotations

import configparser
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("kittyhack.model_server")

_DEVICE_RE = re.compile(r"^(auto|cpu|cuda(?::\d+)?)$")


@dataclass(frozen=True)
class ServerSettings:
    port: int = 8000
    epochs: int = 60
    imgsz: int = 320
    batch: int = 16
    patience: int = 50
    workers: int = 4
    device: str = "auto"


def _positive_int(raw: str, fallback: int, *, allow_zero: bool = False) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    if value > 0 or (allow_zero and value == 0):
        return value
    return fallback


def _image_size(raw: str, fallback: int) -> int:
    value = _positive_int(raw, fallback)
    if value % 32 != 0:
        return fallback
    return value


def load_settings(path: Path) -> ServerSettings:
    """Load `config.ini`. Missing or invalid values fall back to the defaults."""
    defaults = ServerSettings()
    if not path.is_file():
        log.info("No config file at %s; using defaults", path)
        return defaults

    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except Exception as exc:
        log.warning("Could not read %s (%s); using defaults", path, exc)
        return defaults

    server = parser["server"] if parser.has_section("server") else {}
    training = parser["training"] if parser.has_section("training") else {}

    port = _positive_int(server.get("port", ""), defaults.port)
    if not 1 <= port <= 65535:
        log.warning("Invalid port %r; using %s", server.get("port", ""), defaults.port)
        port = defaults.port

    device = str(training.get("device", defaults.device) or defaults.device).strip().lower()
    if not _DEVICE_RE.match(device):
        log.warning("Invalid device %r; using auto", training.get("device", ""))
        device = "auto"

    # batch -1 is Ultralytics' auto batch size. 0 is not a useful batch.
    batch_raw = str(training.get("batch", "")).strip()
    if batch_raw == "-1":
        batch = -1
    else:
        batch = _positive_int(batch_raw, defaults.batch)

    return ServerSettings(
        port=port,
        epochs=_positive_int(training.get("epochs", ""), defaults.epochs),
        imgsz=_image_size(training.get("imgsz", ""), defaults.imgsz),
        batch=batch,
        patience=_positive_int(training.get("patience", ""), defaults.patience, allow_zero=True),
        workers=_positive_int(training.get("workers", ""), defaults.workers, allow_zero=True),
        device=device,
    )
