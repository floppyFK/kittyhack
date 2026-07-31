"""More baseconfig / timeline / database / fake-hardware coverage."""

from datetime import datetime
from zoneinfo import ZoneInfo

import src.baseconfig as baseconfig
from src.baseconfig import AllowedToEnter, get_loggable_config_value, update_single_config_parameter
from src.database import CatsRepo, EventsRepo, ReturnDataCatDB
from src.event_timeline import (
    TimelineAction,
    parse_timeline_json,
    timeline_append,
    timeline_entries_to_html,
    timeline_extract_latest_event,
    timeline_fallback_from_event_type,
    timeline_format_message,
)
from src.hardware_sim import FakePir, FakeRfid
from src.helper import EventType
from src.magnets_rfid import RfidRunState


def test_update_single_config_parameter(tmp_config_ini):
    baseconfig.CONFIG["ELEMENTS_PER_PAGE"] = 33
    update_single_config_parameter("ELEMENTS_PER_PAGE")
    baseconfig.CONFIG.clear()
    baseconfig.load_config()
    assert baseconfig.CONFIG["ELEMENTS_PER_PAGE"] == 33


def test_update_single_unknown_parameter(tmp_config_ini, caplog):
    update_single_config_parameter("NOT_A_REAL_SETTING")
    assert any("Unknown config parameter" in r.message for r in caplog.records)


def test_get_loggable_masks_sensitive():
    # MQTT password / similar keys are masked when present in SENSITIVE set
    from src.baseconfig import SENSITIVE_CONFIG_KEYS

    key = next(iter(SENSITIVE_CONFIG_KEYS))
    masked = get_loggable_config_value(key, "secret-value")
    assert "secret" not in str(masked).lower() or masked == "********"
    assert get_loggable_config_value("LANGUAGE", "en") == "en"


def test_timeline_html_and_parse_and_extract():
    assert "event-timeline-empty" in timeline_entries_to_html([])
    entries = []
    timeline_append(entries, TimelineAction.MOTION_OUTSIDE)
    timeline_append(entries, TimelineAction.EVENT_CONCLUSION, conclusion=EventType.CAT_WENT_INSIDE)
    html = timeline_entries_to_html(entries, timezone="UTC")
    assert "event-timeline-list" in html
    assert parse_timeline_json(None) == []
    assert parse_timeline_json("[]") == []
    assert parse_timeline_json("{bad") == []
    dumped = '[{"action":"motion_outside","at":"x"}]'
    assert len(parse_timeline_json(dumped)) == 1

    multi = [
        {"action": TimelineAction.MOTION_OUTSIDE, "at": "a"},
        {"action": TimelineAction.EVENT_CONCLUSION, "at": "b", "conclusion": "x"},
        {"action": TimelineAction.MOTION_INSIDE, "at": "c"},
        {"action": TimelineAction.EVENT_CONCLUSION, "at": "d", "conclusion": "y"},
    ]
    latest = timeline_extract_latest_event(multi)
    assert latest[0]["action"] == TimelineAction.MOTION_INSIDE
    assert latest[-1]["conclusion"] == "y"
    assert timeline_extract_latest_event([]) == []
    assert timeline_extract_latest_event([{"action": TimelineAction.MOTION_OUTSIDE}])[0][
        "action"
    ] == TimelineAction.MOTION_OUTSIDE


def test_timeline_fallback_and_conclusion_message():
    entries = timeline_fallback_from_event_type(
        f"{EventType.CAT_WENT_INSIDE},{EventType.ENTRY_PER_CAT_ALLOWED}",
        "UTC",
        datetime(2024, 1, 1, tzinfo=ZoneInfo("UTC")),
    )
    assert len(entries) == 2
    assert entries[0]["action"] == TimelineAction.EVENT_CONCLUSION
    msg = timeline_format_message(
        {"action": TimelineAction.EVENT_CONCLUSION, "conclusion": EventType.CAT_WENT_INSIDE}
    )
    assert isinstance(msg, str) and len(msg) > 0


def test_database_update_delete_and_timeline(tmp_kittyhack_db):
    r = CatsRepo.db_add_new_cat(tmp_kittyhack_db, "Bob", "RFIDBOB", "", allow_exit=True)
    assert r.success
    names = CatsRepo.get_cat_names_list(tmp_kittyhack_db)
    assert "Bob" in names
    df = CatsRepo.db_get_cats(tmp_kittyhack_db, ReturnDataCatDB.all_except_photos)
    cat_id = int(df.iloc[0]["id"])
    assert CatsRepo.db_update_cat_data_by_id(
        tmp_kittyhack_db, cat_id, "Bobby", "RFIDBOB2", None, allow_entry=False
    ).success
    assert CatsRepo.get_cat_name_rfid_dict(tmp_kittyhack_db)["RFIDBOB2"] == "Bobby"

    entries = [{"action": "motion_outside", "at": "2024-01-01 00:00:00+00:00"}]
    assert EventsRepo.write_motion_timeline(tmp_kittyhack_db, 7, entries).success
    assert EventsRepo.write_motion_timeline(tmp_kittyhack_db, 7, []).success  # no-op
    timelines = EventsRepo.db_get_motion_timelines(tmp_kittyhack_db, [7])
    assert 7 in timelines
    assert EventsRepo.delete_motion_timeline_by_block_id(tmp_kittyhack_db, 7).success
    assert CatsRepo.db_delete_cat_by_id(tmp_kittyhack_db, cat_id).success
    assert CatsRepo.get_cat_names_list(tmp_kittyhack_db) == []


def test_fake_pir_update_state_and_rfid_run_stop():
    pir = FakePir()
    pir.init()
    pir.update_state("OUTSIDE", 1)
    pir.update_state("INSIDE", 1)
    o, i, _, _ = pir.get_states()
    assert o == 1 and i == 1

    rfid = FakeRfid()
    rfid.run(read_cycles=1)
    assert rfid.get_run_state() == RfidRunState.stopped
    rfid.set_run_state(RfidRunState.running)
    rfid.stop_read(wait_for_stop=False)
    # FakeRfid settles to stopped immediately (no reader thread required).
    assert rfid.get_run_state() == RfidRunState.stopped
