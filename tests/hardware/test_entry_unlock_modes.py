"""Hardware matrix: ALLOWED_TO_ENTER modes (real Magnets, injected PIR/RFID)."""

from __future__ import annotations

import pytest

import src.baseconfig as baseconfig
from src.baseconfig import AllowedToEnter
from tests.helpers.backend_loop_harness import build_hardware_loop_context

pytestmark = [pytest.mark.hardware, pytest.mark.timeout(0)]


def _assert_inside(harness, unlocked: bool, *, settle_s: float = 1.5) -> None:
    harness.run_for(settle_s)
    if unlocked:
        assert harness.wait_inside(True), "expected inside unlocked"
    else:
        # Give the magnet queue time to act if an unlock had been queued.
        harness.run_for(2.0)
        assert harness.magnets.get_inside_state() is False


def test_entry_all_motion_unlocks(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    hw_harness.set_outside(True)
    _assert_inside(hw_harness, True)


def test_entry_all_no_motion_stays_locked(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    hw_harness.set_outside(False)
    _assert_inside(hw_harness, False)


def test_entry_all_rfids_with_tag_unlocks(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL_RFIDS
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    hw_harness.set_outside(True)
    hw_harness.inject_rfid("ANYTAG99")
    _assert_inside(hw_harness, True)


def test_entry_all_rfids_without_tag_stays_locked(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.ALL_RFIDS
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    hw_harness.set_outside(True)
    hw_harness.clear_rfid()
    _assert_inside(hw_harness, False)


def test_entry_known_registered_unlocks(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    hw_harness.set_outside(True)
    hw_harness.inject_rfid("CAT001")
    _assert_inside(hw_harness, True)


def test_entry_known_stranger_stays_locked(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.KNOWN
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    hw_harness.set_outside(True)
    hw_harness.inject_rfid("STRANGER")
    _assert_inside(hw_harness, False)


def test_entry_none_stays_locked(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.NONE
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

    hw_harness.set_outside(True)
    hw_harness.inject_rfid("CAT001")
    _assert_inside(hw_harness, False)


def test_entry_per_cat_allow_unlocks(loop_db):
    h = build_hardware_loop_context(
        db_path=loop_db, cat_rfid="CAT001", allow_entry=True
    )
    try:
        baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.CONFIGURE_PER_CAT
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

        h.set_outside(True)
        h.inject_rfid("CAT001")
        _assert_inside(h, True)
    finally:
        h.shutdown()


def test_entry_per_cat_deny_stays_locked(loop_db):
    h = build_hardware_loop_context(
        db_path=loop_db, cat_rfid="CAT001", allow_entry=False
    )
    try:
        baseconfig.CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter.CONFIGURE_PER_CAT
        baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False

        h.set_outside(True)
        h.inject_rfid("CAT001")
        _assert_inside(h, False)
    finally:
        h.shutdown()
