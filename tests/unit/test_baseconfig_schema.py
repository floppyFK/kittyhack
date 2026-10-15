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
    db = tmp_path / "kittyhack.db"
    cfg = tmp_path / "config.ini"
    cfg.write_text(
        "[Settings]\n"
        "timezone = Europe/Berlin\n"
        "language = en\n"
        "simulate_kittyflap = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KITTYHACK_DATABASE_PATH", str(db))
    monkeypatch.setenv("KITTYHACK_REMOTE_CONFIGFILE", str(tmp_path / "config.remote.ini"))
    monkeypatch.setattr(baseconfig, "CONFIGFILE", str(cfg))
    monkeypatch.chdir(tmp_path)
    with caplog.at_level("WARNING"):
        baseconfig.load_config()
    assert not cfg.exists()
    assert "SIMULATE_KITTYFLAP" not in baseconfig.CONFIG
    assert baseconfig.CONFIG["TIMEZONE"] == "Europe/Berlin"
    assert any("simulate_kittyflap" in r.message.lower() for r in caplog.records)


def test_ini_values_migrate_into_database(tmp_path, monkeypatch):
    db = tmp_path / "kittyhack.db"
    cfg = tmp_path / "config.ini"
    cfg.write_text(
        "[Settings]\n"
        "language = de\n"
        "mouse_threshold = 42.5\n"
        "min_pictures_to_analyze = 10\n"
        "elements_per_page = not-a-number\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KITTYHACK_DATABASE_PATH", str(db))
    monkeypatch.setenv("KITTYHACK_REMOTE_CONFIGFILE", str(tmp_path / "config.remote.ini"))
    monkeypatch.setattr(baseconfig, "CONFIGFILE", str(cfg))
    monkeypatch.chdir(tmp_path)
    baseconfig.load_config()
    assert not cfg.exists()
    assert db.is_file()
    assert baseconfig.CONFIG["LANGUAGE"] == "de"
    assert baseconfig.CONFIG["MOUSE_THRESHOLD"] == 42.5
    assert baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] == 3.0
    assert baseconfig.CONFIG["ELEMENTS_PER_PAGE"] == 20
    assert baseconfig.CONFIG["KITTYHACK_DATABASE_PATH"] == str(db)


def test_remote_only_settings_survive_synced_ini(tmp_path, monkeypatch):
    db = tmp_path / "kittyhack.db"
    remote = tmp_path / "config.remote.ini"
    remote.write_text(
        "[Settings]\nremote_target_host = 192.168.1.70\nremote_control_port = 8888\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KITTYHACK_DATABASE_PATH", str(db))
    monkeypatch.setenv("KITTYHACK_REMOTE_CONFIGFILE", str(remote))
    monkeypatch.setattr(baseconfig, "CONFIGFILE", str(tmp_path / "missing.ini"))
    monkeypatch.setattr(baseconfig, "is_remote_mode", lambda: True)
    monkeypatch.chdir(tmp_path)
    baseconfig.load_config()
    assert not remote.exists()
    assert baseconfig.CONFIG["REMOTE_TARGET_HOST"] == "192.168.1.70"

    ini = tmp_path / "config.ini"
    ini.write_text("[Settings]\nlanguage = de\nmouse_threshold = 55\n", encoding="utf-8")
    monkeypatch.setattr(baseconfig, "CONFIGFILE", str(ini))
    baseconfig.load_config()
    assert not ini.exists()
    assert baseconfig.CONFIG["LANGUAGE"] == "de"
    assert baseconfig.CONFIG["MOUSE_THRESHOLD"] == 55.0
    assert baseconfig.CONFIG["REMOTE_TARGET_HOST"] == "192.168.1.70"
