"""Entry-allow policy helpers (RFID vs video identity)."""
from src.baseconfig import AllowedToEnter
from src.helper import EventType


def _identified_tag_for_entry(tag_id, tag_id_from_video, known_rfid_tags, allowed_to_enter=None):
    """RFID reader identification takes priority over video identification."""
    if tag_id and tag_id in known_rfid_tags:
        return tag_id, "RFID"
    if allowed_to_enter == AllowedToEnter.CONFIGURE_PER_CAT and tag_id:
        # Any present RFID is authoritative; validity is resolved via cat_settings_map.
        return tag_id, "RFID"
    if allowed_to_enter == AllowedToEnter.KNOWN and tag_id:
        # Unknown chip is not a registered cat; do not fall back to video.
        return None, "RFID"
    if tag_id_from_video and tag_id_from_video in known_rfid_tags:
        return tag_id_from_video, "video"
    return None, None


def _compute_tag_id_valid_for_entry(allowed_to_enter, tag_id, identified_tag, cat_settings_map):
    """Return whether the identified cat/tag is allowed to enter under the current policy."""
    if allowed_to_enter == AllowedToEnter.CONFIGURE_PER_CAT:
        if not identified_tag:
            return False
        per_cat_allowed = (
            cat_settings_map[identified_tag].get('allow_entry', True)
            if identified_tag in cat_settings_map
            else False
        )
        return bool(per_cat_allowed)
    if allowed_to_enter == AllowedToEnter.KNOWN:
        return identified_tag is not None
    if allowed_to_enter == AllowedToEnter.ALL_RFIDS:
        return tag_id is not None
    if allowed_to_enter == AllowedToEnter.NONE:
        return False
    if allowed_to_enter == AllowedToEnter.ALL:
        return True
    return False


def _set_per_cat_entry_verdict_flag(additional_verdict_infos, tag_id_valid):
    """Replace per-cat entry allowed/denied flags in the verdict-info list; return the new flag."""
    allowed = str(EventType.ENTRY_PER_CAT_ALLOWED)
    denied = str(EventType.ENTRY_PER_CAT_DENIED)
    for flag in (allowed, denied):
        while flag in additional_verdict_infos:
            additional_verdict_infos.remove(flag)
    new_flag = allowed if tag_id_valid else denied
    additional_verdict_infos.append(new_flag)
    return new_flag
