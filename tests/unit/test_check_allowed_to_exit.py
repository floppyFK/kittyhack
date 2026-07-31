"""check_allowed_to_exit schedule against CONFIG."""

from src.baseconfig import AllowedToExit
from src.helper import check_allowed_to_exit


def test_exit_denied_globally(tmp_config_ini, monkeypatch):
    import src.baseconfig as baseconfig

    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.DENY
    assert check_allowed_to_exit() is False


def test_exit_allow_with_no_ranges(tmp_config_ini, monkeypatch):
    import src.baseconfig as baseconfig

    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    for i in (1, 2, 3):
        baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False
    assert check_allowed_to_exit() is True


def test_exit_allow_outside_configured_range(tmp_config_ini, monkeypatch):
    import src.baseconfig as baseconfig
    from datetime import datetime
    from zoneinfo import ZoneInfo

    baseconfig.CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit.ALLOW
    baseconfig.CONFIG["ALLOWED_TO_EXIT_RANGE1"] = True
    baseconfig.CONFIG["ALLOWED_TO_EXIT_RANGE1_FROM"] = "09:00"
    baseconfig.CONFIG["ALLOWED_TO_EXIT_RANGE1_TO"] = "10:00"
    baseconfig.CONFIG["ALLOWED_TO_EXIT_RANGE2"] = False
    baseconfig.CONFIG["ALLOWED_TO_EXIT_RANGE3"] = False

    fixed = datetime(2024, 6, 1, 15, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr("src.helper.datetime", _FixedDateTime)
    assert check_allowed_to_exit() is False
