"""Visit statistics: classification, duration pairing, backfill, aggregation."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.database import CatsRepo, EventsRepo, VisitStatsRepo
from src.helper import DateTimeUtil, EventType
from src.statistics import (
    CAT_FILTER_ALL,
    backfill_from_events,
    build_dashboard,
    classify_visit,
    format_duration,
    pair_duration,
    record_motion_conclusion,
    resolve_bucket,
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


def test_build_dashboard_kpis_and_heatmap(tmp_kittyhack_db):
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
    heat_sum = sum(sum(row) for row in payload["heatmap"]["cells"])
    assert heat_sum == 2  # only passed events


def test_build_dashboard_empty_without_history(tmp_kittyhack_db):
    payload = build_dashboard(tmp_kittyhack_db, range_key="7d")
    assert payload["empty"] is True
    assert payload["has_history"] is False
