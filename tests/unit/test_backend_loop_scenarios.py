"""Scenario tests for the pumpable backend control loop."""

from __future__ import annotations

import pytest

import src.baseconfig as baseconfig
from src.baseconfig import AllowedToEnter, AllowedToExit
from src.camera import image_buffer
from src.helper import Result
import src.backend.loop as loop_mod
from tests.helpers.backend_loop_harness import build_loop_context


@pytest.fixture
def loop_db(tmp_kittyhack_db, tmp_config_ini):
    """Temp config + kittyhack DB for loop scenarios."""
    baseconfig.CONFIG["KITTYHACK_DATABASE_PATH"] = tmp_kittyhack_db
    return tmp_kittyhack_db


@pytest.fixture(autouse=True)
def _short_loop_delays(monkeypatch):
    """Keep lazy-cat / cooldown delays tiny; scenarios still advance FakeClock."""
    monkeypatch.setattr(loop_mod, "LAZY_CAT_DELAY_PIR_MOTION", 0.0)
    monkeypatch.setattr(loop_mod, "LAZY_CAT_DELAY_CAM_MOTION", 0.0)
    monkeypatch.setattr(loop_mod, "EVENT_COOLDOWN_SECONDS", 0.0)
    monkeypatch.setattr(loop_mod, "FAST_EXIT_POST_CAPTURE_SECONDS", 0.0)
    monkeypatch.setattr(
        "src.database.EventsRepo.write_motion_block_to_db",
        staticmethod(lambda *a, **k: Result(True, "")),
    )


@pytest.fixture
def harness(loop_db):
    h = build_loop_context(db_path=loop_db, cat_rfid="CAT001")
    yield h
    h.shutdown()
    image_buffer.clear()


def test_entry_allow_known_rfid(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.inject_rfid("CAT001")
    harness.advance(2.0)
    harness.pump(5)

    assert harness.magnets.get_inside_state() is True


def test_entry_deny_unknown_rfid(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.inject_rfid("STRANGER")
    harness.advance(2.0)
    harness.pump(5)

    assert harness.magnets.get_inside_state() is False


def test_entry_deny_none_mode(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.NONE
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.inject_rfid("CAT001")
    harness.advance(2.0)
    harness.pump(5)

    assert harness.magnets.get_inside_state() is False


def test_exit_allow_unlocks_outside(harness):
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    for i in (1, 2, 3):
        baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

    harness.set_inside(True)
    harness.pump(3)

    assert harness.magnets.get_outside_state() is True


def test_prey_blocks_entry_unlock(harness, monkeypatch):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = True
    baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 0.0
    baseconfig.CONFIG["MOUSE_THRESHOLD"] = 50.0

    monkeypatch.setattr(
        image_buffer,
        "get_filtered_ids_mono",
        lambda **kwargs: [1] if kwargs.get("min_mouse_probability") else [1],
    )
    monkeypatch.setattr(image_buffer, "size", lambda: 1)

    harness.set_outside(True)
    harness.inject_rfid("CAT001")
    harness.advance(2.0)
    harness.pump(5)

    assert harness.magnets.get_inside_state() is False


def test_manual_override_unlock_inside(harness):
    harness.reset_manual_override()
    assert harness.magnets.get_inside_state() is False

    harness.set_manual_override(unlock_inside=True)
    harness.pump(2)

    assert harness.magnets.get_inside_state() is True


def test_fast_crossing_locks_after_entry(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"] = True

    harness.set_outside(True)
    harness.inject_rfid("CAT001")
    harness.advance(2.0)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True

    # Debounce window after entry unlock is entry_unlocked_mono + 0.2s.
    harness.advance(0.3)
    harness.set_inside(True)
    harness.pump(3)

    assert harness.magnets.get_inside_state() is False
    assert harness.magnets.get_outside_state() is False
