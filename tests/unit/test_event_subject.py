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
