"""Restore an uploaded Kittyhack database without touching a bad file."""

import os
import sqlite3

import src.baseconfig as baseconfig
from src.database import DatabaseCore


def _write_db(path, note, table="events"):
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute(f"INSERT INTO {table} (note) VALUES (?)", (note,))
    conn.commit()
    conn.close()


def _read_note(path, table="events"):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(f"SELECT note FROM {table}").fetchone()
    finally:
        conn.close()
    return row[0]


def test_replace_database_file_keeps_a_backup_and_drops_sidecars(tmp_path):
    current = tmp_path / "kittyhack.db"
    upload = tmp_path / "upload.db"
    _write_db(current, "current")
    _write_db(upload, "uploaded")
    (tmp_path / "kittyhack.db-wal").write_bytes(b"old-wal")
    (tmp_path / "kittyhack.db-shm").write_bytes(b"old-shm")

    result = DatabaseCore.replace_database_file(str(upload), str(current))

    assert result.success
    assert result.message.endswith(".db")
    assert os.path.isfile(result.message)
    assert _read_note(current) == "uploaded"
    assert _read_note(result.message) == "current"
    assert not (tmp_path / "kittyhack.db-wal").exists()
    assert not (tmp_path / "kittyhack.db-shm").exists()


def test_replace_database_file_rejects_non_sqlite_without_changing_destination(tmp_path):
    current = tmp_path / "kittyhack.db"
    upload = tmp_path / "notes.db"
    _write_db(current, "current")
    upload.write_text("this is not a database", encoding="utf-8")

    result = DatabaseCore.replace_database_file(str(upload), str(current))

    assert not result.success
    assert result.message == "invalid_database"
    assert _read_note(current) == "current"
    assert list(tmp_path.glob("kittyhack_backup_*.db")) == []


def test_replace_database_file_rejects_sqlite_without_kittyhack_tables(tmp_path):
    current = tmp_path / "kittyhack.db"
    upload = tmp_path / "other.db"
    _write_db(current, "current")
    _write_db(upload, "other", table="foo")

    result = DatabaseCore.replace_database_file(str(upload), str(current))

    assert result.message == "invalid_database"
    assert _read_note(current) == "current"


def test_replace_database_file_installs_when_destination_is_missing(tmp_path):
    upload = tmp_path / "upload.db"
    dest = tmp_path / "kittyhack.db"
    _write_db(upload, "uploaded", table="settings")

    result = DatabaseCore.replace_database_file(str(upload), str(dest))

    assert result.success
    assert result.message == ""
    assert _read_note(dest, table="settings") == "uploaded"


def test_legacy_database_restore_preserves_current_settings(
    tmp_config_ini, tmp_path
):
    current = str(tmp_config_ini)
    upload = tmp_path / "legacy.db"
    _write_db(upload, "legacy event")
    baseconfig.CONFIG["LANGUAGE"] = "de"
    baseconfig.CONFIG["MOUSE_THRESHOLD"] = 42.5
    assert baseconfig.save_config()

    result = DatabaseCore.replace_database_file(str(upload), current)

    assert result.success
    assert baseconfig.settings_count(current) == 0
    assert baseconfig.ensure_settings_persisted()
    assert baseconfig.settings_count(current) > 0
    conn = sqlite3.connect(current)
    try:
        rows = dict(conn.execute("SELECT key, value FROM settings"))
    finally:
        conn.close()
    assert rows["LANGUAGE"] == '"de"'
    assert rows["MOUSE_THRESHOLD"] == "42.5"
