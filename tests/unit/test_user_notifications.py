"""UserNotifications persistence, mute, and legacy file format."""

from __future__ import annotations

import json

from src.baseconfig import UserNotifications


def _reset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    UserNotifications.notifications = []
    UserNotifications.muted_ids = []
    path = tmp_path / "notifications.json"
    if path.exists():
        path.unlink()
    return path


def test_add_roundtrip_uses_wrapped_file_format(tmp_path, monkeypatch):
    path = _reset(tmp_path, monkeypatch)
    nid = UserNotifications.add("Header", "Body", type="warning", id="n1", muteable=True)
    assert nid == "n1"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["muted_ids"] == []
    assert data["notifications"][0]["id"] == "n1"
    assert data["notifications"][0]["muteable"] is True

    UserNotifications.notifications = []
    UserNotifications.muted_ids = ["stale"]
    UserNotifications.load()
    assert UserNotifications.get_by_id("n1")["header"] == "Header"
    assert UserNotifications.muted_ids == []


def test_load_legacy_list_format(tmp_path, monkeypatch):
    path = _reset(tmp_path, monkeypatch)
    path.write_text(
        json.dumps([{"id": "legacy", "header": "H", "message": "M", "type": "default"}]),
        encoding="utf-8",
    )
    UserNotifications.load()
    assert UserNotifications.get_by_id("legacy")["message"] == "M"
    assert UserNotifications.muted_ids == []


def test_mute_skips_future_adds_and_survives_reload(tmp_path, monkeypatch):
    path = _reset(tmp_path, monkeypatch)
    nid = UserNotifications.ID_INSIDE_UNLOCK_HELD_AFTER_MAX_TIME
    UserNotifications.add("Persistent", "msg", id=nid, muteable=True)
    assert UserNotifications.mute(nid) is True
    assert UserNotifications.get_by_id(nid) is None
    assert UserNotifications.is_muted(nid)

    assert UserNotifications.add("Persistent", "again", id=nid, muteable=True) is None
    assert UserNotifications.get_all() == []

    UserNotifications.notifications = [{"id": "other"}]
    UserNotifications.muted_ids = []
    UserNotifications.load()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert nid in data["muted_ids"]
    assert UserNotifications.is_muted(nid)
    assert UserNotifications.add("Persistent", "third", id=nid, skip_if_id_exists=True) is None


def test_clear_keeps_muted_ids(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    UserNotifications.add("A", "a", id="keep-mute")
    UserNotifications.mute("keep-mute")
    UserNotifications.add("B", "b", id="visible")
    UserNotifications.clear()
    assert UserNotifications.get_all() == []
    assert UserNotifications.is_muted("keep-mute")


def test_clear_muted_unmutes_all_and_allows_add(tmp_path, monkeypatch):
    path = _reset(tmp_path, monkeypatch)
    UserNotifications.mute("first")
    UserNotifications.mute("second")
    assert UserNotifications.get_muted_ids() == ["first", "second"]
    assert UserNotifications.clear_muted() == 2
    assert UserNotifications.get_muted_ids() == []
    assert UserNotifications.clear_muted() == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["muted_ids"] == []
    assert UserNotifications.add("Again", "msg", id="first") == "first"
