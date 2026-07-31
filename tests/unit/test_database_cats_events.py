"""Cats / settings map against a temp SQLite DB."""

from src.database import CatsRepo, DbMigrations


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


def test_empty_db_settings_map(tmp_kittyhack_db):
    assert CatsRepo.get_cat_settings_map(tmp_kittyhack_db) == {}
    assert CatsRepo.get_cat_names_list(tmp_kittyhack_db) == []
