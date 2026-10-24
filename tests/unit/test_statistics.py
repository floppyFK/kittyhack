"""Visit statistics: classification, duration pairing, backfill, aggregation."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.database import CatsRepo, EventsRepo, VisitStatsRepo
from src.helper import DateTimeUtil, EventType
from src.statistics import (
    CAT_FILTER_ALL,
    MANUAL_LOCATION_CONCLUSION,
    apply_manual_location,
    backfill_from_events,
    build_dashboard,
    classify_visit,
    dump_hidden_cat_keys,
    format_duration,
    parse_hidden_cat_keys,
    infer_other_access,
    pair_duration,
    record_motion_conclusion,
    resolve_bucket,
)

_VISIT_RANGE = (
    "2000-01-01 00:00:00.0000+00:00",
    "2100-01-01 00:00:00.0000+00:00",
)


def test_classify_passages_and_prey():
    inside = classify_visit(EventType.CAT_WENT_INSIDE)
    assert inside["passed"] == 1 and inside["direction"] == "in" and inside["prey"] == 0

    outside = classify_visit(EventType.CAT_WENT_OUTSIDE)
    assert outside["passed"] == 1 and outside["direction"] == "out"

    prey_in = classify_visit(EventType.CAT_WENT_INSIDE_WITH_MOUSE)
    assert prey_in["passed"] == 1 and prey_in["prey"] == 1

    probably = classify_visit(EventType.CAT_WENT_PROBABLY_INSIDE)
    assert probably["uncertain_entry"] == 1 and probably["passed"] == 1


def test_classify_attempts_and_denied():
    miss = classify_visit(EventType.MOTION_OUTSIDE_ONLY)
    assert miss["passed"] == 0 and miss["attempt_no_pass"] == 1 and miss["direction"] == "in"

    prey_block = classify_visit(EventType.MOTION_OUTSIDE_WITH_MOUSE)
    assert prey_block["prey"] == 1 and prey_block["attempt_no_pass"] == 1

    denied = classify_visit(
        EventType.MOTION_OUTSIDE_ONLY, [EventType.ENTRY_PER_CAT_DENIED]
    )
    assert denied["denied_entry"] == 1 and denied["attempt_no_pass"] == 1

    comma = classify_visit(
        f"{EventType.CAT_WENT_OUTSIDE},{EventType.EXIT_PER_CAT_DENIED}"
    )
    assert comma["passed"] == 1 and comma["denied_exit"] == 1

    assert classify_visit(EventType.MANUALLY_UNLOCKED) is None


def test_format_duration_and_buckets():
    assert format_duration(None) == "—"
    assert format_duration(45) == "0m"
    assert "h" in format_duration(3660)
    assert resolve_bucket("24h", "auto") == "hour"
    assert resolve_bucket("7d", "auto") == "day"
    assert resolve_bucket("12m", "auto") == "week"
    assert resolve_bucket("all", "auto") == "month"
    assert resolve_bucket("30d", "week") == "week"


def test_record_and_pair_durations(tmp_kittyhack_db):
    t0 = DateTimeUtil.get_utc_date_string(1_700_000_000.0)
    t1 = DateTimeUtil.get_utc_date_string(1_700_000_000.0 + 3600)
    t2 = DateTimeUtil.get_utc_date_string(1_700_000_000.0 + 3600 + 7200)

    r1 = record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=t0,
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.CAT_WENT_OUTSIDE,
    )
    assert r1.success
    r2 = record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=t1,
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.CAT_WENT_INSIDE,
    )
    assert r2.success
    r3 = record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=t2,
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.CAT_WENT_OUTSIDE,
    )
    assert r3.success

    rows = VisitStatsRepo.query_range(
        tmp_kittyhack_db,
        "2000-01-01 00:00:00.0000+00:00",
        "2100-01-01 00:00:00.0000+00:00",
        "RFID001",
    )
    assert len(rows) == 3
    assert rows[1]["duration_outside_s"] == 3600
    assert rows[1]["duration_inside_s"] is None
    assert rows[2]["duration_inside_s"] == 7200

    last = VisitStatsRepo.get_last_passed(tmp_kittyhack_db, "RFID001")
    assert last["direction"] == "out"


def test_pair_duration_skips_clock_jump_and_same_direction(tmp_kittyhack_db):
    later = DateTimeUtil.get_utc_date_string(1_700_010_000.0)
    earlier = DateTimeUtil.get_utc_date_string(1_700_000_000.0)
    record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=later,
        rfid="RFIDX",
        cat_name="X",
        event_type=EventType.CAT_WENT_OUTSIDE,
    )
    out_s, in_s = pair_duration(tmp_kittyhack_db, "RFIDX", "in", earlier)
    assert out_s is None and in_s is None

    same = DateTimeUtil.get_utc_date_string(1_700_020_000.0)
    out_s, in_s = pair_duration(tmp_kittyhack_db, "RFIDX", "out", same)
    assert out_s is None and in_s is None


def test_purge_photos_does_not_delete_visit_stats(tmp_kittyhack_db):
    created = DateTimeUtil.get_utc_date_string(1_700_000_000.0)
    record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=created,
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.CAT_WENT_INSIDE,
    )
    assert VisitStatsRepo.count(tmp_kittyhack_db) == 1

    import sqlite3

    conn = sqlite3.connect(tmp_kittyhack_db)
    for i in range(3):
        conn.execute(
            "INSERT INTO events (block_id, created_at, event_type, rfid, deleted) "
            "VALUES (?, ?, ?, ?, 0)",
            (i, created, EventType.CAT_WENT_INSIDE, "RFID001"),
        )
    conn.commit()
    conn.close()

    result = EventsRepo.purge_excess_photos(tmp_kittyhack_db, max_count=1)
    assert result.success
    assert VisitStatsRepo.count(tmp_kittyhack_db) == 1


def test_backfill_from_events_is_idempotent(tmp_kittyhack_db):
    CatsRepo.db_add_new_cat(tmp_kittyhack_db, "Mia", "RFID001", "")
    created_out = DateTimeUtil.get_utc_date_string(1_700_000_000.0)
    created_in = DateTimeUtil.get_utc_date_string(1_700_003_600.0)

    import sqlite3

    conn = sqlite3.connect(tmp_kittyhack_db)
    conn.execute(
        "INSERT INTO events (block_id, created_at, event_type, rfid, deleted) "
        "VALUES (1, ?, ?, 'RFID001', 0)",
        (created_out, EventType.CAT_WENT_OUTSIDE),
    )
    conn.execute(
        "INSERT INTO events (block_id, created_at, event_type, rfid, deleted) "
        "VALUES (2, ?, ?, 'RFID001', 0)",
        (created_in, EventType.CAT_WENT_INSIDE),
    )
    conn.commit()
    conn.close()

    first = backfill_from_events(tmp_kittyhack_db)
    assert first.success
    assert VisitStatsRepo.count(tmp_kittyhack_db) == 2
    assert VisitStatsRepo.get_meta(tmp_kittyhack_db, "backfilled") == "1"

    second = backfill_from_events(tmp_kittyhack_db)
    assert second.success
    assert second.message == "already_backfilled"
    assert VisitStatsRepo.count(tmp_kittyhack_db) == 2

    rows = VisitStatsRepo.query_range(
        tmp_kittyhack_db,
        "2000-01-01 00:00:00.0000+00:00",
        "2100-01-01 00:00:00.0000+00:00",
    )
    assert rows[1]["duration_outside_s"] == 3600
    assert rows[0]["cat_name"] == "Mia"


def test_build_dashboard_kpis_and_activity_by_hour(tmp_kittyhack_db):
    tz = ZoneInfo("UTC")
    base = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)
    record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=DateTimeUtil.get_utc_date_string(base.timestamp()),
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.CAT_WENT_OUTSIDE,
    )
    record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=DateTimeUtil.get_utc_date_string((base + timedelta(hours=2)).timestamp()),
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.CAT_WENT_INSIDE,
    )
    record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=DateTimeUtil.get_utc_date_string((base + timedelta(hours=3)).timestamp()),
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.MOTION_OUTSIDE_WITH_MOUSE,
        additional_verdict_infos=[EventType.ENTRY_PER_CAT_DENIED],
    )

    payload = build_dashboard(
        tmp_kittyhack_db,
        range_key="custom",
        bucket_key="hour",
        cat_filter=CAT_FILTER_ALL,
        custom_start=datetime(2026, 8, 12, tzinfo=tz).date(),
        custom_end=datetime(2026, 8, 12, tzinfo=tz).date(),
        tz=tz,
    )
    assert payload["empty"] is False
    assert payload["kpis"]["entries"] == 1
    assert payload["kpis"]["exits"] == 1
    assert payload["kpis"]["prey"] == 1
    assert payload["kpis"]["attempts"] == 1
    assert payload["kpis"]["denied_entry"] == 1
    assert payload["cats"][0]["name"] == "Mia"
    assert payload["cats"][0]["location"] == "inside"
    by_hour = payload["activity_by_hour"]
    assert by_hour["labels"] == [f"{h:02d}" for h in range(24)]
    assert sum(by_hour["counts"]) == 2  # only passed events
    assert by_hour["counts"][10] == 1
    assert by_hour["counts"][12] == 1
    assert "heatmap" not in payload
    assert payload["kpis"]["other_entry"] == 0
    assert payload["kpis"]["other_exit"] == 0


def test_activity_by_hour_sums_across_weekdays(tmp_kittyhack_db):
    tz = ZoneInfo("UTC")
    # Wednesday and Thursday, same hour of day.
    for stamp in (
        datetime(2026, 8, 12, 7, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 13, 7, 30, tzinfo=timezone.utc),
    ):
        record_motion_conclusion(
            tmp_kittyhack_db,
            created_at=DateTimeUtil.get_utc_date_string(stamp.timestamp()),
            rfid="RFID001",
            cat_name="Mia",
            event_type=EventType.CAT_WENT_OUTSIDE,
        )

    payload = build_dashboard(
        tmp_kittyhack_db,
        range_key="custom",
        custom_start=datetime(2026, 8, 12, tzinfo=tz).date(),
        custom_end=datetime(2026, 8, 13, tzinfo=tz).date(),
        tz=tz,
    )
    counts = payload["activity_by_hour"]["counts"]
    assert len(counts) == 24
    assert counts[7] == 2
    assert sum(counts) == 2


def test_per_cat_cards_sort_by_passages_then_name(tmp_kittyhack_db):
    tz = ZoneInfo("UTC")
    events = [
        (datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc), "RFIDMIA", "Mia", EventType.CAT_WENT_OUTSIDE),
        (datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc), "RFIDLEO", "Leo", EventType.CAT_WENT_OUTSIDE),
        (datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc), "RFIDLEO", "Leo", EventType.CAT_WENT_INSIDE),
        (datetime(2026, 8, 12, 11, 0, tzinfo=timezone.utc), "RFIDZOE", "Zoe", EventType.CAT_WENT_OUTSIDE),
        (datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc), "RFIDANN", "Ann", EventType.CAT_WENT_OUTSIDE),
    ]
    for stamp, rfid, name, event_type in events:
        record_motion_conclusion(
            tmp_kittyhack_db,
            created_at=DateTimeUtil.get_utc_date_string(stamp.timestamp()),
            rfid=rfid,
            cat_name=name,
            event_type=event_type,
        )

    payload = build_dashboard(
        tmp_kittyhack_db,
        range_key="custom",
        custom_start=datetime(2026, 8, 12, tzinfo=tz).date(),
        custom_end=datetime(2026, 8, 12, tzinfo=tz).date(),
        tz=tz,
    )
    assert [(cat["name"], cat["key"]) for cat in payload["cats"]] == [
        ("Leo", "RFIDLEO"),
        ("Ann", "RFIDANN"),
        ("Mia", "RFIDMIA"),
        ("Zoe", "RFIDZOE"),
    ]


def test_hidden_cat_keys_roundtrip_drops_invalid():
    raw = dump_hidden_cat_keys(["RFID001", "__unknown__", "bad key", ""])
    assert parse_hidden_cat_keys(raw) == {"RFID001", "__unknown__"}
    assert parse_hidden_cat_keys("not-json") == set()
    assert parse_hidden_cat_keys(None) == set()


def test_build_dashboard_empty_without_history(tmp_kittyhack_db):
    payload = build_dashboard(tmp_kittyhack_db, range_key="7d")
    assert payload["empty"] is True
    assert payload["has_history"] is False


def test_infer_other_access_from_same_direction():
    assert infer_other_access("out", "out") == "in"
    assert infer_other_access("in", "in") == "out"
    assert infer_other_access("in", "out") is None
    assert infer_other_access("out", None) is None


def test_same_direction_passages_infer_other_door_and_skip_duration(tmp_kittyhack_db):
    tz = ZoneInfo("UTC")
    base = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    # Left via flap, came back through a window, left via flap again, then came in via flap.
    record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=DateTimeUtil.get_utc_date_string(base.timestamp()),
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.CAT_WENT_OUTSIDE,
    )
    record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=DateTimeUtil.get_utc_date_string((base + timedelta(hours=3)).timestamp()),
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.CAT_WENT_OUTSIDE,
    )
    record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=DateTimeUtil.get_utc_date_string((base + timedelta(hours=4)).timestamp()),
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.CAT_WENT_INSIDE,
    )

    rows = VisitStatsRepo.query_range(
        tmp_kittyhack_db,
        "2000-01-01 00:00:00.0000+00:00",
        "2100-01-01 00:00:00.0000+00:00",
        "RFID001",
    )
    assert len(rows) == 3
    assert rows[1]["duration_inside_s"] is None
    assert rows[1]["duration_outside_s"] is None
    assert rows[2]["duration_outside_s"] == 3600

    payload = build_dashboard(
        tmp_kittyhack_db,
        range_key="custom",
        bucket_key="hour",
        cat_filter=CAT_FILTER_ALL,
        custom_start=datetime(2026, 8, 12, tzinfo=tz).date(),
        custom_end=datetime(2026, 8, 12, tzinfo=tz).date(),
        tz=tz,
    )
    assert payload["kpis"]["exits"] == 2
    assert payload["kpis"]["entries"] == 1
    assert payload["kpis"]["other_entry"] == 1
    assert payload["kpis"]["other_exit"] == 0
    assert payload["cats"][0]["other_access"] == 1
    assert payload["cats"][0]["location"] == "inside"


def _visit_rows(database, rfid="RFID001"):
    return VisitStatsRepo.query_range(database, _VISIT_RANGE[0], _VISIT_RANGE[1], rfid)


def test_manual_location_rejects_bad_input(tmp_kittyhack_db):
    result = apply_manual_location(
        tmp_kittyhack_db, cat_rfid="", cat_name="Mia", direction="in"
    )
    assert result.success is False
    result = apply_manual_location(
        tmp_kittyhack_db, cat_rfid="RFID001", cat_name="Mia", direction="sideways"
    )
    assert result.success is False
    assert VisitStatsRepo.count(tmp_kittyhack_db) == 0


def test_manual_location_first_mark_flips_in_place(tmp_kittyhack_db):
    t0 = DateTimeUtil.get_utc_date_string(1_700_000_000.0)
    t1 = DateTimeUtil.get_utc_date_string(1_700_000_002.0)
    inserted = apply_manual_location(
        tmp_kittyhack_db,
        cat_rfid="RFID001",
        cat_name="Mia",
        direction="in",
        created_at=t0,
    )
    assert inserted.success and inserted.message == "inserted"
    rows = _visit_rows(tmp_kittyhack_db)
    assert len(rows) == 1
    assert rows[0]["conclusion"] == MANUAL_LOCATION_CONCLUSION
    assert rows[0]["passed"] == 1
    assert rows[0]["direction"] == "in"
    assert rows[0]["duration_outside_s"] is None
    assert rows[0]["duration_inside_s"] is None
    assert rows[0]["source_block_id"] is None

    flipped = apply_manual_location(
        tmp_kittyhack_db,
        cat_rfid="RFID001",
        cat_name="Mia",
        direction="out",
        created_at=t1,
    )
    assert flipped.message == "updated"
    rows = _visit_rows(tmp_kittyhack_db)
    assert len(rows) == 1
    assert rows[0]["direction"] == "out"
    assert rows[0]["created_at"] == t1
    assert rows[0]["duration_inside_s"] is None
    assert rows[0]["duration_outside_s"] is None


def test_manual_location_closes_stay_and_reverts_within_5s(tmp_kittyhack_db):
    t0 = DateTimeUtil.get_utc_date_string(1_700_000_000.0)
    t1 = DateTimeUtil.get_utc_date_string(1_700_000_000.0 + 3600)
    t2 = DateTimeUtil.get_utc_date_string(1_700_000_000.0 + 3600 + 3)
    record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=t0,
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.CAT_WENT_OUTSIDE,
    )
    marked = apply_manual_location(
        tmp_kittyhack_db,
        cat_rfid="RFID001",
        cat_name="Mia",
        direction="in",
        created_at=t1,
    )
    assert marked.message == "inserted"
    rows = _visit_rows(tmp_kittyhack_db)
    assert len(rows) == 2
    assert rows[1]["duration_outside_s"] == 3600
    payload = build_dashboard(tmp_kittyhack_db, range_key="all", tz=ZoneInfo("UTC"))
    assert payload["kpis"]["entries"] == 1
    assert payload["kpis"]["exits"] == 1
    assert payload["cats"][0]["location"] == "inside"

    reverted = apply_manual_location(
        tmp_kittyhack_db,
        cat_rfid="RFID001",
        cat_name="Mia",
        direction="out",
        created_at=t2,
    )
    assert reverted.message == "reverted"
    rows = _visit_rows(tmp_kittyhack_db)
    assert len(rows) == 1
    assert rows[0]["direction"] == "out"
    assert rows[0]["conclusion"] == EventType.CAT_WENT_OUTSIDE
    assert VisitStatsRepo.get_last_passed(tmp_kittyhack_db, "RFID001")["direction"] == "out"
    payload = build_dashboard(tmp_kittyhack_db, range_key="all", tz=ZoneInfo("UTC"))
    assert payload["kpis"]["entries"] == 0
    assert payload["kpis"]["exits"] == 1
    assert payload["cats"][0]["location"] == "outside"


def test_manual_location_keeps_period_after_5s(tmp_kittyhack_db):
    t0 = DateTimeUtil.get_utc_date_string(1_700_000_000.0)
    t1 = DateTimeUtil.get_utc_date_string(1_700_000_000.0 + 3600)
    t_edge = DateTimeUtil.get_utc_date_string(1_700_000_000.0 + 3600 + 5)
    record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=t0,
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.CAT_WENT_OUTSIDE,
    )
    apply_manual_location(
        tmp_kittyhack_db,
        cat_rfid="RFID001",
        cat_name="Mia",
        direction="in",
        created_at=t1,
    )
    kept = apply_manual_location(
        tmp_kittyhack_db,
        cat_rfid="RFID001",
        cat_name="Mia",
        direction="out",
        created_at=t_edge,
    )
    assert kept.message == "inserted"
    rows = _visit_rows(tmp_kittyhack_db)
    assert len(rows) == 3
    assert rows[1]["duration_outside_s"] == 3600
    assert rows[2]["duration_inside_s"] == 5
    assert rows[2]["conclusion"] == MANUAL_LOCATION_CONCLUSION
    assert VisitStatsRepo.get_last_passed(tmp_kittyhack_db, "RFID001")["direction"] == "out"


def test_manual_location_same_direction_is_noop(tmp_kittyhack_db):
    t0 = DateTimeUtil.get_utc_date_string(1_700_000_000.0)
    t1 = DateTimeUtil.get_utc_date_string(1_700_000_010.0)
    record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=t0,
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.CAT_WENT_OUTSIDE,
    )
    same = apply_manual_location(
        tmp_kittyhack_db,
        cat_rfid="RFID001",
        cat_name="Mia",
        direction="out",
        created_at=t1,
    )
    assert same.message == "unchanged"
    rows = _visit_rows(tmp_kittyhack_db)
    assert len(rows) == 1
    assert rows[0]["created_at"] == t0


def test_manual_location_does_not_drop_a_recent_flap_passage(tmp_kittyhack_db):
    t0 = DateTimeUtil.get_utc_date_string(1_700_000_000.0)
    t1 = DateTimeUtil.get_utc_date_string(1_700_000_002.0)
    t2 = DateTimeUtil.get_utc_date_string(1_700_000_004.0)
    record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=t0,
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.CAT_WENT_OUTSIDE,
    )
    record_motion_conclusion(
        tmp_kittyhack_db,
        created_at=t1,
        rfid="RFID001",
        cat_name="Mia",
        event_type=EventType.CAT_WENT_INSIDE,
    )
    marked = apply_manual_location(
        tmp_kittyhack_db,
        cat_rfid="RFID001",
        cat_name="Mia",
        direction="out",
        created_at=t2,
    )
    assert marked.message == "inserted"
    rows = _visit_rows(tmp_kittyhack_db)
    assert len(rows) == 3
    assert rows[1]["conclusion"] == EventType.CAT_WENT_INSIDE
    assert rows[2]["conclusion"] == MANUAL_LOCATION_CONCLUSION
    assert rows[2]["duration_inside_s"] == 2
