"""Visit classification, duration pairing, backfill, and dashboard aggregation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from src.database import CatsRepo, VisitStatsRepo
from src.helper import DateTimeUtil, EventType, Result

PASSAGE_IN = {
    EventType.CAT_WENT_INSIDE,
    EventType.CAT_WENT_PROBABLY_INSIDE,
    EventType.CAT_WENT_INSIDE_WITH_MOUSE,
}
PASSAGE_OUT = {EventType.CAT_WENT_OUTSIDE}
PREY_CONCLUSIONS = {
    EventType.CAT_WENT_INSIDE_WITH_MOUSE,
    EventType.MOTION_OUTSIDE_WITH_MOUSE,
}
ATTEMPT_ENTRY_NO_PASS = {
    EventType.MOTION_OUTSIDE_ONLY,
    EventType.MOTION_OUTSIDE_WITH_MOUSE,
}
MOTION_CONCLUSIONS = PASSAGE_IN | PASSAGE_OUT | ATTEMPT_ENTRY_NO_PASS

RANGE_PRESETS = ("24h", "7d", "30d", "12m", "all", "custom")
BUCKETS = ("auto", "hour", "day", "week", "month")

CAT_FILTER_ALL = "all"
CAT_FILTER_UNKNOWN = "__unknown__"


def parse_event_flags(event_type: str, additional: Iterable[str] | None = None) -> tuple[str, set[str]]:
    """Split a comma-separated event_type plus extra flags into (primary, all flags)."""
    parts = [p.strip() for p in str(event_type or "").split(",") if p.strip()]
    flags = set(parts)
    if additional:
        flags.update(str(x).strip() for x in additional if str(x).strip())
    primary = parts[0] if parts else ""
    return primary, flags


def classify_visit(event_type: str, additional: Iterable[str] | None = None) -> dict | None:
    """Map a motion conclusion (+ verdict flags) to visit_stats columns, or None to skip."""
    primary, flags = parse_event_flags(event_type, additional)
    denied_entry = EventType.ENTRY_PER_CAT_DENIED in flags
    denied_exit = EventType.EXIT_PER_CAT_DENIED in flags
    prey = bool(flags & PREY_CONCLUSIONS) or primary in PREY_CONCLUSIONS

    if primary not in MOTION_CONCLUSIONS and not denied_entry and not denied_exit:
        return None

    direction = None
    passed = 0
    if primary in PASSAGE_IN:
        direction = "in"
        passed = 1
    elif primary in PASSAGE_OUT:
        direction = "out"
        passed = 1
    elif primary in ATTEMPT_ENTRY_NO_PASS:
        direction = "in"

    attempt_no_pass = 0
    if primary in ATTEMPT_ENTRY_NO_PASS or denied_entry or denied_exit:
        attempt_no_pass = 1

    return {
        "conclusion": primary or (EventType.ENTRY_PER_CAT_DENIED if denied_entry else EventType.EXIT_PER_CAT_DENIED),
        "direction": direction,
        "passed": passed,
        "prey": int(prey),
        "denied_entry": int(denied_entry),
        "denied_exit": int(denied_exit),
        "attempt_no_pass": attempt_no_pass,
        "uncertain_entry": int(primary == EventType.CAT_WENT_PROBABLY_INSIDE),
    }


def parse_utc_datetime(value) -> datetime | None:
    """Parse a stored UTC datetime string (or datetime) to an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def pair_duration(
    database: str,
    cat_rfid: str,
    direction: str,
    created_at: str,
) -> tuple[float | None, float | None]:
    """Return (duration_outside_s, duration_inside_s) vs the previous opposite passage."""
    if not cat_rfid or direction not in ("in", "out"):
        return None, None
    last = VisitStatsRepo.get_last_passed(database, cat_rfid)
    if not last:
        return None, None
    last_dir = last.get("direction")
    if last_dir == direction or last_dir not in ("in", "out"):
        return None, None
    now_dt = parse_utc_datetime(created_at)
    last_dt = parse_utc_datetime(last.get("created_at"))
    if now_dt is None or last_dt is None:
        return None, None
    delta = (now_dt - last_dt).total_seconds()
    if delta <= 0:
        return None, None
    if direction == "in" and last_dir == "out":
        return delta, None
    if direction == "out" and last_dir == "in":
        return None, delta
    return None, None


def record_motion_conclusion(
    database: str,
    *,
    created_at: str,
    rfid: str | None,
    cat_name: str | None,
    event_type: str,
    additional_verdict_infos: Iterable[str] | None = None,
    source_block_id: int | None = None,
) -> Result:
    """Classify a concluded motion block and persist a visit_stats row."""
    classified = classify_visit(event_type, additional_verdict_infos)
    if classified is None:
        return Result(True, "ignored")
    if not created_at:
        return Result(True, "ignored")
    rfid_norm = (str(rfid).strip() if rfid else "") or None
    name_norm = (str(cat_name).strip() if cat_name else "") or None
    dur_out = dur_in = None
    if classified["passed"] and rfid_norm and classified["direction"] in ("in", "out"):
        try:
            dur_out, dur_in = pair_duration(
                database, rfid_norm, classified["direction"], created_at
            )
        except Exception as e:
            logging.warning(f"[VISIT_STATS] Duration pairing failed: {e}")
    row = {
        **classified,
        "created_at": created_at,
        "cat_rfid": rfid_norm,
        "cat_name": name_norm,
        "duration_outside_s": dur_out,
        "duration_inside_s": dur_in,
        "source_block_id": source_block_id,
    }
    try:
        return VisitStatsRepo.insert(database, row)
    except Exception as e:
        error_message = f"[VISIT_STATS] Failed to record visit: {e}"
        logging.error(error_message)
        return Result(False, error_message)


def backfill_from_events(database: str) -> Result:
    """One-shot import of existing motion blocks into visit_stats (idempotent)."""
    if VisitStatsRepo.get_meta(database, "backfilled") == "1":
        return Result(True, "already_backfilled")
    try:
        name_by_rfid = CatsRepo.get_cat_name_rfid_dict(database)
    except Exception:
        name_by_rfid = {}
    blocks = VisitStatsRepo.fetch_motion_blocks_for_backfill(database)
    inserted = 0
    skipped = 0
    for block in blocks:
        rfid = block.get("rfid")
        cat_name = name_by_rfid.get(rfid) if rfid else None
        result = record_motion_conclusion(
            database,
            created_at=block.get("created_at") or "",
            rfid=rfid,
            cat_name=cat_name,
            event_type=block.get("event_type") or "",
            source_block_id=block.get("block_id"),
        )
        if result.success:
            if result.message in ("ignored", "duplicate"):
                skipped += 1
            else:
                inserted += 1
        else:
            skipped += 1
    meta = VisitStatsRepo.set_meta(database, "backfilled", "1")
    if not meta.success:
        return meta
    logging.info(
        f"[VISIT_STATS] Backfill complete: inserted={inserted}, skipped={skipped}, blocks={len(blocks)}"
    )
    return Result(True, f"inserted={inserted}")


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values)) / float(len(values))


def format_duration(seconds: float | None, *, d: str = "d", h: str = "h", m: str = "m") -> str:
    """Compact duration label, e.g. ``2d 4h 12m``."""
    if seconds is None:
        return "—"
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    if total < 0:
        return "—"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}{d}")
    if hours:
        parts.append(f"{hours}{h}")
    if minutes or not parts:
        parts.append(f"{minutes}{m}")
    return " ".join(parts)


def resolve_bucket(range_key: str, bucket_key: str) -> str:
    """Resolve Auto grouping from the selected range."""
    if bucket_key and bucket_key != "auto":
        return bucket_key
    return {
        "24h": "hour",
        "7d": "day",
        "30d": "day",
        "12m": "week",
        "all": "month",
        "custom": "day",
    }.get(range_key, "day")


def _as_local_date(value, tz: ZoneInfo):
    """Coerce a date/datetime to a timezone-aware local datetime at that calendar day."""
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    return datetime.combine(value, datetime.min.time(), tzinfo=tz)


def range_bounds(
    range_key: str,
    tz: ZoneInfo,
    *,
    custom_start=None,
    custom_end=None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Inclusive local-time window as UTC-aware datetimes."""
    now_local = now.astimezone(tz) if now else datetime.now(tz)
    if range_key == "custom" and custom_start is not None and custom_end is not None:
        start_local = _as_local_date(custom_start, tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_local = _as_local_date(custom_end, tz).replace(
            hour=23, minute=59, second=59, microsecond=999999
        )
        if end_local < start_local:
            start_local, end_local = (
                end_local.replace(hour=0, minute=0, second=0, microsecond=0),
                start_local.replace(hour=23, minute=59, second=59, microsecond=999999),
            )
        return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

    end_local = now_local
    if range_key == "24h":
        start_local = now_local - timedelta(hours=24)
    elif range_key == "7d":
        start_local = now_local - timedelta(days=7)
    elif range_key == "30d":
        start_local = now_local - timedelta(days=30)
    elif range_key == "12m":
        start_local = now_local - timedelta(days=365)
    else:
        start_local = datetime(2000, 1, 1, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def utc_bound_strings(start_utc: datetime, end_utc: datetime) -> tuple[str, str]:
    """Format UTC bounds in the same style as stored ``created_at`` values."""
    return (
        DateTimeUtil.get_utc_date_string(start_utc.timestamp()),
        DateTimeUtil.get_utc_date_string(end_utc.timestamp()),
    )


def bucket_key_for(dt_local: datetime, granularity: str) -> str:
    """Stable bucket id in local time."""
    if granularity == "hour":
        return dt_local.strftime("%Y-%m-%d %H:00")
    if granularity == "week":
        iso = dt_local.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if granularity == "month":
        return dt_local.strftime("%Y-%m")
    return dt_local.strftime("%Y-%m-%d")


def iter_bucket_keys(start_utc: datetime, end_utc: datetime, tz: ZoneInfo, granularity: str) -> list[str]:
    """Every bucket from start to end (inclusive of start, up to end)."""
    start_local = start_utc.astimezone(tz)
    end_local = end_utc.astimezone(tz)
    keys: list[str] = []
    seen: set[str] = set()
    if granularity == "hour":
        cursor = start_local.replace(minute=0, second=0, microsecond=0)
        step = timedelta(hours=1)
    elif granularity == "week":
        cursor = start_local - timedelta(days=start_local.weekday())
        cursor = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
        step = timedelta(days=7)
    elif granularity == "month":
        cursor = start_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        step = None
    else:
        cursor = start_local.replace(hour=0, minute=0, second=0, microsecond=0)
        step = timedelta(days=1)

    safety = 0
    while cursor <= end_local and safety < 5000:
        key = bucket_key_for(cursor, granularity)
        if key not in seen:
            seen.add(key)
            keys.append(key)
        if granularity == "month":
            year = cursor.year + (1 if cursor.month == 12 else 0)
            month = 1 if cursor.month == 12 else cursor.month + 1
            cursor = cursor.replace(year=year, month=month)
        else:
            cursor = cursor + step
        safety += 1
    return keys


def _row_local_dt(row: dict, tz: ZoneInfo) -> datetime | None:
    dt = parse_utc_datetime(row.get("created_at"))
    if dt is None:
        return None
    return dt.astimezone(tz)


def _cat_key(row: dict) -> str:
    rfid = row.get("cat_rfid")
    if rfid:
        return str(rfid)
    return CAT_FILTER_UNKNOWN


def _cat_label(row: dict, unknown_label: str) -> str:
    name = row.get("cat_name")
    if name:
        return str(name)
    rfid = row.get("cat_rfid")
    if rfid:
        return str(rfid)
    return unknown_label


def _typical_hhmm(seconds_past_midnight: list[int]) -> str | None:
    if not seconds_past_midnight:
        return None
    med = _median([float(s) for s in seconds_past_midnight])
    if med is None:
        return None
    total = int(round(med)) % 86400
    hh, rem = divmod(total, 3600)
    mm = rem // 60
    return f"{hh:02d}:{mm:02d}"


@dataclass
class DashboardLabels:
    """Pre-translated strings for the dashboard JSON payload."""

    unknown: str = "Unknown"
    entries: str = "Entries"
    exits: str = "Exits"
    in_label: str = "In"
    out_label: str = "Out"
    prey: str = "Prey"
    prey_entered: str = "Entered with prey"
    prey_blocked: str = "Prey blocked"
    attempts_entry: str = "Entry attempts"
    attempts_exit: str = "Denied exits"
    median_outside: str = "Median outside"
    median_inside: str = "Median inside"
    weekdays: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    duration_d: str = "d"
    duration_h: str = "h"
    duration_m: str = "m"
    inside: str = "Inside"
    outside: str = "Outside"
    location_unknown: str = "Unknown"


def build_dashboard(
    database: str,
    *,
    range_key: str = "30d",
    bucket_key: str = "auto",
    cat_filter: str = CAT_FILTER_ALL,
    custom_start=None,
    custom_end=None,
    tz: ZoneInfo | None = None,
    labels: DashboardLabels | None = None,
    cat_thumbnails: dict[str, str] | None = None,
) -> dict:
    """Aggregate visit_stats into a JSON-serializable dashboard payload."""
    labels = labels or DashboardLabels()
    tz = tz or DateTimeUtil.get_timezone()
    granularity = resolve_bucket(range_key, bucket_key)
    start_utc, end_utc = range_bounds(
        range_key, tz, custom_start=custom_start, custom_end=custom_end
    )
    start_s, end_s = utc_bound_strings(start_utc, end_utc)
    rfid_filter = None if cat_filter in (None, "", CAT_FILTER_ALL) else cat_filter
    rows = VisitStatsRepo.query_range(database, start_s, end_s, rfid_filter)
    total_all_time = VisitStatsRepo.count(database)

    if not rows and total_all_time == 0:
        return {"empty": True, "has_history": False, "granularity": granularity}

    if range_key == "all" and rows:
        first_dt = parse_utc_datetime(rows[0].get("created_at"))
        if first_dt is not None:
            start_utc = first_dt

    bucket_ids = iter_bucket_keys(start_utc, end_utc, tz, granularity)
    if not bucket_ids:
        bucket_ids = [bucket_key_for(end_utc.astimezone(tz), granularity)]

    def empty_series():
        return {k: 0 for k in bucket_ids}

    def empty_dur_lists():
        return {k: [] for k in bucket_ids}

    cats_in_rows: dict[str, str] = {}
    for row in rows:
        cats_in_rows[_cat_key(row)] = _cat_label(row, labels.unknown)

    passages_in = {ck: empty_series() for ck in cats_in_rows}
    passages_out = {ck: empty_series() for ck in cats_in_rows}
    prey_entered = empty_series()
    prey_blocked = empty_series()
    attempts_entry = empty_series()
    attempts_exit = empty_series()
    dur_out_by_bucket = empty_dur_lists()
    dur_in_by_bucket = empty_dur_lists()
    heatmap = [[0 for _ in range(24)] for _ in range(7)]

    entries = exits = prey_n = attempts = denied_entry = denied_exit = uncertain = 0
    dur_out_all: list[float] = []
    dur_in_all: list[float] = []

    per_cat: dict[str, dict] = {}
    for ck, name in cats_in_rows.items():
        per_cat[ck] = {
            "rfid": None if ck == CAT_FILTER_UNKNOWN else ck,
            "name": name,
            "entries": 0,
            "exits": 0,
            "prey": 0,
            "attempts": 0,
            "uncertain": 0,
            "dur_out": [],
            "dur_in": [],
            "first_exit_by_day": {},
            "last_return_by_day": {},
        }

    for row in rows:
        local = _row_local_dt(row, tz)
        if local is None:
            continue
        bkey = bucket_key_for(local, granularity)
        ck = _cat_key(row)
        cat = per_cat.get(ck)
        if bkey not in prey_entered:
            continue

        passed = int(row.get("passed") or 0)
        direction = row.get("direction")
        prey = int(row.get("prey") or 0)
        attempt = int(row.get("attempt_no_pass") or 0)
        if int(row.get("uncertain_entry") or 0):
            uncertain += 1
            if cat:
                cat["uncertain"] += 1

        if passed and direction == "in":
            entries += 1
            passages_in.setdefault(ck, empty_series())
            if bkey in passages_in[ck]:
                passages_in[ck][bkey] += 1
            if cat:
                cat["entries"] += 1
                day = local.strftime("%Y-%m-%d")
                sod = local.hour * 3600 + local.minute * 60 + local.second
                prev = cat["last_return_by_day"].get(day)
                if prev is None or sod > prev:
                    cat["last_return_by_day"][day] = sod
        elif passed and direction == "out":
            exits += 1
            passages_out.setdefault(ck, empty_series())
            if bkey in passages_out[ck]:
                passages_out[ck][bkey] += 1
            if cat:
                cat["exits"] += 1
                day = local.strftime("%Y-%m-%d")
                sod = local.hour * 3600 + local.minute * 60 + local.second
                prev = cat["first_exit_by_day"].get(day)
                if prev is None or sod < prev:
                    cat["first_exit_by_day"][day] = sod

        if prey:
            prey_n += 1
            if cat:
                cat["prey"] += 1
            if passed and direction == "in":
                prey_entered[bkey] += 1
            else:
                prey_blocked[bkey] += 1

        if attempt:
            attempts += 1
            if cat:
                cat["attempts"] += 1
            if int(row.get("denied_exit") or 0):
                attempts_exit[bkey] += 1
                denied_exit += 1
            else:
                attempts_entry[bkey] += 1
                if int(row.get("denied_entry") or 0):
                    denied_entry += 1

        d_out = row.get("duration_outside_s")
        d_in = row.get("duration_inside_s")
        try:
            if d_out is not None and float(d_out) > 0:
                fv = float(d_out)
                dur_out_all.append(fv)
                dur_out_by_bucket[bkey].append(fv)
                if cat:
                    cat["dur_out"].append(fv)
        except (TypeError, ValueError):
            pass
        try:
            if d_in is not None and float(d_in) > 0:
                fv = float(d_in)
                dur_in_all.append(fv)
                dur_in_by_bucket[bkey].append(fv)
                if cat:
                    cat["dur_in"].append(fv)
        except (TypeError, ValueError):
            pass

        if passed:
            heatmap[local.weekday()][local.hour] += 1

    last_locations: dict[str, str] = {}
    for ck in per_cat:
        if ck == CAT_FILTER_UNKNOWN:
            continue
        last = VisitStatsRepo.get_last_passed(database, ck)
        if last and last.get("direction") in ("in", "out"):
            last_locations[ck] = "inside" if last["direction"] == "in" else "outside"

    cat_cards = []
    thumbs = cat_thumbnails or {}
    for ck, cat in sorted(per_cat.items(), key=lambda item: item[1]["name"].lower()):
        loc = last_locations.get(ck, "unknown")
        cat_cards.append(
            {
                "rfid": cat["rfid"],
                "name": cat["name"],
                "thumbnail": thumbs.get(cat["rfid"] or "", ""),
                "location": loc,
                "entries": cat["entries"],
                "exits": cat["exits"],
                "prey": cat["prey"],
                "attempts": cat["attempts"],
                "uncertain": cat["uncertain"],
                "avg_outside_s": _mean(cat["dur_out"]),
                "median_outside_s": _median(cat["dur_out"]),
                "avg_inside_s": _mean(cat["dur_in"]),
                "median_inside_s": _median(cat["dur_in"]),
                "longest_outside_s": max(cat["dur_out"]) if cat["dur_out"] else None,
                "typical_first_exit": _typical_hhmm(list(cat["first_exit_by_day"].values())),
                "typical_last_return": _typical_hhmm(list(cat["last_return_by_day"].values())),
            }
        )

    passage_datasets = []
    for ck, name in sorted(cats_in_rows.items(), key=lambda item: item[1].lower()):
        passage_datasets.append(
            {
                "label": f"{name} · {labels.in_label}",
                "stack": ck,
                "direction": "in",
                "data": [passages_in.get(ck, {}).get(k, 0) for k in bucket_ids],
            }
        )
        passage_datasets.append(
            {
                "label": f"{name} · {labels.out_label}",
                "stack": ck,
                "direction": "out",
                "data": [passages_out.get(ck, {}).get(k, 0) for k in bucket_ids],
            }
        )

    return {
        "empty": len(rows) == 0,
        "has_history": total_all_time > 0,
        "granularity": granularity,
        "kpis": {
            "entries": entries,
            "exits": exits,
            "uncertain_entries": uncertain,
            "prey": prey_n,
            "prey_entered": sum(prey_entered.values()),
            "prey_blocked": sum(prey_blocked.values()),
            "attempts": attempts,
            "denied_entry": denied_entry,
            "denied_exit": denied_exit,
            "avg_outside_s": _mean(dur_out_all),
            "median_outside_s": _median(dur_out_all),
            "avg_inside_s": _mean(dur_in_all),
            "median_inside_s": _median(dur_in_all),
            "avg_outside": format_duration(
                _mean(dur_out_all), d=labels.duration_d, h=labels.duration_h, m=labels.duration_m
            ),
            "median_outside": format_duration(
                _median(dur_out_all), d=labels.duration_d, h=labels.duration_h, m=labels.duration_m
            ),
            "avg_inside": format_duration(
                _mean(dur_in_all), d=labels.duration_d, h=labels.duration_h, m=labels.duration_m
            ),
            "median_inside": format_duration(
                _median(dur_in_all), d=labels.duration_d, h=labels.duration_h, m=labels.duration_m
            ),
        },
        "passages": {"labels": bucket_ids, "datasets": passage_datasets},
        "prey": {
            "labels": bucket_ids,
            "entered": [prey_entered[k] for k in bucket_ids],
            "blocked": [prey_blocked[k] for k in bucket_ids],
            "entered_label": labels.prey_entered,
            "blocked_label": labels.prey_blocked,
        },
        "attempts": {
            "labels": bucket_ids,
            "entry": [attempts_entry[k] for k in bucket_ids],
            "exit": [attempts_exit[k] for k in bucket_ids],
            "entry_label": labels.attempts_entry,
            "exit_label": labels.attempts_exit,
        },
        "durations": {
            "labels": bucket_ids,
            "outside": [_median(dur_out_by_bucket[k]) for k in bucket_ids],
            "inside": [_median(dur_in_by_bucket[k]) for k in bucket_ids],
            "outside_label": labels.median_outside,
            "inside_label": labels.median_inside,
        },
        "heatmap": {
            "weekdays": list(labels.weekdays),
            "hours": [f"{h:02d}" for h in range(24)],
            "cells": heatmap,
        },
        "cats": cat_cards,
        "i18n": {
            "entries": labels.entries,
            "exits": labels.exits,
            "prey": labels.prey,
            "inside": labels.inside,
            "outside": labels.outside,
            "location_unknown": labels.location_unknown,
        },
    }
