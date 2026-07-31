"""DateTimeUtil helpers."""

from datetime import date, datetime

from src.helper import DateTimeUtil


def test_format_date_minmax_start_and_end():
    d = date(2024, 1, 15)
    start = DateTimeUtil.format_date_minmax(d, to_start=True)
    end = DateTimeUtil.format_date_minmax(d, to_start=False)
    assert start.startswith("2024-01-15 00:00:00")
    assert end.startswith("2024-01-15 23:59:59")


def test_get_utc_date_string_has_offset():
    s = DateTimeUtil.get_utc_date_string(0.0)
    assert "+00:00" in s
    assert s.startswith("1970-01-01")
