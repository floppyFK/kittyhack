"""Model server URL selection in Kittyhack."""

from src.baseconfig import CONFIG
from src.model.model_server_api import (
    PUBLIC_MODEL_SERVER_URL,
    normalize_model_server_host,
    resolved_model_server_base_url,
)
from src.model.remote_trainer import RemoteModelTrainer


def test_public_model_server_is_the_default(tmp_config_ini):
    assert CONFIG["MODEL_SERVER_MODE"] == "public"
    assert CONFIG["MODEL_SERVER_PORT"] == 8000
    assert resolved_model_server_base_url() == PUBLIC_MODEL_SERVER_URL
    assert RemoteModelTrainer.base_url() == PUBLIC_MODEL_SERVER_URL


def test_custom_model_server_url(tmp_config_ini):
    CONFIG["MODEL_SERVER_MODE"] = "custom"
    CONFIG["MODEL_SERVER_HOST"] = "192.168.1.50"
    CONFIG["MODEL_SERVER_PORT"] = 8123
    assert resolved_model_server_base_url() == "http://192.168.1.50:8123"

    CONFIG["MODEL_SERVER_HOST"] = "192.168."
    assert resolved_model_server_base_url() == ""
    CONFIG["MODEL_SERVER_HOST"] = "192"
    assert resolved_model_server_base_url() == ""
    CONFIG["MODEL_SERVER_HOST"] = "192.168.1"
    assert resolved_model_server_base_url() == ""
    CONFIG["MODEL_SERVER_HOST"] = "192.168.1.31"
    assert resolved_model_server_base_url() == "http://192.168.1.31:8123"
    CONFIG["MODEL_SERVER_HOST"] = "bad host"
    assert normalize_model_server_host("bad host") is None
    assert resolved_model_server_base_url() == ""


def test_local_model_server_url_reads_install_config(tmp_config_ini, tmp_path, monkeypatch):
    install = tmp_path / "kittyhack-model-server"
    install.mkdir()
    (install / "config.ini").write_text("[server]\nport = 9000\n", encoding="utf-8")
    monkeypatch.setattr("src.paths.model_server_root", lambda: str(install))
    CONFIG["MODEL_SERVER_MODE"] = "local"
    assert resolved_model_server_base_url() == "http://127.0.0.1:9000"


def test_enqueue_reports_busy_server(tmp_config_ini, tmp_path, monkeypatch):
    zip_path = tmp_path / "train.zip"
    zip_path.write_bytes(b"zip")

    class Response:
        status_code = 409
        content = b'{"detail": "A training job is already running."}'

        def raise_for_status(self):
            raise RuntimeError("409")

    monkeypatch.setattr(
        "src.model.remote_trainer.requests.post",
        lambda *args, **kwargs: Response(),
    )
    code, detail = RemoteModelTrainer.enqueue_model_training(str(zip_path))
    assert code == "server_busy"
    assert detail == ""
