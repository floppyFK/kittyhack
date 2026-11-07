"""Local model server config, dataset checks, and HTTP contract."""

from __future__ import annotations

import io
import sys
import threading
import zipfile
from pathlib import Path

import pytest

MODEL_SERVER_DIR = Path(__file__).resolve().parents[2] / "model-server"


def _load_model_server():
    root = str(MODEL_SERVER_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)
    for name in ("config", "train", "server"):
        module = sys.modules.get(name)
        module_file = str(getattr(module, "__file__", "") or "")
        if module is not None and root not in module_file:
            raise RuntimeError(f"{name} is already imported from {module_file}")
    import config as model_config
    import train as model_train

    return model_config, model_train


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _dataset_zip(*, nested: str = "") -> bytes:
    prefix = f"{nested}/" if nested else ""
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
        b"\x00\x00\x00\x03\x00\x01\xa0\xb3\xb3\xa0\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return _zip_bytes(
        {
            f"{prefix}classes.txt": b"cat\n",
            f"{prefix}images/a.png": png,
            f"{prefix}labels/a.txt": b"0 0.5 0.5 0.2 0.2\n",
        }
    )


def test_load_settings_defaults_and_overrides(tmp_path):
    config, _train = _load_model_server()
    missing = config.load_settings(tmp_path / "missing.ini")
    assert missing.port == 8000
    assert missing.epochs == 60
    assert missing.device == "auto"

    path = tmp_path / "config.ini"
    path.write_text(
        "[server]\nport = 8123\nignored = yes\n\n"
        "[training]\nepochs = 12\nimgsz = 480\nbatch = -1\n"
        "patience = 0\nworkers = 0\ndevice = CUDA:0\n",
        encoding="utf-8",
    )
    loaded = config.load_settings(path)
    assert loaded.port == 8123
    assert loaded.epochs == 12
    assert loaded.imgsz == 480
    assert loaded.batch == -1
    assert loaded.patience == 0
    assert loaded.workers == 0
    assert loaded.device == "cuda:0"

    path.write_text("[server]\nport = 0\n\n[training]\nimgsz = 100\ndevice = gpu\n", encoding="utf-8")
    fallback = config.load_settings(path)
    assert fallback.port == 8000
    assert fallback.imgsz == 320
    assert fallback.device == "auto"

    path.write_text("[training]\ndevice = MPS\n", encoding="utf-8")
    assert config.load_settings(path).device == "mps"


def test_validate_and_split_dataset(tmp_path):
    _config, train = _load_model_server()
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    assert train.validate_training_zip(bad)

    nested = tmp_path / "nested.zip"
    nested.write_bytes(_dataset_zip(nested="export"))
    assert train.validate_training_zip(nested) is None
    train.safe_extract(nested, tmp_path / "work")
    assert (tmp_path / "work" / "classes.txt").is_file()
    assert (tmp_path / "work" / "images" / "a.png").is_file()

    train.split_dataset(tmp_path / "work")
    assert (tmp_path / "work" / "train" / "images" / "a.png").is_file()
    assert (tmp_path / "work" / "val" / "images" / "a.png").is_file()

    unsafe = tmp_path / "unsafe.zip"
    unsafe.write_bytes(_zip_bytes({"../classes.txt": b"cat\n", "images/a.png": b"x", "labels/a.txt": b"0 0 0 0 0\n"}))
    assert train.validate_training_zip(unsafe)


def test_runtime_device_label():
    _config, train = _load_model_server()
    assert train.describe_runtime_device(type("Device", (), {"type": "cpu"})()) == "CPU"
    assert train.describe_runtime_device(type("Device", (), {"type": "cuda", "index": 0})()).startswith("GPU")
    assert train.describe_runtime_device(type("Device", (), {"type": "mps"})()) == "GPU (Apple MPS)"


def _fake_torch(*, cuda: bool, mps: bool | None):
    backends = type("Backends", (), {})()
    if mps is not None:
        backends.mps = type("Mps", (), {"is_available": staticmethod(lambda: mps)})()
    return type(
        "Torch",
        (),
        {"cuda": type("Cuda", (), {"is_available": staticmethod(lambda: cuda)})(), "backends": backends},
    )()


def test_resolve_train_device():
    _config, train = _load_model_server()
    mac = _fake_torch(cuda=False, mps=True)
    assert train.resolve_train_device("auto", mac) == "mps"
    assert train.resolve_train_device("auto", _fake_torch(cuda=True, mps=False)) is None
    assert train.resolve_train_device("auto", _fake_torch(cuda=False, mps=False)) is None
    assert train.resolve_train_device("auto", _fake_torch(cuda=False, mps=None)) is None
    assert train.resolve_train_device("cpu", mac) == "cpu"
    assert train.resolve_train_device("cuda:0", mac) == "cuda:0"


def test_status_page_shows_epoch_log_and_device():
    pytest.importorskip("fastapi")
    _load_model_server()
    import server as model_server
    from config import ServerSettings

    progress = model_server.TrainingProgress()
    progress.begin()
    progress.update(phase="training", epoch=2, epochs=10, device="CPU", message="Finished epoch 2 of 10")
    page = model_server._status_page(
        ServerSettings(device="cpu"),
        [{"status": "training", "model_name": "flap", "yolo_variant": "yolov8n.pt", "image_size": 320, "updated_at": ""}],
        progress.snapshot(),
        "CPU",
    )
    assert "Epoch 2 of 10" in page
    assert "Runs on CPU." in page
    assert "Finished epoch 2 of 10" in page
    assert "flap" in page


def test_resolve_training_request_fields():
    _config, train = _load_model_server()
    assert train.normalize_pretrained("s", "") == "yolov8s.pt"
    assert train.normalize_pretrained("nope", "yolov8m.pt") == "yolov8m.pt"
    assert train.normalize_pretrained("", "") == "yolov8n.pt"
    assert train.resolve_image_size("", "", 320) == 320
    assert train.resolve_image_size("480", "", 320) == 480
    assert train.resolve_image_size("100", "640", 320) == 640


@pytest.fixture
def api_app(tmp_path):
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    del fastapi
    _config, train = _load_model_server()
    import server as model_server

    seen = {}
    hold = threading.Event()

    def trainer(request):
        seen["image_size"] = request.image_size
        seen["pretrained"] = request.pretrained
        seen["request"] = request
        if not hold.is_set() and seen.get("block"):
            hold.wait(5)
        if request.cancel_event.is_set():
            raise train.Cancelled()
        model_server_zip = _zip_bytes({"model.pt": b"pt", "labels.txt": b"cat\n", "info.json": b"{}"})
        request.model_zip.write_bytes(model_server_zip)

    config_file = tmp_path / "config.ini"
    config_file.write_text("[server]\nport = 8000\n\n[training]\nimgsz = 640\nepochs = 4\n", encoding="utf-8")
    app = model_server.create_app(tmp_path, trainer=trainer)
    app.state.seen = seen
    app.state.hold = hold
    yield app
    app.state.cancel_event.set()
    app.state.hold.set()
    for thread in list(app.state.threads):
        thread.join(timeout=5)


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def test_status_page_shows_idle_server_and_training_settings(api_app):
    client = _client(api_app)
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "Kittyhack model server" in page.text
    assert "Idle" in page.text
    assert "No training is running." in page.text
    assert ">Epochs</th><td>4</td>" in page.text
    assert ">Image size</th><td>640</td>" in page.text


def test_upload_status_download_and_ack(api_app):
    client = _client(api_app)
    status = client.get("/server_status")
    assert status.status_code == 200
    assert status.json()["maintenance"] is False

    rejected = client.post(
        "/upload",
        files={"file": ("data.zip", b"nope", "application/zip")},
    )
    assert rejected.status_code == 400

    created = client.post(
        "/upload",
        files={"file": ("data.zip", _dataset_zip(), "application/zip")},
        data={"yolo_model_variant": "s", "image_size": "480", "model_name": "flap"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["yolo_model_variant"] == "yolov8s.pt"
    assert body["image_size"] == 480
    assert body["job_token"]
    for thread in list(api_app.state.threads):
        thread.join(timeout=5)
    assert api_app.state.seen["pretrained"] == "yolov8s.pt"
    assert api_app.state.seen["image_size"] == 480

    polled = client.get(f"/status/{body['job_id']}")
    assert polled.status_code == 200
    payload = polled.json()
    assert payload["status"] == "completed"
    result_id = payload["result_id"]

    downloaded = client.get(f"/download/{result_id}")
    assert downloaded.status_code == 200
    assert downloaded.headers["X-Model-SHA256"]
    assert downloaded.headers["X-Delete-Token"]
    assert int(downloaded.headers["X-Model-Size"]) == len(downloaded.content)

    acked = client.post(
        f"/download/{result_id}/ack",
        json={
            "delete_token": downloaded.headers["X-Delete-Token"],
            "sha256": downloaded.headers["X-Model-SHA256"],
            "size_bytes": int(downloaded.headers["X-Model-Size"]),
        },
    )
    assert acked.status_code == 200
    assert acked.json()["message"] == "deleted"
    again = client.post(
        f"/download/{result_id}/ack",
        json={"delete_token": downloaded.headers["X-Delete-Token"], "sha256": downloaded.headers["X-Model-SHA256"]},
    )
    assert again.status_code == 200
    assert again.json()["message"] == "already_deleted"


def test_second_upload_is_rejected_while_training(api_app):
    api_app.state.seen["block"] = True
    client = _client(api_app)
    first = client.post(
        "/upload",
        files={"file": ("data.zip", _dataset_zip(), "application/zip")},
    )
    assert first.status_code == 200
    started = threading.Event()

    def _wait():
        for _ in range(50):
            row = api_app.state.store.get(first.json()["job_id"])
            if row and row["status"] in {"pending", "training"}:
                started.set()
                return
            threading.Event().wait(0.02)

    _wait()
    assert started.is_set()
    second = client.post(
        "/upload",
        files={"file": ("data.zip", _dataset_zip(), "application/zip")},
    )
    assert second.status_code == 409
    cancelled = client.post(f"/cancel/{first.json()['job_id']}")
    assert cancelled.status_code == 200
    api_app.state.hold.set()
    for thread in list(api_app.state.threads):
        thread.join(timeout=5)
    assert api_app.state.store.get(first.json()["job_id"])["status"] == "cancelled"


def test_image_size_falls_back_to_config(api_app):
    client = _client(api_app)
    created = client.post(
        "/upload",
        files={"file": ("data.zip", _dataset_zip(), "application/zip")},
        data={"image_size": "100"},
    )
    assert created.status_code == 200
    assert created.json()["image_size"] == 640
    for thread in list(api_app.state.threads):
        thread.join(timeout=5)
    assert api_app.state.seen["image_size"] == 640
