"""Last-events subject when the camera saw a registered cat and no chip was stored."""

from src.server_ui.live_view import resolve_event_subject


def test_chip_on_the_event_wins_over_camera():
    kind, rfid = resolve_event_subject(
        "0100A268EB26517C",
        {"krümel": 99.0},
        ["krümel"],
        {"krümel": "0100A268EB26517C"},
        32.0,
    )
    assert (kind, rfid) == ("chip", "0100A268EB26517C")


def test_registered_camera_class_without_chip_keeps_the_cat():
    kind, rfid = resolve_event_subject(
        "",
        {"krümel": 93.6},
        ["krümel", "testkatze"],
        {"krümel": "0100A268EB26517C", "testkatze": "OTHER"},
        32.0,
    )
    assert (kind, rfid) == ("camera", "0100A268EB26517C")


def test_generic_cat_class_stays_unknown():
    kind, rfid = resolve_event_subject(
        None,
        {"cat": 80.0},
        ["krümel"],
        {"krümel": "0100A268EB26517C"},
        32.0,
    )
    assert (kind, rfid) == ("unknown", None)


def test_person_wins_when_more_confident_than_the_cat():
    kind, rfid = resolve_event_subject(
        None,
        {"krümel": 40.0, "person": 90.0},
        ["krümel"],
        {"krümel": "0100A268EB26517C"},
        32.0,
    )
    assert (kind, rfid) == ("person", None)


def test_below_threshold_is_not_a_subject():
    kind, rfid = resolve_event_subject(
        None,
        {"krümel": 10.0},
        ["krümel"],
        {"krümel": "0100A268EB26517C"},
        32.0,
    )
    assert (kind, rfid) == ("none", None)


def test_chip_mode_keeps_a_registered_class_unknown():
    kind, rfid = resolve_event_subject(
        "",
        {"krümel": 93.6},
        ["krümel", "testkatze"],
        {"krümel": "0100A268EB26517C", "testkatze": "OTHER"},
        32.0,
        mode="chip",
    )
    assert (kind, rfid) == ("unknown", None)


def test_chip_mode_ignores_a_generic_cat_class():
    kind, rfid = resolve_event_subject(
        None,
        {"cat": 80.0},
        ["krümel"],
        {"krümel": "0100A268EB26517C"},
        32.0,
        mode="chip",
    )
    assert (kind, rfid) == ("none", None)


def test_chip_mode_still_names_a_person():
    kind, rfid = resolve_event_subject(
        None,
        {"krümel": 40.0, "person": 90.0},
        ["krümel"],
        {"krümel": "0100A268EB26517C"},
        32.0,
        mode="chip",
    )
    assert (kind, rfid) == ("person", None)


def test_chip_on_the_event_wins_in_chip_mode():
    kind, rfid = resolve_event_subject(
        "0100A268EB26517C",
        {"krümel": 99.0},
        ["krümel"],
        {"krümel": "0100A268EB26517C"},
        32.0,
        mode="chip",
    )
    assert (kind, rfid) == ("chip", "0100A268EB26517C")


def test_unknown_mode_follows_the_camera_default():
    kind, rfid = resolve_event_subject(
        "",
        {"krümel": 93.6},
        ["krümel"],
        {"krümel": "0100A268EB26517C"},
        32.0,
        mode="auto",
    )
    assert (kind, rfid) == ("camera", "0100A268EB26517C")


def test_subject_mode_click_accepts_only_a_fresh_timestamp():
    from src.server_ui.helpers import subject_mode_click_advanced

    now = 1_700_000_000_000
    toggled, previous = subject_mode_click_advanced(None, "camera", now_ms=now)
    assert (toggled, previous) == (False, None)
    toggled, previous = subject_mode_click_advanced(previous, 0, now_ms=now)
    assert (toggled, previous) == (False, None)
    toggled, previous = subject_mode_click_advanced(previous, now - 60_000, now_ms=now)
    assert (toggled, previous) == (False, now - 60_000)
    toggled, previous = subject_mode_click_advanced(previous, now - 200, now_ms=now)
    assert (toggled, previous) == (True, now - 200)
    toggled, previous = subject_mode_click_advanced(previous, now - 200, now_ms=now)
    assert (toggled, previous) == (False, now - 200)
    toggled, previous = subject_mode_click_advanced(previous, now - 100, now_ms=now)
    assert (toggled, previous) == (True, now - 100)
