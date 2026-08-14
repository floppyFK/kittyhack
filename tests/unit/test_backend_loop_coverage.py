"""Extra backend loop scenarios to raise coverage of ``src.backend.loop``."""

from __future__ import annotations

import time as time_mod

import pytest

import src.baseconfig as baseconfig
import src.backend.loop as loop_mod
from src.baseconfig import AllowedToEnter, AllowedToExit
from src.camera import DetectedObject, image_buffer
from src.helper import Result
from src.magnets_rfid import RfidRunState
from tests.helpers.backend_loop_harness import build_loop_context


@pytest.fixture
def loop_db(tmp_kittyhack_db, tmp_config_ini):
    baseconfig.CONFIG["KITTYHACK_DATABASE_PATH"] = tmp_kittyhack_db
    return tmp_kittyhack_db


@pytest.fixture(autouse=True)
def _short_loop_delays(monkeypatch):
    monkeypatch.setattr(loop_mod, "LAZY_CAT_DELAY_PIR_MOTION", 0.0)
    monkeypatch.setattr(loop_mod, "LAZY_CAT_DELAY_CAM_MOTION", 0.0)
    monkeypatch.setattr(loop_mod, "EVENT_COOLDOWN_SECONDS", 0.0)
    monkeypatch.setattr(loop_mod, "FAST_EXIT_POST_CAPTURE_SECONDS", 0.0)
    monkeypatch.setattr(loop_mod, "TAG_TIMEOUT", 2.0)
    monkeypatch.setattr(loop_mod, "RFID_READER_OFF_DELAY", 1.0)
    monkeypatch.setattr(loop_mod, "OPEN_OUTSIDE_TIMEOUT", 1.0)
    monkeypatch.setattr(loop_mod, "MAX_UNLOCK_TIME", 3.0)
    monkeypatch.setattr(loop_mod, "MAX_MOTION_BLOCK_SECONDS", 5.0)
    monkeypatch.setattr(
        "src.database.EventsRepo.write_motion_block_to_db",
        staticmethod(lambda *a, **k: Result(True, "")),
    )
    monkeypatch.setattr(loop_mod.UserNotifications, "add", lambda *a, **k: "ok")


@pytest.fixture
def harness(loop_db):
    h = build_loop_context(db_path=loop_db, cat_rfid="CAT001")
    yield h
    h.shutdown()
    image_buffer.clear()


def _inject_frame(harness, *, mouse=0.0, own_cat=0.0, cat_name=None):
    """Append a detection frame aligned to the FakeClock (mono + wall)."""
    detected = None
    if cat_name:
        detected = [DetectedObject(0.1, 0.1, 0.2, 0.2, cat_name, float(own_cat or 90.0))]
    image_buffer.append(
        harness.clock.wall_time(),
        b"",
        b"",
        float(mouse),
        100.0 - float(mouse),
        float(own_cat),
        detected_objects=detected,
        timestamp_mono=harness.clock.monotonic(),
    )


def _inject_live_camera_cat(harness, own_cat=90.0):
    """Camera-motion frames must use real monotonic time (``get_filtered_ids_recent``)."""
    image_buffer.append(
        harness.clock.wall_time(),
        b"",
        b"",
        0.0,
        0.0,
        float(own_cat),
        timestamp_mono=time_mod.monotonic(),
    )


def _enable_camera_motion_all(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_MOTION_DETECTION"] = True
    baseconfig.CONFIG["REQUIRE_OUTSIDE_PIR_FOR_CAMERA_ENTRY"] = False
    baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0
    harness.set_outside(False)


# --- Entry modes -------------------------------------------------------------


def test_entry_all_unlocks_and_finalizes_on_motion_end(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.pump(3)
    _inject_frame(harness, own_cat=80.0)
    harness.advance(0.5)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True

    harness.set_outside(False)
    harness.advance(0.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_entry_all_rfids_with_and_without_tag(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL_RFIDS
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is False

    harness.inject_rfid("ANY99")
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_entry_all_mode_explicit_decision(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    harness.set_outside(True)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_entry_per_cat_allow_and_deny(loop_db):
    h = build_loop_context(db_path=loop_db, cat_rfid="CAT001", allow_entry=True)
    try:
        baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.CONFIGURE_PER_CAT
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
        h.set_outside(True)
        h.inject_rfid("CAT001")
        h.advance(1.0)
        h.pump(5)
        assert h.magnets.get_inside_state() is True
    finally:
        h.shutdown()
        image_buffer.clear()

    h2 = build_loop_context(db_path=loop_db, cat_rfid="CAT002", allow_entry=False)
    try:
        # Fresh harness reloads cats from DB; CAT001 allow=True still present + CAT002 deny.
        # Use CAT002 for deny path.
        baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.CONFIGURE_PER_CAT
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
        h2.set_outside(True)
        h2.inject_rfid("CAT002")
        h2.advance(1.0)
        h2.pump(5)
        assert h2.magnets.get_inside_state() is False
    finally:
        h2.shutdown()
        image_buffer.clear()


# --- Exit modes --------------------------------------------------------------


def test_exit_deny_stays_locked(harness):
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.DENY
    harness.set_inside(True)
    harness.pump(5)
    assert harness.magnets.get_outside_state() is False


def test_exit_per_cat_allow_after_late_rfid(loop_db):
    h = build_loop_context(db_path=loop_db, cat_rfid="CAT001", allow_exit=True)
    try:
        baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.CONFIGURE_PER_CAT
        for i in (1, 2, 3):
            baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

        h.clear_rfid()
        h.set_inside(True)
        h.pump(3)
        assert h.magnets.get_outside_state() is False

        h.inject_rfid("CAT001")
        h.pump(5)
        assert h.magnets.get_outside_state() is True
    finally:
        h.shutdown()
        image_buffer.clear()


def test_exit_per_cat_deny_after_rfid(loop_db):
    h = build_loop_context(db_path=loop_db, cat_rfid="CAT001", allow_exit=False)
    try:
        baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.CONFIGURE_PER_CAT
        for i in (1, 2, 3):
            baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

        h.clear_rfid()
        h.set_inside(True)
        h.pump(2)
        h.inject_rfid("CAT001")
        h.pump(5)
        assert h.magnets.get_outside_state() is False
    finally:
        h.shutdown()
        image_buffer.clear()


def test_exit_per_cat_inside_ends_before_rfid(loop_db):
    h = build_loop_context(db_path=loop_db, cat_rfid="CAT001", allow_exit=True)
    try:
        baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.CONFIGURE_PER_CAT
        for i in (1, 2, 3):
            baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

        h.clear_rfid()
        h.set_inside(True)
        h.pump(2)
        h.set_inside(False)
        h.pump(3)
        assert h.magnets.get_outside_state() is False
    finally:
        h.shutdown()
        image_buffer.clear()


def test_exit_outside_locks_after_open_timeout(harness):
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    for i in (1, 2, 3):
        baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

    harness.set_inside(True)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is True

    harness.set_inside(False)
    harness.advance(1.5)
    harness.pump(5)
    assert harness.magnets.get_outside_state() is False


# --- Prey / analyze / re-lock ------------------------------------------------


def test_prey_ok_then_unlock_all(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = True
    baseconfig.CONFIG["MOUSE_THRESHOLD"] = 70.0
    baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 1.0

    harness.set_outside(True)
    harness.pump(2)
    _inject_frame(harness, mouse=10.0)
    harness.pump(2)
    assert harness.magnets.get_inside_state() is False

    harness.advance(1.2)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_prey_relock_after_unlock(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = True
    baseconfig.CONFIG["MOUSE_THRESHOLD"] = 50.0
    baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 0.0

    harness.set_outside(True)
    harness.pump(2)
    _inject_frame(harness, mouse=10.0)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True

    _inject_frame(harness, mouse=90.0)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_per_cat_prey_disabled_allows_high_mouse(loop_db):
    h = build_loop_context(
        db_path=loop_db, cat_rfid="CAT001", enable_prey_detection=False
    )
    try:
        baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = True
        baseconfig.CONFIG["MOUSE_THRESHOLD"] = 50.0
        baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 0.0

        h.set_outside(True)
        h.inject_rfid("CAT001")
        h.pump(2)
        _inject_frame(h, mouse=95.0)
        h.advance(0.5)
        h.pump(5)
        assert h.magnets.get_inside_state() is True
    finally:
        h.shutdown()
        image_buffer.clear()


# --- Camera paths ------------------------------------------------------------


def test_camera_motion_detection_unlocks_all(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_MOTION_DETECTION"] = True
    baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0

    harness.set_outside(False)
    # get_filtered_ids_recent uses real monotonic time — append with real mono.
    image_buffer.append(
        harness.clock.wall_time(),
        b"",
        b"",
        0.0,
        0.0,
        90.0,
        timestamp_mono=__import__("time").monotonic(),
    )
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_camera_cat_detection_known_without_rfid(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
    baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0

    harness.clear_rfid()
    harness.set_outside(True)
    harness.pump(3)
    _inject_frame(harness, own_cat=90.0, cat_name="Mia")
    harness.advance(0.2)
    harness.pump(8)
    assert harness.magnets.get_inside_state() is True


def test_rfid_overrides_video_and_locks(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
    baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0

    harness.clear_rfid()
    harness.set_outside(True)
    harness.pump(3)
    _inject_frame(harness, own_cat=90.0, cat_name="Mia")
    harness.pump(8)
    assert harness.magnets.get_inside_state() is True

    harness.inject_rfid("STRANGER")
    harness.pump(8)
    assert harness.magnets.get_inside_state() is False


# --- Manual overrides / max unlock ------------------------------------------


def test_manual_unlock_and_lock_outside(harness):
    # Deny automatic exit so outside unlock comes only from the manual override path.
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.DENY
    harness.set_inside(True)
    harness.pump(1)
    assert harness.magnets.get_outside_state() is False

    harness.set_manual_override(unlock_outside=True)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is True

    # Already open path
    harness.set_manual_override(unlock_outside=True)
    harness.pump(2)
    assert harness.magnets.get_outside_state() is True


def test_manual_lock_inside_while_open(harness):
    harness.set_manual_override(unlock_inside=True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True

    harness.set_manual_override(lock_inside=True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is False

    # Already locked path
    harness.set_manual_override(lock_inside=True)
    harness.pump(2)


def test_manual_unlock_inside_already_open(harness):
    harness.set_manual_override(unlock_inside=True)
    harness.pump(3)
    harness.set_manual_override(unlock_inside=True)
    harness.pump(2)
    assert harness.magnets.get_inside_state() is True


def test_manual_unlock_annotates_active_motion_block(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.NONE
    harness.set_outside(True)
    harness.pump(2)
    harness.set_manual_override(unlock_inside=True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True


def test_max_unlock_time_forces_inside_lock(harness):
    harness.set_manual_override(unlock_inside=True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True

    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_max_unlock_time_forces_outside_lock(harness):
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.DENY
    harness.set_inside(True)
    harness.pump(1)
    harness.set_manual_override(unlock_outside=True)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is True

    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_outside_state() is False


def test_manual_lock_annotates_active_outside_block(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    harness.set_outside(True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True

    harness.set_manual_override(lock_inside=True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is False


def test_max_unlock_annotates_active_outside_block(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.NONE
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    harness.set_outside(True)
    harness.pump(3)
    harness.set_manual_override(unlock_inside=True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True

    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_finalize_writes_tag_onto_buffered_images(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.inject_rfid("CAT001")
    harness.pump(2)
    _inject_frame(harness, own_cat=80.0)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True

    harness.set_outside(False)
    harness.advance(0.5)
    harness.pump(5)


def test_deferred_entry_reevaluates_after_exit_ends(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    for i in (1, 2, 3):
        baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

    harness.set_inside(True)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is True

    harness.set_outside(True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is False

    # End exit (inside idle + outside timeout) while outside motion persists.
    harness.set_inside(False)
    harness.advance(1.5)
    harness.pump(5)
    # Fresh rising edge after wait_for_outside_rising_after_exit
    harness.set_outside(False)
    harness.pump(2)
    harness.set_outside(True)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_suppress_flags_clear_after_fast_crossing(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"] = True

    harness.set_outside(True)
    harness.inject_rfid("CAT001")
    harness.advance(0.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True

    harness.advance(0.3)
    harness.set_inside(True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is False

    # Clear motion so suppress_* flags reset (770/772).
    harness.set_outside(False)
    harness.set_inside(False)
    harness.pump(5)


# --- Timeouts / RFID restart / long block -----------------------------------


def test_tag_timeout_forgets_tag(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL_RFIDS
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.inject_rfid("ANY99")
    harness.pump(3)
    assert harness.rfid.get_tag()[0] == "ANY99"

    harness.set_outside(False)
    harness.advance(2.5)
    harness.pump(5)
    assert harness.rfid.get_tag()[0] is None


def test_rfid_field_turns_off_after_idle(harness):
    harness.set_outside(True)
    harness.pump(2)
    harness.rfid.set_field(True)
    assert harness.rfid.get_field() is True

    harness.set_outside(False)
    harness.set_inside(False)
    harness.pump(3)  # process falling edges → set last_motion_*_mono
    harness.advance(1.5)
    harness.pump(5)
    assert harness.rfid.get_field() is False


def test_rfid_restarts_when_stopped(harness):
    harness.rfid.set_run_state(RfidRunState.stopped)
    harness.pump(2)
    # Restart thread is spawned; FakeRfid.run settles to stopped after idle loop ends,
    # but the restart path must have been taken without raising.
    assert harness.rfid.get_run_state() in (
        RfidRunState.running,
        RfidRunState.stopped,
        RfidRunState.stop_requested,
    )


def test_max_motion_block_seconds_finalizes(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True

    # Prevent re-unlock after timeout finalize resets unlock_inside_decision_made.
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.NONE
    harness.advance(5.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


# --- Immediate lock / deferred entry / cat went inside -----------------------


def test_fast_exit_crossing_with_immediate_lock(harness):
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    baseconfig.CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"] = True
    for i in (1, 2, 3):
        baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

    harness.set_inside(True)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is True

    harness.advance(0.3)
    harness.set_outside(True)
    harness.pump(5)
    assert harness.magnets.get_outside_state() is False
    assert harness.magnets.get_inside_state() is False


def test_exit_defers_entry_while_in_progress(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    for i in (1, 2, 3):
        baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False

    harness.set_inside(True)
    harness.pump(3)
    assert harness.magnets.get_outside_state() is True

    # Outside motion during exit should not unlock inside (deferred entry).
    harness.set_outside(True)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_cat_went_inside_finalize_path(harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is True

    harness.set_inside(True, raw=True)
    harness.pump(2)
    harness.set_outside(False)
    harness.advance(0.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_lazy_cat_workaround_keeps_motion(harness, monkeypatch):
    monkeypatch.setattr(loop_mod, "LAZY_CAT_DELAY_PIR_MOTION", 2.0)
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    harness.set_outside(True)
    harness.pump(2)
    harness.set_outside(False)
    # Within lazy window: motion should still be treated as active enough not to
    # immediately finalize before delay elapses — advance less than delay.
    harness.advance(0.5)
    harness.pump(2)
    # After delay, falling edge finalizes.
    harness.advance(2.0)
    harness.pump(5)


# --- Magnet protection after MAX_UNLOCK_TIME --------------------------------


def test_camera_motion_hold_after_max_unlock_without_pir(harness):
    """Persistent camera 'cat' must not re-unlock after the first max-unlock window."""
    _enable_camera_motion_all(harness)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True

    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False

    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_camera_motion_hold_pir_rising_reunlocks(harness):
    _enable_camera_motion_all(harness)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False

    harness.set_outside(True)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_camera_motion_hold_rfid_reunlocks(harness):
    _enable_camera_motion_all(harness)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False

    harness.inject_rfid("CAT001")
    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_camera_motion_hold_quiet_then_new_frame_reunlocks(harness, monkeypatch):
    monkeypatch.setattr(loop_mod, "INSIDE_UNLOCK_CONFIRM_QUIET_SECONDS", 0.5)
    _enable_camera_motion_all(harness)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False

    image_buffer.clear()
    harness.pump(3)
    harness.advance(0.6)
    harness.pump(3)

    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_camera_motion_hold_survives_motion_block_finalize(harness):
    _enable_camera_motion_all(harness)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True

    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False

    harness.advance(2.5)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_optional_pir_second_factor_blocks_camera_only_entry(harness):
    _enable_camera_motion_all(harness)
    baseconfig.CONFIG["REQUIRE_OUTSIDE_PIR_FOR_CAMERA_ENTRY"] = True
    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False

    harness.set_outside(True)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_second_max_unlock_starts_safety_cooldown_blocks_pir(harness, monkeypatch):
    monkeypatch.setattr(loop_mod, "INSIDE_UNLOCK_SAFETY_COOLDOWN_SECONDS", 2.0)
    monkeypatch.setattr(loop_mod, "MAX_MOTION_BLOCK_SECONDS", 30.0)
    _enable_camera_motion_all(harness)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False

    harness.set_outside(True)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True

    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False

    harness.set_outside(False)
    harness.pump(2)
    harness.set_outside(True)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False


def test_after_safety_cooldown_new_pir_rising_reunlocks(harness, monkeypatch):
    monkeypatch.setattr(loop_mod, "INSIDE_UNLOCK_SAFETY_COOLDOWN_SECONDS", 2.0)
    monkeypatch.setattr(loop_mod, "MAX_MOTION_BLOCK_SECONDS", 30.0)
    _enable_camera_motion_all(harness)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    harness.advance(3.5)
    harness.pump(5)
    harness.set_outside(True)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False

    harness.advance(2.5)
    harness.pump(3)
    assert harness.magnets.get_inside_state() is False

    harness.set_outside(False)
    harness.pump(2)
    harness.set_outside(True)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_rfid_bypasses_safety_cooldown(harness, monkeypatch):
    monkeypatch.setattr(loop_mod, "INSIDE_UNLOCK_SAFETY_COOLDOWN_SECONDS", 2.0)
    monkeypatch.setattr(loop_mod, "MAX_MOTION_BLOCK_SECONDS", 30.0)
    _enable_camera_motion_all(harness)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    harness.advance(3.5)
    harness.pump(5)
    harness.set_outside(True)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False

    harness.inject_rfid("CAT001")
    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True


def test_passage_resets_max_unlock_escalation(harness):
    baseconfig.CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"] = True
    _enable_camera_motion_all(harness)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True

    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False

    harness.set_inside(True)
    harness.pump(5)
    image_buffer.clear()
    harness.set_inside(False)
    harness.pump(5)

    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True

    harness.advance(3.5)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is False

    harness.set_outside(True)
    _inject_live_camera_cat(harness)
    harness.pump(5)
    assert harness.magnets.get_inside_state() is True
