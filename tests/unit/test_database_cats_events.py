"""Cats / settings map against a temp SQLite DB."""

import json
import sqlite3

from src.database import CatsRepo, EventsRepo, ReturnDataCatDB


def test_add_cat_and_settings_map(tmp_kittyhack_db):
    result = CatsRepo.db_add_new_cat(
        tmp_kittyhack_db,
        name="Mia",
        rfid="RFID001",
        cat_image_path="",
        enable_prey_detection=True,
        allow_entry=True,
        allow_exit=False,
    )
    assert result.success

    names = CatsRepo.get_cat_name_rfid_dict(tmp_kittyhack_db)
    assert names["RFID001"] == "Mia"

    settings = CatsRepo.get_cat_settings_map(tmp_kittyhack_db)
    assert settings["RFID001"]["allow_entry"] is True
    assert settings["RFID001"]["allow_exit"] is False
    assert settings["RFID001"]["enable_prey_detection"] is True

    tags = CatsRepo.db_get_all_rfid_tags(tmp_kittyhack_db)
    assert "RFID001" in tags

    hidden = CatsRepo.db_add_new_cat(
        tmp_kittyhack_db,
        name="Hidden",
        rfid="RFID002",
        cat_image_path="",
        show_location=False,
    )
    assert hidden.success
    df = CatsRepo.db_get_cats(tmp_kittyhack_db, ReturnDataCatDB.all_except_photos)
    row = df[df["rfid"] == "RFID002"].iloc[0]
    assert int(row["show_location"]) == 0
    visible = df[df["rfid"] == "RFID001"].iloc[0]
    assert int(visible["show_location"]) == 1


def test_empty_db_settings_map(tmp_kittyhack_db):
    assert CatsRepo.get_cat_settings_map(tmp_kittyhack_db) == {}
    assert CatsRepo.get_cat_names_list(tmp_kittyhack_db) == []


def test_motion_frame_timeline_round_trip_and_action_update(tmp_kittyhack_db):
    frames = [
        {"at": "2024-06-01 12:00:00+00:00", "pid": 10},
        {"at": "2024-06-01 12:00:01+00:00", "objects": []},
    ]
    with sqlite3.connect(tmp_kittyhack_db) as conn:
        conn.execute(
            "INSERT INTO motion_timeline (block_id, timeline_json, frames_json) "
            "VALUES (?, ?, ?)",
            (7, "[]", json.dumps(frames)),
        )

    assert EventsRepo.db_get_motion_frames(tmp_kittyhack_db, 7) == frames

    result = EventsRepo.write_motion_timeline(
        tmp_kittyhack_db,
        7,
        [{"action": "inside_opened", "at": "2024-06-01 12:00:00+00:00"}],
    )
    assert result.success
    assert EventsRepo.db_get_motion_frames(tmp_kittyhack_db, 7) == frames


def test_block_object_max(tmp_kittyhack_db):
    rows = [
        (1, 101, json.dumps({"detected_objects": [{"object_name": "Person", "probability": 91.0}]})),
        (2, 101, json.dumps({"detected_objects": [{"object_name": "Mia", "probability": 66.0}]})),
        (3, 101, json.dumps({"detected_objects": [{"object_name": "Mia", "probability": 52.0}]})),
        (4, 102, json.dumps({"detected_objects": [{"object_name": "Leo", "probability": 95.0}]})),
        (5, 103, json.dumps({"detected_objects": [{"object_name": "Person", "probability": 88.0}]})),
        (6, 104, json.dumps({"detected_objects": []})),
    ]
    con = sqlite3.connect(tmp_kittyhack_db)
    con.executemany(
        "INSERT INTO events (id, block_id, created_at, event_type, rfid, event_text, deleted) "
        "VALUES (?, ?, '2026-10-31 05:00:00', 'motion_outside_only', '', ?, 0)",
        rows,
    )
    con.execute("UPDATE events SET deleted = 1 WHERE block_id = 103")
    con.commit()
    con.close()

    result = EventsRepo.db_get_block_object_max(tmp_kittyhack_db, [101, 102, 103, 104], ["mia", "Leo", "person"])
    assert result == {101: {"person": 91.0, "mia": 66.0}, 102: {"leo": 95.0}}
    assert EventsRepo.db_get_block_object_max(tmp_kittyhack_db, [], ["person"]) == {}
