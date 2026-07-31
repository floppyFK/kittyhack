"""Unit tests for entry allow/deny policy helpers."""

from src.baseconfig import AllowedToEnter
from src.backend.entry_policy import (
    _compute_tag_id_valid_for_entry,
    _identified_tag_for_entry,
    _set_per_cat_entry_verdict_flag,
)
from src.helper import EventType


KNOWN = ["tag_a", "tag_b"]


def test_identified_tag_rfid_priority_over_video():
    tag, source = _identified_tag_for_entry("tag_a", "tag_b", KNOWN)
    assert tag == "tag_a"
    assert source == "RFID"


def test_identified_tag_falls_back_to_video():
    tag, source = _identified_tag_for_entry(None, "tag_b", KNOWN)
    assert tag == "tag_b"
    assert source == "video"


def test_identified_tag_known_mode_rejects_unknown_rfid_video_fallback():
    tag, source = _identified_tag_for_entry(
        "unknown", "tag_a", KNOWN, allowed_to_enter=AllowedToEnter.KNOWN
    )
    assert tag is None
    assert source == "RFID"


def test_compute_valid_all_and_none():
    assert _compute_tag_id_valid_for_entry(AllowedToEnter.ALL, None, None, {}) is True
    assert _compute_tag_id_valid_for_entry(AllowedToEnter.NONE, "x", "x", {}) is False


def test_compute_valid_known_and_all_rfids():
    assert _compute_tag_id_valid_for_entry(AllowedToEnter.KNOWN, "x", "tag_a", {}) is True
    assert _compute_tag_id_valid_for_entry(AllowedToEnter.KNOWN, "x", None, {}) is False
    assert _compute_tag_id_valid_for_entry(AllowedToEnter.ALL_RFIDS, "any", None, {}) is True
    assert _compute_tag_id_valid_for_entry(AllowedToEnter.ALL_RFIDS, None, None, {}) is False


def test_compute_valid_per_cat():
    settings = {"tag_a": {"allow_entry": True}, "tag_b": {"allow_entry": False}}
    assert (
        _compute_tag_id_valid_for_entry(
            AllowedToEnter.CONFIGURE_PER_CAT, "tag_a", "tag_a", settings
        )
        is True
    )
    assert (
        _compute_tag_id_valid_for_entry(
            AllowedToEnter.CONFIGURE_PER_CAT, "tag_b", "tag_b", settings
        )
        is False
    )
    assert (
        _compute_tag_id_valid_for_entry(
            AllowedToEnter.CONFIGURE_PER_CAT, None, None, settings
        )
        is False
    )


def test_identified_tag_configure_per_cat_keeps_unknown_rfid():
    tag, source = _identified_tag_for_entry(
        "stranger",
        "tag_a",
        KNOWN,
        allowed_to_enter=AllowedToEnter.CONFIGURE_PER_CAT,
    )
    assert tag == "stranger"
    assert source == "RFID"


def test_identified_tag_returns_none_when_nothing_matches():
    tag, source = _identified_tag_for_entry(None, "unknown_video", KNOWN)
    assert tag is None
    assert source is None


def test_per_cat_verdict_flag_denied():
    infos = []
    flag = _set_per_cat_entry_verdict_flag(infos, False)
    assert flag == str(EventType.ENTRY_PER_CAT_DENIED)
    assert infos == [str(EventType.ENTRY_PER_CAT_DENIED)]
