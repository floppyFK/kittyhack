"""Fixtures for on-target hardware matrix tests (real Magnets GPIO)."""

from __future__ import annotations

from pathlib import Path

import pytest

import src.backend.loop as loop_mod
from src.camera import image_buffer
from tests.helpers.backend_loop_harness import build_hardware_loop_context


def _gpio_available() -> bool:
    return Path("/sys/class/gpio").exists() or Path(
        "/sys/devices/platform/soc/fe200000.gpio/gpiochip0"
    ).exists()


@pytest.fixture(scope="module", autouse=True)
def _require_real_gpio():
    """Skip the whole hardware module when no Pi GPIO sysfs is present."""
    if not _gpio_available():
        pytest.skip("no GPIO sysfs — run these tests on a Kittyflap")


@pytest.fixture(autouse=True)
def _short_loop_delays(monkeypatch):
    """Keep lazy-cat / cooldown delays tiny; magnet waits still use real time."""
    monkeypatch.setattr(loop_mod, "LAZY_CAT_DELAY_PIR_MOTION", 0.0)
    monkeypatch.setattr(loop_mod, "LAZY_CAT_DELAY_CAM_MOTION", 0.0)
    monkeypatch.setattr(loop_mod, "EVENT_COOLDOWN_SECONDS", 0.0)
    monkeypatch.setattr(loop_mod, "FAST_EXIT_POST_CAPTURE_SECONDS", 0.0)
    from src.helper import Result

    monkeypatch.setattr(
        "src.database.EventsRepo.write_motion_block_to_db",
        staticmethod(lambda *a, **k: Result(True, "")),
    )


@pytest.fixture
def loop_db(tmp_kittyhack_db, tmp_config_ini):
    """Temp config + kittyhack DB for hardware loop scenarios."""
    import src.baseconfig as baseconfig

    baseconfig.CONFIG["KITTYHACK_DATABASE_PATH"] = tmp_kittyhack_db
    return tmp_kittyhack_db


@pytest.fixture
def hw_harness(loop_db):
    """Real Magnets + FakePir/FakeRfid loop; always tear down locked."""
    h = build_hardware_loop_context(db_path=loop_db, cat_rfid="CAT001")
    try:
        yield h
    finally:
        try:
            h.shutdown()
        except Exception:
            pass
        image_buffer.clear()
