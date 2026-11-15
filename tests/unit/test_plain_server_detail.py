"""Parser for model-server upload error messages."""

import json

import requests

from src.model.remote_trainer import RemoteModelTrainer, plain_server_detail

_SIZE = "Provided zip file is too large. Maximum allowed upload size is 150MB"
_PICTURES = "Too many pictures in the training data. Maximum allowed pictures: 1000"


def test_plain_server_detail_size_and_picture_sentences():
    assert plain_server_detail({"detail": _SIZE}) == _SIZE
    assert plain_server_detail(json.dumps({"detail": _PICTURES}).encode()) == _PICTURES
    assert plain_server_detail({"detail": {"message": _PICTURES}}) == _PICTURES


def test_plain_server_detail_rejects_html_and_empty_bodies():
    assert plain_server_detail(b"") == ""
    assert plain_server_detail({"detail": ""}) == ""
    assert plain_server_detail(b"<html><body>413 Request Entity Too Large</body></html>") == ""
    assert plain_server_detail({"detail": "File <b>too large</b>"}) == ""
    assert plain_server_detail({"detail": "x" * 301}) == ""
    assert plain_server_detail("not-json") == ""


def test_enqueue_413_returns_server_detail(tmp_path, monkeypatch):
    zip_path = tmp_path / "training.zip"
    zip_path.write_bytes(b"not-a-real-zip")

    class _Response:
        status_code = 413
        content = json.dumps({"detail": _PICTURES}).encode()

        def raise_for_status(self):
            raise requests.HTTPError("413")

    monkeypatch.setattr(
        "src.model.remote_trainer.requests.post",
        lambda *args, **kwargs: _Response(),
    )

    code, message = RemoteModelTrainer.enqueue_model_training(str(zip_path))
    assert code == "file_too_large"
    assert message == _PICTURES


def test_enqueue_413_without_json_has_no_message(tmp_path, monkeypatch):
    zip_path = tmp_path / "training.zip"
    zip_path.write_bytes(b"not-a-real-zip")

    class _Response:
        status_code = 413
        content = b"<html>Request Entity Too Large</html>"

        def raise_for_status(self):
            raise requests.HTTPError("413")

    monkeypatch.setattr(
        "src.model.remote_trainer.requests.post",
        lambda *args, **kwargs: _Response(),
    )

    code, message = RemoteModelTrainer.enqueue_model_training(str(zip_path))
    assert code == "file_too_large"
    assert message == ""
