"""Event timeline append / format helpers."""

from src.event_timeline import (
    TimelineAction,
    timeline_append,
    timeline_door_markers,
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


def test_timeline_door_markers_positions_and_filters_actions():
    frames = [
        "2024-06-01 12:00:00+00:00",
        "2024-06-01 12:00:05+00:00",
        "2024-06-01 12:00:10+00:00",
    ]
    entries = [
        {"action": TimelineAction.INSIDE_OPENED, "at": "2024-06-01 11:59:58+00:00"},
        {"action": TimelineAction.PREY_DETECTED, "at": "2024-06-01 12:00:05+00:00"},
        {"action": TimelineAction.OUTSIDE_CLOSED, "at": "2024-06-01 12:00:05+00:00"},
        {"action": TimelineAction.INSIDE_CLOSED_MAX_TIME, "at": "2024-06-01 12:00:12+00:00"},
    ]

    markers = timeline_door_markers(entries, frames)

    assert [marker["kind"] for marker in markers] == [
        "inside-open",
        "outside-close",
        "inside-close",
    ]
    assert [marker["position"] for marker in markers] == [0.0, 0.5, 1.0]
    assert [marker["frame"] for marker in markers] == [0, 1, 2]
    assert all(marker["label"] for marker in markers)


def test_timeline_door_markers_handles_single_frame():
    markers = timeline_door_markers(
        [{"action": TimelineAction.OUTSIDE_OPENED, "at": "2024-06-01 12:00:02+00:00"}],
        ["2024-06-01 12:00:00+00:00"],
    )
    assert markers[0]["position"] == 0.0
    assert markers[0]["frame"] == 0
