"""Schema load/save roundtrip and simulate-key removal."""

from pathlib import Path

import src.baseconfig as baseconfig
from src.baseconfig import AllowedToEnter, SETTINGS_SCHEMA


def test_schema_has_no_simulate_kittyflap():
    keys = {s.key for s in SETTINGS_SCHEMA}
    assert "SIMULATE_KITTYFLAP" not in keys
    assert "REQUIRE_OUTSIDE_PIR_FOR_CAMERA_ENTRY" in keys


def test_create_default_and_reload(tmp_config_ini):
    assert Path(tmp_config_ini).is_file()
    assert "TIMEZONE" in baseconfig.CONFIG
    assert "SIMULATE_KITTYFLAP" not in baseconfig.CONFIG
    assert baseconfig.CONFIG["ALLOWED_TO_ENTER"] == AllowedToEnter.ALL


def test_save_load_roundtrip(tmp_config_ini, monkeypatch):
    baseconfig.CONFIG["MOUSE_THRESHOLD"] = 42.5
    baseconfig.CONFIG["ELEMENTS_PER_PAGE"] = 15
    baseconfig.save_config()
    baseconfig.CONFIG.clear()
    baseconfig.load_config()
    assert baseconfig.CONFIG["MOUSE_THRESHOLD"] == 42.5
    assert baseconfig.CONFIG["ELEMENTS_PER_PAGE"] == 15


def test_legacy_simulate_key_removed_with_warning(tmp_path, monkeypatch, caplog):
    cfg = tmp_path / "config.ini"
    cfg.write_text(
        "[Settings]\n"
        "timezone = Europe/Berlin\n"
        "language = en\n"
        "simulate_kittyflap = true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(baseconfig, "CONFIGFILE", str(cfg))
    monkeypatch.chdir(tmp_path)
    with caplog.at_level("WARNING"):
        baseconfig.load_config()
    text = cfg.read_text(encoding="utf-8").lower()
    assert "simulate_kittyflap" not in text
    assert "SIMULATE_KITTYFLAP" not in baseconfig.CONFIG
    assert any("simulate_kittyflap" in r.message.lower() for r in caplog.records)
