"""Backend decision helpers: exit, prey gating, unlock gates, event conclusion."""

from src.baseconfig import AllowedToEnter, AllowedToExit
from src.backend.decisions import (
    build_unlock_inside_conditions,
    compute_mouse_check,
    conclude_motion_event_type,
    no_prey_within_timeout,
    pir_ok_for_camera_entry,
    resolve_per_cat_exit,
    resolve_prey_detection_enabled,
    time_in_exit_ranges,
    unlock_inside_ready,
)
from src.backend.entry_policy import (
    _compute_tag_id_valid_for_entry,
    _identified_tag_for_entry,
)
from src.helper import EventType
from src.hardware_sim import FakeMagnets, FakePir, FakeRfid


def test_resolve_per_cat_exit_non_per_cat_modes():
    allowed, pending, flag = resolve_per_cat_exit(AllowedToExit.ALLOW, None, {})
    assert allowed is True and pending is False and flag is None


def test_resolve_per_cat_exit_waits_for_rfid():
    allowed, pending, flag = resolve_per_cat_exit(
        AllowedToExit.CONFIGURE_PER_CAT, None, {"t1": {"allow_exit": True}}
    )
    assert allowed is False and pending is True and flag is None


def test_resolve_per_cat_exit_allow_and_deny():
    settings = {
        "ok": {"allow_exit": True},
        "no": {"allow_exit": False},
    }
    a, p, f = resolve_per_cat_exit(AllowedToExit.CONFIGURE_PER_CAT, "ok", settings)
    assert a is True and p is False and f == str(EventType.EXIT_PER_CAT_ALLOWED)
    a, p, f = resolve_per_cat_exit(AllowedToExit.CONFIGURE_PER_CAT, "no", settings)
    assert a is False and p is False and f == str(EventType.EXIT_PER_CAT_DENIED)
    # Unknown chip → denied (not in map)
    a, p, f = resolve_per_cat_exit(AllowedToExit.CONFIGURE_PER_CAT, "ghost", settings)
    assert a is False and p is False and f == str(EventType.EXIT_PER_CAT_DENIED)


def test_resolve_prey_detection_per_cat_override():
    settings = {"c1": {"enable_prey_detection": False}}
    enabled, disabled = resolve_prey_detection_enabled(True, "c1", None, settings)
    assert enabled is False and disabled is True
    enabled, disabled = resolve_prey_detection_enabled(True, None, "c1", settings)
    assert enabled is False and disabled is True
    enabled, disabled = resolve_prey_detection_enabled(True, "other", None, settings)
    assert enabled is True and disabled is False


def test_compute_mouse_check_paths():
    ok, cond = compute_mouse_check(False, 5, 0.0, 1.5)
    assert ok is True and cond["mouse_check_disabled"] is True

    ok, cond = compute_mouse_check(True, 0, 2.0, 1.5)
    assert ok is True and cond["no_mouse_detected"] is True

    ok, cond = compute_mouse_check(True, 0, 0.5, 1.5)
    assert ok is False  # not enough analysis time yet

    ok, cond = compute_mouse_check(True, 3, 5.0, 1.5)
    assert ok is False and cond["no_mouse_detected"] is False


def test_no_prey_within_timeout_gates():
    assert no_prey_within_timeout(True, 100.0, 101.0, 300) is True
    assert no_prey_within_timeout(False, 0.0, 50.0, 300) is True
    assert no_prey_within_timeout(False, 100.0, 200.0, 300) is False
    assert no_prey_within_timeout(False, 100.0, 450.0, 300) is True


def test_unlock_inside_ready_requires_all_gates():
    conds = build_unlock_inside_conditions(
        motion_outside=True,
        tag_id_valid=True,
        inside_locked=True,
        mouse_check=True,
        outside_locked=True,
        no_unlock_queued=True,
        no_prey_within_timeout_effective=True,
        not_manually_locked=True,
    )
    assert unlock_inside_ready(conds) is True
    conds["mouse_check"] = False
    assert unlock_inside_ready(conds) is False
    conds["mouse_check"] = True
    conds["not_held_after_max_unlock"] = False
    assert unlock_inside_ready(conds) is False
    conds["not_held_after_max_unlock"] = True
    conds["not_in_safety_cooldown"] = False
    assert unlock_inside_ready(conds) is False


def test_pir_ok_for_camera_entry():
    assert pir_ok_for_camera_entry(False, True, True, False) is True
    assert pir_ok_for_camera_entry(True, False, False, False) is True
    assert pir_ok_for_camera_entry(True, True, False, False) is False
    assert pir_ok_for_camera_entry(True, True, False, True) is True
    assert pir_ok_for_camera_entry(True, False, True, False) is False
    assert pir_ok_for_camera_entry(True, False, True, True) is True


def test_full_entry_path_with_fakes_and_gates():
    """RFID inject → identify → policy → prey OK → unlock gates ready."""
    pir = FakePir()
    magnets = FakeMagnets()
    rfid = FakeRfid()
    pir.init()
    magnets.init()

    pir.trigger_outside(True)
    rfid.inject_tag("CAT1", 10.0)
    known = ["CAT1"]
    tag_id, _ = rfid.get_tag()
    identified, source = _identified_tag_for_entry(
        tag_id, None, known, allowed_to_enter=AllowedToEnter.KNOWN
    )
    assert identified == "CAT1" and source == "RFID"
    tag_ok = _compute_tag_id_valid_for_entry(
        AllowedToEnter.KNOWN, tag_id, identified, {}
    )
    mouse_ok, _ = compute_mouse_check(True, 0, 2.0, 1.5)
    prey_ok = no_prey_within_timeout(False, 0.0, 100.0, 300)
    outside, inside, _, _ = pir.get_states()
    conds = build_unlock_inside_conditions(
        motion_outside=outside == 1,
        tag_id_valid=tag_ok,
        inside_locked=not magnets.get_inside_state(),
        mouse_check=mouse_ok,
        outside_locked=not magnets.get_outside_state(),
        no_unlock_queued=not magnets.check_queued("unlock_inside"),
        no_prey_within_timeout_effective=prey_ok,
        not_manually_locked=True,
    )
    assert unlock_inside_ready(conds)
    magnets.queue_command("unlock_inside")
    assert magnets.get_inside_state() is True


def test_prey_blocks_unlock_until_timeout():
    mouse_ok, _ = compute_mouse_check(True, 2, 5.0, 1.5)
    assert mouse_ok is False
    assert no_prey_within_timeout(False, 100.0, 150.0, 300) is False


def test_conclude_motion_event_types():
    assert (
        conclude_motion_event_type(
            first_motion_outside_mono=10.0,
            first_motion_inside_raw_mono=0.0,
            unlock_inside_tm=0.0,
            tag_id=None,
            no_mouse_detected=True,
        )
        == EventType.MOTION_OUTSIDE_ONLY
    )
    assert (
        conclude_motion_event_type(
            first_motion_outside_mono=10.0,
            first_motion_inside_raw_mono=0.0,
            unlock_inside_tm=0.0,
            tag_id=None,
            no_mouse_detected=False,
        )
        == EventType.MOTION_OUTSIDE_WITH_MOUSE
    )
    assert (
        conclude_motion_event_type(
            first_motion_outside_mono=10.0,
            first_motion_inside_raw_mono=0.0,
            unlock_inside_tm=12.0,
            tag_id="CAT1",
            no_mouse_detected=True,
        )
        == EventType.CAT_WENT_PROBABLY_INSIDE
    )
    assert (
        conclude_motion_event_type(
            first_motion_outside_mono=10.0,
            first_motion_inside_raw_mono=15.0,
            unlock_inside_tm=0.0,
            tag_id=None,
            no_mouse_detected=True,
        )
        == EventType.CAT_WENT_INSIDE
    )
    assert (
        conclude_motion_event_type(
            first_motion_outside_mono=10.0,
            first_motion_inside_raw_mono=15.0,
            unlock_inside_tm=0.0,
            tag_id=None,
            no_mouse_detected=False,
        )
        == EventType.CAT_WENT_INSIDE_WITH_MOUSE
    )
    assert (
        conclude_motion_event_type(
            first_motion_outside_mono=20.0,
            first_motion_inside_raw_mono=10.0,
            unlock_inside_tm=0.0,
            tag_id=None,
            no_mouse_detected=True,
        )
        == EventType.CAT_WENT_OUTSIDE
    )


def test_time_in_exit_ranges_day_and_overnight():
    ranges = [(True, "09:00", "17:00"), (False, "00:00", "00:00"), (False, "00:00", "00:00")]
    assert time_in_exit_ranges("12:00", ranges) is True
    assert time_in_exit_ranges("18:00", ranges) is False

    overnight = [(True, "22:00", "06:00")]
    assert time_in_exit_ranges("23:30", overnight) is True
    assert time_in_exit_ranges("05:00", overnight) is True
    assert time_in_exit_ranges("12:00", overnight) is False

    assert time_in_exit_ranges("12:00", [(False, "09:00", "17:00")]) is True
