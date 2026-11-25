"""Initial remote sync must ship a consistent settings table."""

import json
import os
import shutil
import sqlite3
from io import BytesIO

import pytest

import src.baseconfig as baseconfig
from src.database import DatabaseCore


def _settings_rows(path):
    conn = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "settings" not in tables:
            return None
        return dict(conn.execute("SELECT key, value FROM settings"))
    finally:
        conn.close()


def _event_note(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT note FROM events").fetchone()[0]
    finally:
        conn.close()


def _write_settings(path, rows, event_note=None):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        [(key, json.dumps(value), "2020-01-01T00:00:00Z") for key, value in rows.items()],
    )
    if event_note is not None:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO events (note) VALUES (?)", (event_note,))
    conn.commit()
    conn.close()


def _point_at_database(monkeypatch, tmp_path, dest):
    monkeypatch.setenv("KITTYHACK_DATABASE_PATH", str(dest))
    monkeypatch.setenv("KITTYHACK_REMOTE_CONFIGFILE", str(tmp_path / "config.remote.ini"))
    monkeypatch.setattr(baseconfig, "CONFIGFILE", str(tmp_path / "config.ini"))


def test_consistent_read_includes_wal_settings_raw_copy_does_not(tmp_path):
    db = tmp_path / "kittyhack.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("INSERT INTO events (note) VALUES ('event')")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute(
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('YOLO_MODEL', ?, ?)",
        (json.dumps("flap-model"), "2020-01-01T00:00:00Z"),
    )
    # Leave this connection open so closing it cannot checkpoint the WAL for us.

    raw = tmp_path / "raw.db"
    shutil.copy(db, raw)

    good = tmp_path / "good.db"
    with DatabaseCore.consistent_database_read(str(db)):
        shutil.copy(db, good)
    conn.close()

    assert _event_note(raw) == "event"
    assert _settings_rows(raw) is None
    assert _settings_rows(good)["YOLO_MODEL"] == json.dumps("flap-model")
    assert _event_note(good) == "event"


def test_checkpoint_busy_writes_no_archive(tmp_path):
    db = tmp_path / "kittyhack.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("INSERT INTO events (note) VALUES ('event')")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    reader = sqlite3.connect(db, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT note FROM events").fetchall()
    conn.execute(
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('YOLO_MODEL', ?, ?)",
        (json.dumps("flap-model"), "2020-01-01T00:00:00Z"),
    )

    archive = BytesIO()
    try:
        with pytest.raises(RuntimeError, match="uncheckpointed WAL frames"):
            DatabaseCore.write_sync_tar([(str(db), "kittyhack.db")], archive)
        assert archive.getvalue() == b""
    finally:
        reader.close()
        conn.close()


def test_install_synced_database_drops_sidecars_and_keeps_remote_only(tmp_path, monkeypatch):
    from src.remote.control_client import install_synced_database

    dest = tmp_path / "kittyhack.db"
    source = tmp_path / "from-flap.db"
    _write_settings(
        dest,
        {"REMOTE_TARGET_HOST": "10.0.0.8", "YOLO_MODEL": "remote-model", "LANGUAGE": "de"},
        event_note="local",
    )
    (tmp_path / "kittyhack.db-wal").write_bytes(b"default-settings-wal")
    (tmp_path / "kittyhack.db-shm").write_bytes(b"default-settings-shm")
    _write_settings(
        source,
        {"REMOTE_TARGET_HOST": "192.0.2.1", "YOLO_MODEL": "flap-model", "LANGUAGE": "en"},
        event_note="from-flap",
    )
    ini = tmp_path / "config.ini"
    ini.write_text("[Settings]\nyolo_model = ini-model\n", encoding="utf-8")
    (tmp_path / "config.ini.lock").write_text("lock", encoding="utf-8")
    _point_at_database(monkeypatch, tmp_path, dest)

    install_synced_database(str(source))

    rows = _settings_rows(dest)
    assert rows["YOLO_MODEL"] == json.dumps("flap-model")
    assert rows["LANGUAGE"] == json.dumps("en")
    assert rows["REMOTE_TARGET_HOST"] == json.dumps("10.0.0.8")
    assert _event_note(dest) == "from-flap"
    assert not (tmp_path / "kittyhack.db-wal").exists()
    assert not (tmp_path / "kittyhack.db-shm").exists()
    assert not (tmp_path / "kittyhack.db-journal").exists()
    assert not (tmp_path / "kittyhack.db.pre-sync").exists()
    assert (dest.parent / (dest.name + ".remote_synced")).is_file()
    assert not ini.exists()
    assert (tmp_path / "config.ini.bak").read_text(encoding="utf-8").startswith("[Settings]")
    assert (tmp_path / "config.ini.bak.lock").read_text(encoding="utf-8") == "lock"
    assert not source.exists()


def test_install_synced_database_rejects_empty_settings(tmp_path, monkeypatch):
    from src.remote.control_client import install_synced_database

    dest = tmp_path / "kittyhack.db"
    source = tmp_path / "from-flap.db"
    _write_settings(
        dest,
        {"REMOTE_TARGET_HOST": "10.0.0.8", "LANGUAGE": "de"},
        event_note="local",
    )
    (tmp_path / "kittyhack.db-wal").write_bytes(b"old-wal")
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("INSERT INTO events (note) VALUES ('from-flap')")
    conn.commit()
    conn.close()
    ini = tmp_path / "config.ini"
    ini.write_text("[Settings]\nlanguage = en\n", encoding="utf-8")
    _point_at_database(monkeypatch, tmp_path, dest)

    with pytest.raises(RuntimeError, match="no settings"):
        install_synced_database(str(source))

    rows = _settings_rows(dest)
    assert rows == {
        "REMOTE_TARGET_HOST": json.dumps("10.0.0.8"),
        "LANGUAGE": json.dumps("de"),
    }
    assert _event_note(dest) == "local"
    assert not (tmp_path / "kittyhack.db-wal").exists()
    assert not (tmp_path / "kittyhack.db.pre-sync").exists()
    assert not (dest.parent / (dest.name + ".remote_synced")).exists()
    assert ini.is_file()
    assert not (tmp_path / "config.ini.bak").exists()
