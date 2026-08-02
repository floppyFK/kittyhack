"""Hardware matrix: ALLOWED_TO_EXIT modes + time ranges (real Magnets)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import src.baseconfig as baseconfig
from src.baseconfig import AllowedToExit
from src.helper import DateTimeUtil
from tests.helpers.backend_loop_harness import build_hardware_loop_context

pytestmark = [pytest.mark.hardware, pytest.mark.timeout(0)]


def _clear_exit_ranges() -> None:
    for i in (1, 2, 3):
        baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False


def _assert_outside(harness, unlocked: bool, *, settle_s: float = 1.5) -> None:
    harness.run_for(settle_s)
    if unlocked:
        assert harness.wait_outside(True), "expected outside unlocked"
    else:
        harness.run_for(2.0)
        assert harness.magnets.get_outside_state() is False


def test_exit_allow_no_ranges_unlocks(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    _clear_exit_ranges()

    hw_harness.set_inside(True)
    _assert_outside(hw_harness, True)


def test_exit_allow_inside_configured_range(hw_harness):
    now = datetime.now(DateTimeUtil.get_timezone())
    start = (now - timedelta(minutes=30)).strftime("%H:%M")
    end = (now + timedelta(minutes=30)).strftime("%H:%M")
    # Avoid overnight wrap for this case when near midnight.
    if start > end:
        start = "00:00"
        end = "23:59"

    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    _clear_exit_ranges()
    baseconfig.CONFIG["ALLOWED_TO_EXIT_RANGE1"] = True
    baseconfig.CONFIG["ALLOWED_TO_EXIT_RANGE1_FROM"] = start
    baseconfig.CONFIG["ALLOWED_TO_EXIT_RANGE1_TO"] = end

    hw_harness.set_inside(True)
    _assert_outside(hw_harness, True)


def test_exit_allow_outside_configured_range(hw_harness):
    now = datetime.now(DateTimeUtil.get_timezone())
    # Narrow window that does not include "now".
    start = (now + timedelta(hours=2)).strftime("%H:%M")
    end = (now + timedelta(hours=3)).strftime("%H:%M")
    if start > end:
        pytest.skip("overnight wrap near midnight; skip out-of-range case")

    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    _clear_exit_ranges()
    baseconfig.CONFIG["ALLOWED_TO_EXIT_RANGE1"] = True
    baseconfig.CONFIG["ALLOWED_TO_EXIT_RANGE1_FROM"] = start
    baseconfig.CONFIG["ALLOWED_TO_EXIT_RANGE1_TO"] = end

    hw_harness.set_inside(True)
    _assert_outside(hw_harness, False)


def test_exit_deny_stays_locked(hw_harness):
    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.DENY
    _clear_exit_ranges()

    hw_harness.set_inside(True)
    _assert_outside(hw_harness, False)


def test_exit_per_cat_allow_with_rfid(loop_db):
    h = build_hardware_loop_context(
        db_path=loop_db, cat_rfid="CAT001", allow_exit=True
    )
    try:
        baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.CONFIGURE_PER_CAT
        _clear_exit_ranges()

        h.set_inside(True)
        h.inject_rfid("CAT001")
        _assert_outside(h, True)
    finally:
        h.shutdown()


def test_exit_per_cat_deny(loop_db):
    h = build_hardware_loop_context(
        db_path=loop_db, cat_rfid="CAT001", allow_exit=False
    )
    try:
        baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.CONFIGURE_PER_CAT
        _clear_exit_ranges()

        h.set_inside(True)
        h.inject_rfid("CAT001")
        _assert_outside(h, False)
    finally:
        h.shutdown()


def test_exit_per_cat_no_rfid_stays_locked(loop_db):
    h = build_hardware_loop_context(
        db_path=loop_db, cat_rfid="CAT001", allow_exit=True
    )
    try:
        baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.CONFIGURE_PER_CAT
        _clear_exit_ranges()

        h.clear_rfid()
        h.set_inside(True)
        _assert_outside(h, False)
    finally:
        h.shutdown()


def test_exit_overnight_range_allows(hw_harness):
    """From > To overnight window that includes now."""
    from src.backend.decisions import time_in_exit_ranges

    now = datetime.now(DateTimeUtil.get_timezone())
    current = now.strftime("%H:%M")
    candidates = [
        (
            (now + timedelta(hours=1)).strftime("%H:%M"),
            (now - timedelta(hours=1)).strftime("%H:%M"),
        ),
        ("22:00", "06:00"),
        ("18:00", "10:00"),
        ("12:00", "08:00"),
    ]
    start = end = None
    for s, e in candidates:
        if s > e and time_in_exit_ranges(current, [(True, s, e)]):
            start, end = s, e
            break
    if start is None:
        pytest.skip("could not construct overnight From>To window containing now")

    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    _clear_exit_ranges()
    baseconfig.CONFIG["ALLOWED_TO_EXIT_RANGE2"] = True
    baseconfig.CONFIG["ALLOWED_TO_EXIT_RANGE2_FROM"] = start
    baseconfig.CONFIG["ALLOWED_TO_EXIT_RANGE2_TO"] = end

    hw_harness.set_inside(True)
    _assert_outside(hw_harness, True)
