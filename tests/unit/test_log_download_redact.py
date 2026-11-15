"""Diagnostic database snapshots must not contain plaintext secrets."""

import json
import sqlite3

import pytest

import src.baseconfig as baseconfig


def _write_db(path, settings, extra_sql=None):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
        [(key, raw, "2026-01-01T00:00:00Z") for key, raw in settings],
    )
    if extra_sql:
        conn.executescript(extra_sql)
    conn.commit()
    conn.close()


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT key, value FROM settings"))
    finally:
        conn.close()


def test_redact_masks_nonempty_secrets_only(tmp_path):
    db = tmp_path / "snapshot.db"
    _write_db(
        db,
        [
            ("MQTT_PASSWORD", json.dumps("s3cret")),
            ("MQTT_USERNAME", json.dumps("")),
            ("EMAIL", json.dumps("a@b.c")),
            ("IP_CAMERA_URL", json.dumps("rtsp://user:pass@cam/stream")),
            ("LABELSTUDIO_API_TOKEN", json.dumps("tok")),
            ("MODEL_TRAINING_JOB_TOKEN", json.dumps("job")),
            ("MQTT_BROKER_ADDRESS", json.dumps("192.168.1.1")),
            ("LANGUAGE", json.dumps("de")),
        ],
        extra_sql="CREATE TABLE events (id INTEGER PRIMARY KEY, note TEXT); INSERT INTO events(note) VALUES ('keep');",
    )

    baseconfig.redact_sensitive_settings_in_database(str(db))

    rows = _rows(db)
    assert json.loads(rows["MQTT_PASSWORD"]) == "********"
    assert json.loads(rows["MQTT_USERNAME"]) == ""
    assert json.loads(rows["EMAIL"]) == "********"
    assert json.loads(rows["IP_CAMERA_URL"]) == "********"
    assert json.loads(rows["LABELSTUDIO_API_TOKEN"]) == "********"
    assert json.loads(rows["MODEL_TRAINING_JOB_TOKEN"]) == "********"
    assert json.loads(rows["MQTT_BROKER_ADDRESS"]) == "192.168.1.1"
    assert json.loads(rows["LANGUAGE"]) == "de"

    conn = sqlite3.connect(db)
    try:
        note = conn.execute("SELECT note FROM events").fetchone()[0]
    finally:
        conn.close()
    assert note == "keep"


def test_redact_leaves_null_and_missing_table(tmp_path):
    db = tmp_path / "snapshot.db"
    _write_db(db, [("MQTT_PASSWORD", "null")])
    baseconfig.redact_sensitive_settings_in_database(str(db))
    assert _rows(db)["MQTT_PASSWORD"] == "null"

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    baseconfig.redact_sensitive_settings_in_database(str(empty))
    baseconfig.redact_sensitive_settings_in_database(str(tmp_path / "missing.db"))


def test_redact_refuses_live_database(tmp_path, monkeypatch):
    db = tmp_path / "kittyhack.db"
    _write_db(db, [("MQTT_PASSWORD", json.dumps("keep-me"))])
    monkeypatch.setitem(baseconfig.CONFIG, "KITTYHACK_DATABASE_PATH", str(db))

    with pytest.raises(ValueError, match="live kittyhack database"):
        baseconfig.redact_sensitive_settings_in_database(str(db))

    assert json.loads(_rows(db)["MQTT_PASSWORD"]) == "keep-me"
