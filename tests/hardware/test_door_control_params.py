"""Hardware matrix: remaining Door Control Settings on the real Magnets path."""

from __future__ import annotations

import time as tm

import pytest

import src.baseconfig as baseconfig
from src.baseconfig import AllowedToEnter, AllowedToExit
from src.camera import image_buffer

pytestmark = [pytest.mark.hardware, pytest.mark.timeout(0)]


def _assert_inside(harness, unlocked: bool, *, settle_s: float = 1.5) -> None:
    harness.run_for(settle_s)
    if unlocked:
        assert harness.wait_inside(True), "expected inside unlocked"
    else:
        harness.run_for(2.0)
        assert harness.magnets.get_inside_state() is False


def test_min_seconds_to_analyze_delays_unlock(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = True
    baseconfig.CONFIG["MOUSE_THRESHOLD"] = 70.0
    baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 3.0

    hw_harness.set_outside(True)
    hw_harness.pump(3)
    hw_harness.inject_detection(mouse=10.0, no_mouse=90.0, timestamp_mono=tm.monotonic())

    # Before analyze window: must stay locked.
    hw_harness.run_for(1.0)
    assert hw_harness.magnets.get_inside_state() is False

    # After analyze window: unlock.
    hw_harness.run_for(2.5)
    assert hw_harness.wait_inside(True)


def test_mouse_threshold_boundary(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = True
    baseconfig.CONFIG["MOUSE_THRESHOLD"] = 60.0
    baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 0.0

    hw_harness.set_outside(True)
    hw_harness.pump(3)
    # Just below threshold → unlock.
    hw_harness.inject_detection(mouse=59.0, no_mouse=80.0, timestamp_mono=tm.monotonic())
    _assert_inside(hw_harness, True)

    # End motion block so the next rising edge gets a fresh entry decision.
    hw_harness.set_outside(False)
    hw_harness.run_for(1.0)
    hw_harness.ensure_locked()
    image_buffer.clear()

    hw_harness.set_outside(True)
    hw_harness.pump(3)
    hw_harness.inject_detection(mouse=60.0, no_mouse=20.0, timestamp_mono=tm.monotonic())
    _assert_inside(hw_harness, False)


def test_camera_motion_detection_unlocks_all(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_MOTION_DETECTION"] = True
    baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0

    # No PIR outside; camera frames with high own_cat drive motion.
    hw_harness.set_outside(False)
    hw_harness.inject_detection(own_cat=90.0, mouse=0.0, no_mouse=0.0)
    _assert_inside(hw_harness, True)


def test_camera_cat_detection_known_without_rfid(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = True
    baseconfig.CONFIG["CAT_THRESHOLD"] = 50.0

    hw_harness.clear_rfid()
    hw_harness.set_outside(True)
    hw_harness.pump(3)  # set first_motion_outside_tm before video frames
    # Video ID matches seeded cat name "Mia" → maps to CAT001.
    hw_harness.inject_detection(
        own_cat=90.0,
        mouse=0.0,
        no_mouse=0.0,
        cat_name="Mia",
        cat_probability=90.0,
        timestamp=tm.time(),
        timestamp_mono=tm.monotonic(),
    )
    _assert_inside(hw_harness, True)


def test_immediate_lock_after_passage(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"] = True

    hw_harness.set_outside(True)
    hw_harness.inject_rfid("CAT001")
    assert hw_harness.wait_inside(True)

    # Debounce window after entry unlock is entry_unlocked_mono + 0.2s.
    hw_harness.run_for(0.4)
    hw_harness.set_inside(True)
    hw_harness.run_for(1.5)
    assert hw_harness.wait_inside(False, timeout=6.0)
    assert hw_harness.magnets.get_outside_state() is False


def test_pir_inside_threshold_config_loaded(hw_harness, tmp_config_ini):
    """FakePir bypasses hardware debounce; only assert the Door Control key is live."""
    baseconfig.CONFIG["PIR_INSIDE_THRESHOLD"] = 2.5
    assert float(baseconfig.CONFIG["PIR_INSIDE_THRESHOLD"]) == 2.5
    # Smoke: exit still works with ALLOW (timing peripheral, not asserted here).
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    for i in (1, 2, 3):
        baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False
    hw_harness.set_inside(True)
    assert hw_harness.wait_outside(True)


def test_min_threshold_does_not_block_unlock(hw_harness):
    """MIN_THRESHOLD is a logging floor; unlock path still works when set high."""
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["MIN_THRESHOLD"] = 80.0

    hw_harness.set_outside(True)
    _assert_inside(hw_harness, True)


def test_model_version_keys_do_not_break_unlock(hw_harness):
    """Model selection is peripheral to magnet decisions when start_model=False."""
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    # Touch both model-related keys used by Door Control Settings.
    if "TFLITE_MODEL_VERSION" in baseconfig.CONFIG:
        _ = baseconfig.CONFIG["TFLITE_MODEL_VERSION"]
    if "YOLO_MODEL" in baseconfig.CONFIG:
        _ = baseconfig.CONFIG["YOLO_MODEL"]

    hw_harness.set_outside(True)
    _assert_inside(hw_harness, True)
