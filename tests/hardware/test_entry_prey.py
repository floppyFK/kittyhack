"""Hardware matrix: prey / mouse-check branches on the real Magnets path."""

from __future__ import annotations

import time as tm

import pytest

import src.baseconfig as baseconfig
from src.baseconfig import AllowedToEnter
from src.camera import image_buffer
from tests.helpers.backend_loop_harness import build_hardware_loop_context

pytestmark = [pytest.mark.hardware, pytest.mark.timeout(0)]


def _assert_inside(harness, unlocked: bool, *, settle_s: float = 1.5) -> None:
    harness.run_for(settle_s)
    if unlocked:
        assert harness.wait_inside(True), "expected inside unlocked"
    else:
        harness.run_for(2.0)
        assert harness.magnets.get_inside_state() is False


@pytest.mark.parametrize("enter_mode", [AllowedToEnter.KNOWN, AllowedToEnter.ALL])
def test_prey_global_off_unlocks(hw_harness, enter_mode):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = enter_mode
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    hw_harness.set_outside(True)
    if enter_mode == AllowedToEnter.KNOWN:
        hw_harness.inject_rfid("CAT001")
    _assert_inside(hw_harness, True)


@pytest.mark.parametrize("enter_mode", [AllowedToEnter.KNOWN, AllowedToEnter.ALL])
def test_prey_ok_below_threshold_unlocks(hw_harness, enter_mode):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = enter_mode
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = True
    baseconfig.CONFIG["MOUSE_THRESHOLD"] = 70.0
    baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 0.5

    hw_harness.set_outside(True)
    if enter_mode == AllowedToEnter.KNOWN:
        hw_harness.inject_rfid("CAT001")
    hw_harness.pump(3)  # establish first_motion_outside_mono before frames
    # Below threshold — not counted as prey.
    hw_harness.inject_detection(mouse=40.0, no_mouse=80.0, timestamp_mono=tm.monotonic())
    hw_harness.run_for(0.7)
    _assert_inside(hw_harness, True)


@pytest.mark.parametrize("enter_mode", [AllowedToEnter.KNOWN, AllowedToEnter.ALL])
def test_prey_block_at_or_above_threshold(hw_harness, enter_mode):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = enter_mode
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = True
    baseconfig.CONFIG["MOUSE_THRESHOLD"] = 50.0
    baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 0.0

    hw_harness.set_outside(True)
    if enter_mode == AllowedToEnter.KNOWN:
        hw_harness.inject_rfid("CAT001")
    hw_harness.pump(3)
    hw_harness.inject_detection(mouse=90.0, no_mouse=10.0, timestamp_mono=tm.monotonic())
    _assert_inside(hw_harness, False)


def test_per_cat_prey_disabled_skips_check(loop_db):
    h = build_hardware_loop_context(
        db_path=loop_db,
        cat_rfid="CAT001",
        enable_prey_detection=False,
    )
    try:
        baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = True
        baseconfig.CONFIG["MOUSE_THRESHOLD"] = 50.0
        baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 0.0

        h.set_outside(True)
        h.inject_rfid("CAT001")
        h.pump(3)
        h.inject_detection(mouse=95.0, no_mouse=5.0, timestamp_mono=tm.monotonic())
        _assert_inside(h, True)
    finally:
        h.shutdown()


def test_post_prey_lockout_blocks_unlock(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = True
    baseconfig.CONFIG["MOUSE_THRESHOLD"] = 50.0
    baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 0.0
    # Short lockout so the suite stays practical while still exercising the gate.
    baseconfig.CONFIG["LOCK_DURATION_AFTER_PREY_DETECTION"] = 12

    hw_harness.set_outside(True)
    hw_harness.inject_rfid("CAT001")
    hw_harness.pump(3)
    hw_harness.inject_detection(mouse=95.0, no_mouse=5.0, timestamp_mono=tm.monotonic())
    _assert_inside(hw_harness, False)

    # Clear prey frames; lockout from prey_detection_mono must still block.
    image_buffer.clear()
    hw_harness.inject_detection(mouse=10.0, no_mouse=90.0, timestamp_mono=tm.monotonic())
    hw_harness.run_for(1.0)
    assert hw_harness.magnets.get_inside_state() is False

    # After lockout expires, unlock should succeed.
    hw_harness.run_for(13.0)
    image_buffer.clear()
    hw_harness.inject_detection(mouse=10.0, no_mouse=90.0, timestamp_mono=tm.monotonic())
    _assert_inside(hw_harness, True)
