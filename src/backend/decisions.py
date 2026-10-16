"""Pure decision helpers for the backend control loop (unit-test friendly)."""

from __future__ import annotations

import logging

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


def pir_ok_for_camera_entry(
    require_pir: bool,
    use_camera_for_motion: bool,
    use_camera_for_cat_detection: bool,
    pir_outside_active: bool,
) -> bool:
    """Whether optional PIR second-factor for camera-based entry is satisfied.

    When the setting is off, or neither camera option is enabled, this is True.
    """
    if not require_pir:
        return True
    if not (use_camera_for_motion or use_camera_for_cat_detection):
        return True
    return bool(pir_outside_active)


def should_suggest_pir_second_factor(
    require_pir: bool,
    use_camera_for_motion: bool,
    use_camera_for_cat_detection: bool,
) -> bool:
    """True when camera-based entry is on and PIR 2FA is still optional/off."""
    if require_pir:
        return False
    return bool(use_camera_for_motion or use_camera_for_cat_detection)


def sync_pir_second_factor_suggestion() -> bool:
    """Queue or drop the muteable PIR-2FA tip from the current CONFIG.

    Returns True if a new notification was added.
    """
    from src.baseconfig import CONFIG, UserNotifications, set_language

    _ = set_language(CONFIG.get("LANGUAGE", "en"))
    nid = UserNotifications.ID_SUGGEST_PIR_SECOND_FACTOR
    should = should_suggest_pir_second_factor(
        bool(CONFIG.get("REQUIRE_OUTSIDE_PIR_FOR_CAMERA_ENTRY", False)),
        bool(CONFIG.get("USE_CAMERA_FOR_MOTION_DETECTION", False)),
        bool(CONFIG.get("USE_CAMERA_FOR_CAT_DETECTION", False)),
    )
    if not should:
        if UserNotifications.get_by_id(nid):
            UserNotifications.remove(nid)
        return False
    added = UserNotifications.add(
        header=_("PIR confirmation for camera entry"),
        message=(
            _("You are using the camera to detect cats, but the outside PIR is not configured "
              "as a second factor for entry.")
            + "\n\n"
            + _("If the camera keeps detecting a cat when none is at the flap, magnet protection "
                "will lock the inside. A real cat without RFID can then only enter when the "
                "outside PIR also triggers.")
            + "\n\n"
            + _("Consider enabling **Additionally require the outside PIR for camera-based entry** in Settings. "
                "This is useful if the camera sometimes falsely detects a cat due to sun glare "
                "or reflections.")
        ),
        type="warning",
        id=nid,
        skip_if_id_exists=True,
        muteable=True,
    )
    return added == nid


def uses_original_detection_model(tflite_version) -> bool:
    """True when a bundled Kittyflap TFLite model is the active detector."""
    return bool(str(tflite_version or "").strip())


def _has_custom_yolo_model() -> bool:
    try:
        from src.model import YoloModel

        return any(
            (model.get("unique_id") or "").strip()
            for model in YoloModel.get_model_list()
        )
    except Exception as e:
        logging.debug(f"[USR_NOTIFICATIONS] Could not list custom models: {e}")
        return False


def _model_training_guide_url(language: str | None) -> str:
    if str(language or "").strip().lower().startswith("de"):
        return (
            "https://github.com/floppyFK/kittyhack/wiki/"
            "%5BDE%5D-Kittyhack-v2.0-%E2%80%90-Eigene-KI%E2%80%90Modelle-trainieren"
        )
    return (
        "https://github.com/floppyFK/kittyhack/wiki/"
        "%5BEN%5D-Kittyhack-v2.0-%E2%80%90-Train-own-AI%E2%80%90Models"
    )


def _original_model_notice_message(has_custom_model: bool) -> str:
    from src.baseconfig import CONFIG, set_language

    language = CONFIG.get("LANGUAGE", "en")
    _ = set_language(language)
    intro = _("Kittyhack is using currently the model that came with the Kittyflap (v1 or v2).")
    problem = _(
        "This model is extremely inaccurate, because it was not trained for your cats. "
        "Not all Kittyhack features work with it. The camera cannot identify your cat "
        "with this model, for example."
    )
    if has_custom_model:
        action = _(
            "You already have a trained model. It is much more accurate. Select it under "
            "**Settings**, in the **Flap** section, as the **Detection model**."
        )
        return f"{intro}\n\n{problem}\n\n{action}"
    later = _(
        "The flap still works with the microchip reader. You do not need to change anything right now."
    )
    guide = _("model training guide")
    train = _(
        "However, a model you train yourself is much more accurate. Kittyhack needs pictures of your cats first. "
        "You can create that model later under **AI Training**. "
        "The steps are explained in the [{guide}]({url})."
    ).format(guide=guide, url=_model_training_guide_url(language))
    return f"{intro}\n\n{problem}\n\n{later}\n\n{train}"


def sync_original_model_notice() -> bool:
    """Queue or drop the muteable original-model hint from the current CONFIG.

    New installations ship without a trained model, so the text explains that
    the flap still works and training can wait.

    Returns True if a new notification was added.
    """
    from src.baseconfig import CONFIG, UserNotifications, set_language

    _ = set_language(CONFIG.get("LANGUAGE", "en"))
    nid = UserNotifications.ID_ORIGINAL_MODEL_ACCURACY
    if not uses_original_detection_model(CONFIG.get("TFLITE_MODEL_VERSION")):
        if UserNotifications.get_by_id(nid):
            UserNotifications.remove(nid)
        return False
    added = UserNotifications.add(
        header=_("Original detection model"),
        message=_original_model_notice_message(_has_custom_yolo_model()),
        type="warning",
        id=nid,
        skip_if_id_exists=True,
        muteable=True,
    )
    return added == nid


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
    not_held_after_max_unlock: bool = True,
    not_in_safety_cooldown: bool = True,
    pir_ok_for_camera_entry: bool = True,
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
        "not_held_after_max_unlock": bool(not_held_after_max_unlock),
        "not_in_safety_cooldown": bool(not_in_safety_cooldown),
        "pir_ok_for_camera_entry": bool(pir_ok_for_camera_entry),
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
