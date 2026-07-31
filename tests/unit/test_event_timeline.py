"""Event timeline append / format helpers."""

from src.event_timeline import (
    TimelineAction,
    timeline_append,
    timeline_format_message,
    timeline_format_time,
)


def test_timeline_append_adds_action_and_timestamp():
    entries = []
    timeline_append(entries, TimelineAction.MOTION_OUTSIDE, cat_name="Mia")
    assert len(entries) == 1
    assert entries[0]["action"] == TimelineAction.MOTION_OUTSIDE
    assert entries[0]["cat_name"] == "Mia"
    assert "at" in entries[0]


def test_timeline_append_skips_none_detail():
    entries = []
    timeline_append(entries, TimelineAction.ENTRY_ALLOWED, cat_name=None, source="RFID")
    assert "cat_name" not in entries[0]
    assert entries[0]["source"] == "RFID"


def test_timeline_format_time_utc():
    # Stored with +00:00 offset
    formatted = timeline_format_time("2024-06-01 12:34:56.00+00:00", "UTC")
    assert formatted == "12:34:56"


def test_timeline_format_message_entry_and_exit_actions():
    assert "Mia" in timeline_format_message(
        {"action": TimelineAction.ENTRY_ALLOWED, "cat_name": "Mia", "source": "RFID"}
    )
    assert timeline_format_message({"action": TimelineAction.PREY_DETECTED})
    assert timeline_format_message({"action": TimelineAction.OUTSIDE_OPENED})
    # Unknown action falls through safely
    msg = timeline_format_message({"action": "not_a_real_action"})
    assert isinstance(msg, str)
