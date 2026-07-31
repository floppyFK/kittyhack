"""Pure decision helpers for the backend control loop (unit-test friendly)."""

from __future__ import annotations

from src.baseconfig import AllowedToExit
from src.helper import EventType


def resolve_per_cat_exit(
    allowed_to_exit,
    tag_id,
    cat_settings_map: dict,
) -> tuple[bool, bool, str | None]:
    """Resolve per-cat exit allowance.

    Returns:
        ``(per_cat_exit_allowed, pending_rfid, verdict_flag_or_None)``

        - Non per-cat modes: ``(True, False, None)``
        - Per-cat without RFID yet: ``(False, True, None)`` — keep waiting
        - Per-cat with RFID: allowed from settings (default True if known key),
          plus a verdict flag string when a tag is known
    """
    if allowed_to_exit != AllowedToExit.CONFIGURE_PER_CAT:
        return True, False, None

    if tag_id is None:
        return False, True, None

    per_cat_exit_allowed = False
    if tag_id in cat_settings_map:
        per_cat_exit_allowed = bool(cat_settings_map[tag_id].get("allow_exit", True))

    flag = (
        str(EventType.EXIT_PER_CAT_ALLOWED)
        if per_cat_exit_allowed
        else str(EventType.EXIT_PER_CAT_DENIED)
    )
    return per_cat_exit_allowed, False, flag


def resolve_prey_detection_enabled(
    global_enabled: bool,
    tag_id,
    tag_id_from_video,
    cat_settings_map: dict,
) -> tuple[bool, bool]:
    """Apply per-cat prey override.

    Returns:
        ``(prey_detection_enabled, per_cat_prey_detection_disabled)``
    """
    prey_detection_enabled = bool(global_enabled)
    per_cat_prey_detection_disabled = False
    current_tag_any = tag_id if tag_id else tag_id_from_video
    if current_tag_any and current_tag_any in cat_settings_map:
        if cat_settings_map[current_tag_any].get("enable_prey_detection", True) is False:
            prey_detection_enabled = False
            per_cat_prey_detection_disabled = True
    return prey_detection_enabled, per_cat_prey_detection_disabled


def compute_mouse_check(
    prey_detection_enabled: bool,
    ids_with_mouse_count: int,
    analysis_elapsed_s: float,
    min_seconds_to_analyze: float,
) -> tuple[bool, dict]:
    """Whether prey gating is satisfied for an inside unlock.

    ``mouse_check`` True means "OK to unlock from a prey perspective"
    (disabled, or no mouse after enough analysis time).
    """
    conditions = {
        "mouse_check_disabled": prey_detection_enabled is False,
        "no_mouse_detected": int(ids_with_mouse_count) == 0,
        "sufficient_analysis_time": float(analysis_elapsed_s)
        >= float(min_seconds_to_analyze or 0.0),
    }
    mouse_check = conditions["mouse_check_disabled"] or (
        conditions["no_mouse_detected"] and conditions["sufficient_analysis_time"]
    )
    return mouse_check, conditions


def no_prey_within_timeout(
    per_cat_prey_detection_disabled: bool,
    prey_detection_mono: float,
    now_mono: float,
    lock_duration_s: float,
) -> bool:
    """True if the post-prey lock window does not block unlocking."""
    if per_cat_prey_detection_disabled:
        return True
    prey_mono = float(prey_detection_mono or 0.0)
    if prey_mono == 0.0:
        return True
    return (float(now_mono) - prey_mono) > float(lock_duration_s)


def unlock_inside_ready(conditions: dict) -> bool:
    """True when every unlock-inside gate in ``conditions`` is True."""
    return all(bool(v) for v in conditions.values())


def build_unlock_inside_conditions(
    *,
    motion_outside: bool,
    tag_id_valid: bool,
    inside_locked: bool,
    mouse_check: bool,
    outside_locked: bool,
    no_unlock_queued: bool,
    no_prey_within_timeout_effective: bool,
    not_manually_locked: bool,
) -> dict:
    """Named gates for logging / MQTT (same keys as the live loop)."""
    return {
        "motion_outside": bool(motion_outside),
        "tag_id_valid": bool(tag_id_valid),
        "inside_locked": bool(inside_locked),
        "mouse_check": bool(mouse_check),
        "outside_locked": bool(outside_locked),
        "no_unlock_queued": bool(no_unlock_queued),
        "no_prey_within_timeout": bool(no_prey_within_timeout_effective),
        "not_manually_locked": bool(not_manually_locked),
    }


def conclude_motion_event_type(
    *,
    first_motion_outside_mono: float,
    first_motion_inside_raw_mono: float,
    unlock_inside_tm: float,
    tag_id,
    no_mouse_detected: bool,
) -> str:
    """Pick the motion-block conclusion event-type string (``EventType`` value)."""
    if first_motion_inside_raw_mono == 0.0 or (
        first_motion_outside_mono - first_motion_inside_raw_mono
    ) > 60.0:
        if unlock_inside_tm > first_motion_outside_mono and tag_id is not None:
            return EventType.CAT_WENT_PROBABLY_INSIDE
        if no_mouse_detected:
            return EventType.MOTION_OUTSIDE_ONLY
        return EventType.MOTION_OUTSIDE_WITH_MOUSE

    if first_motion_outside_mono < first_motion_inside_raw_mono:
        if no_mouse_detected:
            return EventType.CAT_WENT_INSIDE
        return EventType.CAT_WENT_INSIDE_WITH_MOUSE

    return EventType.CAT_WENT_OUTSIDE


def time_in_exit_ranges(
    current_hhmm: str,
    ranges: list[tuple[bool, str, str]],
) -> bool:
    """Whether ``current_hhmm`` (``HH:MM``) falls in any enabled exit range.

    Each range is ``(enabled, start_hhmm, end_hhmm)``. Overnight ranges where
    ``start > end`` are supported. If no range is enabled, returns True.
    """
    any_configured = False
    for enabled, start_time, end_time in ranges:
        if not enabled:
            continue
        any_configured = True
        if start_time > end_time:
            if current_hhmm >= start_time or current_hhmm <= end_time:
                return True
        elif start_time <= current_hhmm <= end_time:
            return True
    return not any_configured
