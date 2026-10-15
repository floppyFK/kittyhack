"""Regression: concurrent settings writes must not wipe other keys.

After a database restore the UI forces a reboot. During that reboot both
kittyhack.service and kittyhack_control.service write STARTUP_SHUTDOWN_FLAG.
Those writes share kittyhack.db and must not drop the rest of the settings.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sqlite3


def _toggle_shutdown_flag(db_path: str, iterations: int) -> None:
    """Child process: repeatedly update STARTUP_SHUTDOWN_FLAG like a service stop."""
    os.environ["KITTYHACK_DATABASE_PATH"] = db_path
    import src.baseconfig as baseconfig

    baseconfig.load_config()
    for i in range(iterations):
        baseconfig.CONFIG["STARTUP_SHUTDOWN_FLAG"] = bool(i % 2)
        baseconfig.update_single_config_parameter("STARTUP_SHUTDOWN_FLAG")


def _writer_elements_per_page(db_path: str, iterations: int) -> None:
    os.environ["KITTYHACK_DATABASE_PATH"] = db_path
    import src.baseconfig as baseconfig

    baseconfig.load_config()
    for i in range(iterations):
        baseconfig.CONFIG["ELEMENTS_PER_PAGE"] = 10 + (i % 5)
        baseconfig.update_single_config_parameter("ELEMENTS_PER_PAGE")


def _reader_reject_missing_settings(db_path: str, observed_bad, iterations: int) -> None:
    for _ in range(iterations):
        try:
            conn = sqlite3.connect(db_path, timeout=30)
            try:
                rows = dict(conn.execute("SELECT key, value FROM settings"))
            finally:
                conn.close()
        except Exception as exc:
            observed_bad.append(str(exc))
            return
        if "TIMEZONE" not in rows or "ELEMENTS_PER_PAGE" not in rows:
            observed_bad.append("missing keys")
            return
        try:
            json.loads(rows["ELEMENTS_PER_PAGE"])
            json.loads(rows["TIMEZONE"])
        except Exception as exc:
            observed_bad.append(str(exc))
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

    db_path = str(tmp_config_ini)
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_toggle_shutdown_flag, args=(db_path, 40)),
        ctx.Process(target=_toggle_shutdown_flag, args=(db_path, 40)),
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0, f"worker exited with {proc.exitcode}"

    baseconfig.CONFIG.clear()
    baseconfig.load_config()
    assert baseconfig.CONFIG["CAMERA_SOURCE"] == "ip_camera"
    assert baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] is True
    assert baseconfig.CONFIG["LIVE_VIEW_REFRESH_INTERVAL"] == 0.5
    assert baseconfig.CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"] is True
    assert baseconfig.CONFIG["LAST_BOOTED_VERSION"] == "4dda8cf"
    assert baseconfig.CONFIG["MQTT_DEVICE_ID"] == "kittyhack_keepme"


def test_concurrent_settings_writes_never_drop_rows(tmp_config_ini):
    """Readers must keep seeing a complete settings table during writes."""
    db_path = str(tmp_config_ini)
    ctx = mp.get_context("spawn")
    with ctx.Manager() as manager:
        observed_bad = manager.list()
        writer = ctx.Process(target=_writer_elements_per_page, args=(db_path, 80))
        reader = ctx.Process(
            target=_reader_reject_missing_settings,
            args=(db_path, observed_bad, 200),
        )
        writer.start()
        reader.start()
        writer.join(timeout=60)
        reader.join(timeout=60)
        assert writer.exitcode == 0
        assert reader.exitcode == 0
        assert list(observed_bad) == []
