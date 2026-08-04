"""Regression: concurrent config.ini writes must not wipe settings.

After a database restore the UI forces a reboot. During that reboot both
kittyhack.service and kittyhack_control.service write STARTUP_SHUTDOWN_FLAG.
Previously those used in-place open(..., "w") which truncates first; one process
could read a near-empty file and persist it, so the next boot silently loaded
all defaults (see _logs_config_reset_after_dbrestore).
"""

from __future__ import annotations

import configparser
import multiprocessing as mp
from pathlib import Path

import pytest

try:
    import fcntl
except Exception:
    fcntl = None

pytestmark = pytest.mark.skipif(
    fcntl is None,
    reason="fcntl flock required (Linux target runtime)",
)


def _toggle_shutdown_flag(config_path: str, iterations: int) -> None:
    """Child process: repeatedly update STARTUP_SHUTDOWN_FLAG like a service stop."""
    import src.baseconfig as baseconfig

    baseconfig.CONFIGFILE = config_path
    baseconfig.load_config()
    for i in range(iterations):
        baseconfig.CONFIG["STARTUP_SHUTDOWN_FLAG"] = bool(i % 2)
        baseconfig.update_single_config_parameter("STARTUP_SHUTDOWN_FLAG")


def _writer_elements_per_page(config_path: str, iterations: int) -> None:
    import src.baseconfig as baseconfig

    baseconfig.CONFIGFILE = config_path
    baseconfig.load_config()
    for i in range(iterations):
        baseconfig.CONFIG["ELEMENTS_PER_PAGE"] = 10 + (i % 5)
        baseconfig.update_single_config_parameter("ELEMENTS_PER_PAGE")


def _reader_reject_empty_or_corrupt(config_path: str, observed_bad, iterations: int) -> None:
    for _ in range(iterations):
        try:
            raw = Path(config_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if not raw.strip() or "[Settings]" not in raw:
            observed_bad.append(raw)
            return
        parser = configparser.ConfigParser()
        try:
            parser.read_string(raw)
        except Exception:
            observed_bad.append(raw)
            return
        if not parser.has_section("Settings"):
            observed_bad.append(raw)
            return


def test_concurrent_shutdown_flag_writes_preserve_settings(tmp_config_ini):
    import src.baseconfig as baseconfig

    # Non-default values that must survive the reboot-style write race.
    baseconfig.CONFIG["CAMERA_SOURCE"] = "ip_camera"
    baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
    baseconfig.CONFIG["LIVE_VIEW_REFRESH_INTERVAL"] = 0.5
    baseconfig.CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"] = True
    baseconfig.CONFIG["LAST_BOOTED_VERSION"] = "4dda8cf"
    baseconfig.CONFIG["MQTT_DEVICE_ID"] = "kittyhack_keepme"
    assert baseconfig.save_config()

    cfg_path = str(tmp_config_ini)
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_toggle_shutdown_flag, args=(cfg_path, 40)),
        ctx.Process(target=_toggle_shutdown_flag, args=(cfg_path, 40)),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker exited with {p.exitcode}"

    # File must still be a full Settings section, not a single-key remnant.
    text = Path(cfg_path).read_text(encoding="utf-8")
    assert "[Settings]" in text
    assert "camera_source" in text.lower()

    baseconfig.CONFIG.clear()
    baseconfig.load_config()
    assert baseconfig.CONFIG["CAMERA_SOURCE"] == "ip_camera"
    assert baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] is True
    assert baseconfig.CONFIG["LIVE_VIEW_REFRESH_INTERVAL"] == 0.5
    assert baseconfig.CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"] is True
    assert baseconfig.CONFIG["LAST_BOOTED_VERSION"] == "4dda8cf"
    assert baseconfig.CONFIG["MQTT_DEVICE_ID"] == "kittyhack_keepme"


def test_atomic_config_write_never_exposes_empty_file(tmp_config_ini):
    """Readers must not observe a truncated config.ini mid-write."""
    cfg_path = str(tmp_config_ini)
    ctx = mp.get_context("spawn")
    with ctx.Manager() as manager:
        observed_bad = manager.list()
        w = ctx.Process(target=_writer_elements_per_page, args=(cfg_path, 80))
        r = ctx.Process(
            target=_reader_reject_empty_or_corrupt,
            args=(cfg_path, observed_bad, 200),
        )
        w.start()
        r.start()
        w.join(timeout=60)
        r.join(timeout=60)
        assert w.exitcode == 0
        assert r.exitcode == 0
        assert list(observed_bad) == []
