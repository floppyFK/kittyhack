"""Motion-block event timeline: structured action log for the events UI."""

from __future__ import annotations

import html
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from src.baseconfig import CONFIG, set_language
from src.helper import (
    DateTimeUtil,
    EventType,
)
from src.clock import wall_time

_ = set_language(CONFIG["LANGUAGE"])

class TimelineAction:
    """Canonical action string constants stored in motion-block timelines."""

    MOTION_OUTSIDE = "motion_outside"
    MOTION_OUTSIDE_END = "motion_outside_end"
    MOTION_INSIDE = "motion_inside"
    CAT_DETECTED_VIDEO = "cat_detected_video"
    CAT_DETECTED_RFID = "cat_detected_rfid"
    RFID_OVERRIDES_VIDEO = "rfid_overrides_video"
    ENTRY_ALLOWED = "entry_allowed"
    ENTRY_DENIED = "entry_denied"
    PREY_DETECTED = "prey_detected"
    NO_PREY_DETECTED = "no_prey_detected"
    PER_CAT_PREY_DISABLED = "per_cat_prey_disabled"
    INSIDE_OPENED = "inside_opened"
    INSIDE_OPENED_MANUAL = "inside_opened_manual"
    INSIDE_CLOSED = "inside_closed"
    INSIDE_CLOSED_PREY = "inside_closed_prey"
    INSIDE_CLOSED_ENTRY_DENIED = "inside_closed_entry_denied"
    INSIDE_CLOSED_MOTION_END = "inside_closed_motion_end"
    INSIDE_CLOSED_MAX_TIME = "inside_closed_max_time"
    INSIDE_CLOSED_MANUAL = "inside_closed_manual"
    OUTSIDE_OPENED = "outside_opened"
    OUTSIDE_CLOSED = "outside_closed"
    EVENT_CONCLUSION = "event_conclusion"
    EXIT_SKIPPED_ENTRY = "exit_skipped_entry"
    FAST_IN_OUT_CROSSING = "fast_in_out_crossing"
    INSIDE_CLOSED_FAST_IN_OUT = "inside_closed_fast_in_out"
    OUTSIDE_CLOSED_FAST_IN_OUT = "outside_closed_fast_in_out"


_DOOR_ACTION_KINDS = {
    TimelineAction.INSIDE_OPENED: "inside-open",
    TimelineAction.INSIDE_OPENED_MANUAL: "inside-open",
    TimelineAction.INSIDE_CLOSED: "inside-close",
    TimelineAction.INSIDE_CLOSED_PREY: "inside-close",
    TimelineAction.INSIDE_CLOSED_ENTRY_DENIED: "inside-close",
    TimelineAction.INSIDE_CLOSED_MOTION_END: "inside-close",
    TimelineAction.INSIDE_CLOSED_MAX_TIME: "inside-close",
    TimelineAction.INSIDE_CLOSED_MANUAL: "inside-close",
    TimelineAction.INSIDE_CLOSED_FAST_IN_OUT: "inside-close",
    TimelineAction.OUTSIDE_OPENED: "outside-open",
    TimelineAction.OUTSIDE_CLOSED: "outside-close",
    TimelineAction.OUTSIDE_CLOSED_FAST_IN_OUT: "outside-close",
}


def timeline_door_action_kind(action: str) -> str | None:
    """Return the visual marker kind for a door action, if it is one."""
    return _DOOR_ACTION_KINDS.get(action)


def timeline_parse_timestamp(at_value) -> float | None:
    """Parse a stored timeline/event timestamp to Unix seconds."""
    try:
        if isinstance(at_value, (int, float)):
            return float(at_value)
        raw = str(at_value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        if len(raw) >= 5 and raw[-5] in "+-" and ":" not in raw[-5:]:
            raw = raw[:-2] + ":" + raw[-2:]
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.timestamp()
    except Exception:
        return None


def timeline_door_markers(entries: list, frame_timestamps: list) -> list:
    """Return positioned door-action markers for an event player timeline."""
    parsed_frames = [timeline_parse_timestamp(value) for value in frame_timestamps]
    valid_frames = [value for value in parsed_frames if value is not None]
    if not valid_frames:
        return []
    start = min(valid_frames)
    end = max(valid_frames)
    span = max(0.0, end - start)
    markers = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        kind = timeline_door_action_kind(entry.get("action"))
        at = timeline_parse_timestamp(entry.get("at"))
        if not kind or at is None:
            continue
        position = 0.0 if span == 0 else max(0.0, min(1.0, (at - start) / span))
        nearest_idx = min(
            range(len(parsed_frames)),
            key=lambda idx: abs((parsed_frames[idx] if parsed_frames[idx] is not None else start) - at),
        )
        markers.append(
            {
                "position": position,
                "frame": nearest_idx,
                "kind": kind,
                "label": timeline_format_message(entry),
            }
        )
    return markers

def timeline_append(entries: list, action: str, **detail) -> None:
    """Append one timeline entry (UTC timestamp, second precision)."""
    entry = {"action": action, "at": DateTimeUtil.get_utc_date_string(wall_time())}
    for key, value in detail.items():
        if value is not None:
            entry[key] = value
    entries.append(entry)

def timeline_format_time(at_utc: str, timezone: str) -> str:
    """Format a stored UTC timestamp for display (HH:MM:SS, no milliseconds)."""
    try:
        raw = str(at_utc).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        if len(raw) >= 5 and raw[-5] in "+-" and ":" not in raw[-5:]:
            raw = raw[:-2] + ":" + raw[-2:]
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo(timezone)).strftime("%H:%M:%S")
    except Exception:
        text = str(at_utc)
        return text.split(".")[0][-8:] if text else ""

def timeline_format_message(entry: dict) -> str:
    """Return a translated label for one timeline entry."""
    action = entry.get("action", "")
    cat = entry.get("cat_name") or _("Unknown cat")
    source = entry.get("source", "")
    rfid_cat = entry.get("rfid_cat_name") or entry.get("rfid_cat") or _("Unknown cat")
    video_cat = entry.get("video_cat_name") or entry.get("video_cat") or _("Unknown cat")
    conclusion = entry.get("conclusion", "")

    messages = {
        TimelineAction.MOTION_OUTSIDE: _("Motion detected outside"),
        TimelineAction.MOTION_OUTSIDE_END: _("Motion outside ended"),
        TimelineAction.MOTION_INSIDE: _("Motion detected inside"),
        TimelineAction.CAT_DETECTED_VIDEO: _("{cat} detected via video").format(cat=cat),
        TimelineAction.CAT_DETECTED_RFID: _("{cat} detected via RFID").format(cat=cat),
        TimelineAction.RFID_OVERRIDES_VIDEO: _(
            "RFID identification ({rfid_cat}) overrides video ({video_cat})"
        ).format(rfid_cat=rfid_cat, video_cat=video_cat),
        TimelineAction.ENTRY_ALLOWED: _("Entry allowed for {cat}").format(cat=cat)
        + (f" ({source})" if source else ""),
        TimelineAction.ENTRY_DENIED: _("Entry denied for {cat}").format(cat=cat)
        + (f" ({source})" if source else ""),
        TimelineAction.PREY_DETECTED: _("Prey detected"),
        TimelineAction.NO_PREY_DETECTED: _("No prey detected"),
        TimelineAction.PER_CAT_PREY_DISABLED: _("Prey detection disabled for {cat}").format(cat=cat),
        TimelineAction.INSIDE_OPENED: _("Inside unlocked"),
        TimelineAction.INSIDE_OPENED_MANUAL: _("Inside unlocked manually"),
        TimelineAction.INSIDE_CLOSED: _("Inside locked"),
        TimelineAction.INSIDE_CLOSED_PREY: _("Inside locked after detecting prey"),
        TimelineAction.INSIDE_CLOSED_ENTRY_DENIED: _("Inside locked because entry was denied"),
        TimelineAction.INSIDE_CLOSED_MOTION_END: _("Inside locked after movement ended"),
        TimelineAction.INSIDE_CLOSED_MAX_TIME: _("Inside locked after the maximum unlock time"),
        TimelineAction.INSIDE_CLOSED_MANUAL: _("Inside locked manually"),
        TimelineAction.OUTSIDE_OPENED: _("Outside unlocked"),
        TimelineAction.OUTSIDE_CLOSED: _("Outside locked"),
        TimelineAction.EXIT_SKIPPED_ENTRY: _("Entry decision skipped (exit in progress)"),
        TimelineAction.FAST_IN_OUT_CROSSING: _("Cat crossed the flap"),
        TimelineAction.INSIDE_CLOSED_FAST_IN_OUT: _("Inside locked after passage"),
        TimelineAction.OUTSIDE_CLOSED_FAST_IN_OUT: _("Outside locked after passage"),
    }

    if action == TimelineAction.EVENT_CONCLUSION and conclusion:
        return EventType.to_pretty_string(conclusion)

    return messages.get(action, action.replace("_", " ").capitalize())

def timeline_entries_to_html(entries: list, timezone: str | None = None) -> str:
    """Render timeline entries as an HTML list."""
    tz = timezone or CONFIG.get("TIMEZONE", "UTC")
    if not entries:
        return f'<p class="event-timeline-empty">{html.escape(_("No detailed timeline recorded for this event."))}</p>'

    items = []
    for entry in entries:
        time_str = timeline_format_time(entry.get("at", ""), tz)
        message = html.escape(timeline_format_message(entry))
        items.append(
            f'<li class="event-timeline-item">'
            f'<span class="event-timeline-time">{html.escape(time_str)}</span>'
            f'<span class="event-timeline-text">{message}</span>'
            f"</li>"
        )
    return f'<ul class="event-timeline-list">{"".join(items)}</ul>'

def timeline_fallback_from_event_type(event_type: str, timezone: str, created_at) -> list:
    """Build a coarse single-timestamp timeline from legacy comma-separated event_type."""
    try:
        if hasattr(created_at, "strftime"):
            dt = created_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            at = dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S+00:00")
        else:
            at = str(created_at)
    except Exception:
        at = DateTimeUtil.get_utc_date_string(wall_time())

    entries = []
    for part in str(event_type).split(","):
        part = part.strip()
        if not part:
            continue
        entries.append({"action": TimelineAction.EVENT_CONCLUSION, "at": at, "conclusion": part})
    return entries

def parse_timeline_json(timeline_json: str | None) -> list:
    """Parse a stored timeline JSON string into a list (empty on error)."""
    if not timeline_json:
        return []
    try:
        data = json.loads(timeline_json)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def timeline_extract_latest_event(entries: list) -> list:
    """Keep only the latest completed segment (between EVENT_CONCLUSIONs)."""
    if not entries:
        return []

    latest_conclusion_idx = -1
    for idx, entry in enumerate(entries):
        if isinstance(entry, dict) and entry.get("action") == TimelineAction.EVENT_CONCLUSION:
            latest_conclusion_idx = idx

    if latest_conclusion_idx < 0:
        return entries

    start_idx = 0
    for idx in range(latest_conclusion_idx - 1, -1, -1):
        entry = entries[idx]
        if isinstance(entry, dict) and entry.get("action") == TimelineAction.EVENT_CONCLUSION:
            start_idx = idx + 1
            break

    return entries[start_idx:latest_conclusion_idx + 1]
