"""Unit tests for YOLO metadata, camera config, and ModelHandler helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.baseconfig as baseconfig
from src.model.camera_config import (
    _effective_camera_stream_config,
    _is_remote_internal_proxy_stream,
    _remote_control_disconnected,
    _remote_internal_proxy_url,
)
from src.model.detection import _parse_yolo_detection_results
from src.model.model_handler import ModelHandler
from src.model.remote_trainer import RemoteModelTrainer
from src.model.yolo_inference_worker import (
    configure_worker_compute,
    yolo_model_worker_process,
)
from src.model.yolo_model import YoloModel


# --- YoloModel ---------------------------------------------------------------


@pytest.fixture
def yolo_root(tmp_path, monkeypatch):
    root = tmp_path / "yolo"
    root.mkdir()
    monkeypatch.setattr(YoloModel, "BASE_DIR", str(root))
    return root


def _install_model(root: Path, dirname: str, *, job_id: str, **info_extra) -> Path:
    d = root / dirname
    d.mkdir()
    info = {
        "MODEL_NAME": dirname,
        "JOB_ID": job_id,
        "TIMESTAMP_UTC": "2024-01-02T03:04:05+00:00",
        "MODEL_IMAGE_SIZE": 416,
        "YOLO_MODEL_VARIANT": "s",
        "EFFECTIVE_FPS": 12.5,
        "EFFECTIVE_FPS_UPDATED_AT_UTC": "2024-01-02T04:00:00+00:00",
    }
    info.update(info_extra)
    (d / "info.json").write_text(json.dumps(info), encoding="utf-8")
    return d


def test_normalize_model_image_size():
    assert YoloModel.get_supported_image_sizes()[0] == 320
    assert YoloModel._normalize_model_image_size("416") == 416
    assert YoloModel._normalize_model_image_size("999") == 640
    assert YoloModel._normalize_model_image_size("100") == 320
    assert YoloModel._normalize_model_image_size("nope") == 320
    assert YoloModel._normalize_model_image_size(321) in YoloModel.get_supported_image_sizes()


def test_get_model_list_empty_and_populated(yolo_root):
    assert YoloModel.get_model_list() == []
    _install_model(yolo_root, "mia_v1", job_id="uid-1")
    models = YoloModel.get_model_list()
    assert len(models) == 1
    m = models[0]
    assert m["unique_id"] == "uid-1"
    assert m["model_image_size"] == 416
    assert m["yolo_variant"] == "yolov8s.pt"
    assert m["effective_fps"] == 12.5
    assert "Mia" in m["full_display_name"] or "mia" in m["full_display_name"].lower()


def test_get_path_size_update_rename_delete(yolo_root):
    _install_model(yolo_root, "cat_a", job_id="job-a", PRETRAINED_MODEL="custom.pt")
    assert YoloModel.get_model_path("missing") is None
    assert YoloModel.get_model_image_size("missing") is None

    path = YoloModel.get_model_path("job-a")
    assert path and path.endswith("cat_a")
    assert YoloModel.get_model_image_size("job-a") == 416

    assert YoloModel.update_model_metadata("job-a", {"EFFECTIVE_FPS": 9.0}) is True
    info = json.loads((yolo_root / "cat_a" / "info.json").read_text(encoding="utf-8"))
    assert info["EFFECTIVE_FPS"] == 9.0

    assert YoloModel.update_model_metadata("nope", {"x": 1}) is False
    assert YoloModel.update_model_metadata("job-a", "not-a-dict") is False  # type: ignore[arg-type]

    assert YoloModel.rename_model("job-a", "Renamed Cat") is True
    info = json.loads((yolo_root / "cat_a" / "info.json").read_text(encoding="utf-8"))
    assert info["MODEL_NAME"] == "Renamed Cat"
    assert YoloModel.rename_model("missing", "x") is False

    assert YoloModel.delete_model("job-a") is True
    assert not (yolo_root / "cat_a").exists()
    assert YoloModel.delete_model("job-a") is False


def test_get_model_list_without_info_json(yolo_root):
    (yolo_root / "bare").mkdir()
    models = YoloModel.get_model_list()
    assert len(models) == 1
    assert models[0]["directory"] == "bare"
    assert models[0]["unique_id"] is None


# --- camera_config -----------------------------------------------------------


def test_remote_internal_proxy_url(monkeypatch):
    monkeypatch.setattr("src.model.camera_config.is_remote_mode", lambda: False)
    assert _remote_internal_proxy_url() is None

    monkeypatch.setattr("src.model.camera_config.is_remote_mode", lambda: True)
    baseconfig.CONFIG["CAMERA_SOURCE"] = "ip_camera"
    assert _remote_internal_proxy_url() is None

    baseconfig.CONFIG["CAMERA_SOURCE"] = "internal"
    baseconfig.CONFIG["REMOTE_TARGET_HOST"] = ""
    assert _remote_internal_proxy_url() is None

    baseconfig.CONFIG["REMOTE_TARGET_HOST"] = "192.168.1.10"
    assert _remote_internal_proxy_url() == "http://192.168.1.10/video"

    baseconfig.CONFIG["REMOTE_TARGET_HOST"] = "https://flap.example"
    assert _remote_internal_proxy_url() == "https://flap.example/video"


def test_effective_camera_stream_and_proxy_detect(monkeypatch):
    monkeypatch.setattr("src.model.camera_config.is_remote_mode", lambda: False)
    baseconfig.CONFIG["CAMERA_SOURCE"] = "internal"
    baseconfig.CONFIG["IP_CAMERA_URL"] = ""
    assert _effective_camera_stream_config() == ("internal", "")
    assert _is_remote_internal_proxy_stream("ip_camera", "http://x/video") is False

    monkeypatch.setattr("src.model.camera_config.is_remote_mode", lambda: True)
    baseconfig.CONFIG["CAMERA_SOURCE"] = "internal"
    baseconfig.CONFIG["REMOTE_TARGET_HOST"] = "10.0.0.2"
    monkeypatch.setattr(
        "src.model.camera_config._remote_control_disconnected", lambda: False
    )
    src, url = _effective_camera_stream_config()
    assert src == "ip_camera" and url.endswith("/video")
    assert _is_remote_internal_proxy_stream(src, url) is True

    monkeypatch.setattr(
        "src.model.camera_config._remote_control_disconnected", lambda: True
    )
    assert _effective_camera_stream_config() == ("disconnected", "")


def test_remote_control_disconnected(monkeypatch):
    monkeypatch.setattr("src.model.camera_config.is_remote_mode", lambda: False)
    assert _remote_control_disconnected() is False

    monkeypatch.setattr("src.model.camera_config.is_remote_mode", lambda: True)
    baseconfig.CONFIG["REMOTE_TARGET_HOST"] = ""
    assert _remote_control_disconnected() is False

    baseconfig.CONFIG["REMOTE_TARGET_HOST"] = "host"

    class _Client:
        def ensure_started(self):
            return None

        def is_manual_disconnect(self):
            return True

        def wait_until_ready(self, timeout=0):
            return False

        def had_successful_connection(self):
            return True

        @classmethod
        def instance(cls):
            return cls()

    import sys
    import types

    fake_mod = types.ModuleType("src.remote.control_client")
    fake_mod.RemoteControlClient = _Client
    monkeypatch.setitem(sys.modules, "src.remote.control_client", fake_mod)
    # Also stub parent packages if needed
    if "src.remote" not in sys.modules:
        sys.modules["src.remote"] = types.ModuleType("src.remote")

    assert _remote_control_disconnected() is True


# --- ModelHandler light helpers ---------------------------------------------


def test_model_handler_device_and_labels(tmp_path, monkeypatch, tmp_kittyhack_db):
    baseconfig.CONFIG["KITTYHACK_DATABASE_PATH"] = tmp_kittyhack_db
    baseconfig.CONFIG["INFERENCE_DEVICE"] = "gpu"
    monkeypatch.setattr("src.model.model_handler.is_remote_mode", lambda: False)

    labels = tmp_path / "labels.txt"
    labels.write_text("prey\nmia\n", encoding="utf-8")
    modeldir = str(tmp_path)

    # Avoid CatsRepo hitting unexpected paths; DB is empty → [].
    h = ModelHandler(
        model="yolo",
        modeldir=modeldir,
        labelfile="labels.txt",
        model_image_size=320,
    )
    assert h.inference_device == "cpu"  # forced on target mode
    assert h.labels == ["prey", "mia"]
    assert h._uses_openvino_backend() is False
    assert h._resolved_inference_device() == "cpu"

    h.inference_device = "gpu"
    assert h._uses_openvino_backend() is True
    assert h._resolved_inference_device() == "intel:gpu"
    h.inference_device = "intel:npu"
    assert h._uses_openvino_backend() is True

    h.pause()
    assert h.paused is True
    assert h.get_run_state() is False
    h.resume()
    assert h.paused is False
    assert h.get_run_state() is True

    h._last_effective_fps = 8.0
    h._last_avg_inference_fps = 7.5
    h._last_fps_update_tm = 123.0
    assert h.get_effective_fps_snapshot()[0] == 8.0
    fps, avg, ts = h.get_fps_metrics_snapshot()
    assert fps == 8.0 and avg == 7.5 and ts == 123.0

    h.set_videostream_buffer_size(5)
    # No live stream → False
    assert h.check_videostream_status() is False


def test_get_model_threads_target_is_always_one_core(monkeypatch):
    import src.backend.model_runtime as mr

    monkeypatch.setattr(mr.multiprocessing, "cpu_count", lambda: 4)
    monkeypatch.setattr(mr, "is_remote_mode", lambda: False)
    assert mr._get_model_threads() == 1

    monkeypatch.setattr(mr, "is_remote_mode", lambda: True)
    assert mr._get_model_threads() == 4


def test_parse_yolo_below_threshold_still_returns_objects():
    import numpy as np

    class _Boxes:
        def __init__(self):
            self.xyxyn = [np.array([0.0, 0.0, 0.1, 0.1])]
            self.conf = [np.float32(0.2)]
            self.cls = [np.int32(0)]

        def __len__(self):
            return 1

    mouse, cat, objs = _parse_yolo_detection_results(
        [SimpleNamespace(boxes=_Boxes())],
        labels=["prey"],
        cat_names=[],
        min_threshold=50.0,
    )
    assert mouse in (19, 20)
    assert len(objs) == 1


# --- RemoteModelTrainer state / extract --------------------------------------


def test_download_state_read_write_and_stale_worker(monkeypatch, tmp_path):
    state_path = str(tmp_path / "dl_state.json")
    monkeypatch.setattr("src.model.remote_trainer._MODEL_DL_STATE_PATH", state_path)

    RemoteModelTrainer._write_model_download_state(
        {"status": "downloading", "pid": 999999, "result_id": "r1"}
    )
    monkeypatch.setattr("src.model.remote_trainer._pid_alive", lambda pid: False)
    state = RemoteModelTrainer.get_model_download_state()
    assert state["status"] == "error"
    assert state["error"] == "worker_died"


def test_extract_model_zip_success_and_missing(tmp_path, monkeypatch):
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setattr("src.model.remote_trainer.models_yolo_root", lambda: str(models))

    zpath = tmp_path / "m.zip"
    import zipfile

    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(
            "info.json",
            json.dumps(
                {
                    "MODEL_NAME": "ExtractMe",
                    "TIMESTAMP_UTC": "2024-05-01T10:00:00+00:00",
                }
            ),
        )
        zf.writestr("labels.txt", "prey\n")
        zf.writestr("model.pt", b"x")
        zf.writestr("best_ncnn_model/model.ncnn.bin", b"b")
        zf.writestr("best_ncnn_model/model.ncnn.param", b"p")

    ok, target, err = RemoteModelTrainer._extract_model_zip(str(zpath), model_name="ExtractMe")
    assert ok and not err
    assert Path(target).is_dir()
    assert (Path(target) / "model.pt").exists()

    incomplete = tmp_path / "bad.zip"
    with zipfile.ZipFile(incomplete, "w") as zf:
        zf.writestr("info.json", "{}")
        zf.writestr("labels.txt", "prey\n")
    ok, target, err = RemoteModelTrainer._extract_model_zip(str(incomplete), model_name="Bad")
    assert ok is False
    assert "missing_files" in err

    ok, _, err = RemoteModelTrainer._extract_model_zip(str(tmp_path / "nope.zip"))
    assert ok is False and err == "zip_missing"


def test_start_download_model_async_guards(monkeypatch, tmp_path):
    state_path = str(tmp_path / "dl_state.json")
    monkeypatch.setattr("src.model.remote_trainer._MODEL_DL_STATE_PATH", state_path)

    RemoteModelTrainer._write_model_download_state(
        {"status": "done", "result_id": "same", "pid": 0}
    )
    assert (
        RemoteModelTrainer.start_download_model_async("job", "same", model_name="m")
        is False
    )

    RemoteModelTrainer._write_model_download_state(
        {"status": "downloading", "result_id": "other", "pid": 1}
    )
    monkeypatch.setattr("src.model.remote_trainer._pid_alive", lambda pid: True)
    assert (
        RemoteModelTrainer.start_download_model_async("job", "new", model_name="m")
        is False
    )


def test_start_download_model_async_uses_repo_root_cwd(monkeypatch, tmp_path):
    """Worker must be started from the repo root so `-m src.model_download_worker` resolves."""
    import src.model.remote_trainer as rt
    from src.paths import kittyhack_root

    state_path = str(tmp_path / "dl_state.json")
    monkeypatch.setattr("src.model.remote_trainer._MODEL_DL_STATE_PATH", state_path)
    captured: dict[str, object] = {}

    class _Proc:
        pid = 12345

    def fake_popen(args, cwd=None, **kwargs):
        captured["args"] = args
        captured["cwd"] = cwd
        return _Proc()

    monkeypatch.setattr("src.model.remote_trainer.Versioning.get_git_version", lambda: "3.0.0-test")
    monkeypatch.setattr("src.model.remote_trainer.subprocess.Popen", fake_popen)

    assert RemoteModelTrainer.start_download_model_async("job", "rid", model_name="m") is True
    assert captured["cwd"] == kittyhack_root()
    assert Path(str(captured["cwd"]), "src", "model_download_worker.py").is_file()
    args = captured["args"]
    assert isinstance(args, list)
    assert args[1:3] == ["-m", "src.model_download_worker"]
    # The previous `join(__file__, "..")` cwd was `src/`, which cannot import package `src`.
    old_cwd = os.path.abspath(os.path.join(os.path.dirname(rt.__file__), ".."))
    assert captured["cwd"] != old_cwd


def test_check_model_training_retries_worker_died(monkeypatch, tmp_path):
    job_id = "f920f776-6726-44d2-a2d5-66dcac27d96d"
    state_path = str(tmp_path / "dl_state.json")
    monkeypatch.setattr("src.model.remote_trainer._MODEL_DL_STATE_PATH", state_path)
    monkeypatch.setitem(baseconfig.CONFIG, "MODEL_TRAINING", job_id)
    monkeypatch.setitem(baseconfig.CONFIG, "MODEL_TRAINING_JOB_TOKEN", "")

    RemoteModelTrainer._write_model_download_state(
        {
            "status": "error",
            "error": "worker_died",
            "training_job_id": job_id,
            "result_id": "rid-1",
            "bytes_downloaded": 0,
            "finalized": True,
            "retry_at": 0,
        }
    )
    started: list[tuple[str, str]] = []

    def fake_start(training_job_id, result_id, model_name="", token=None):
        started.append((training_job_id, result_id))
        return True

    monkeypatch.setattr(RemoteModelTrainer, "start_download_model_async", fake_start)
    status = RemoteModelTrainer.check_model_training_result(
        show_notification=False, return_pretty_status=False
    )
    assert started == [(job_id, "rid-1")]
    assert status == "downloading"


def test_yolo_worker_process_is_forking_picklable():
    """Python 3.14 forkserver pickles Process.target; a nested worker would crash camera start."""
    import io
    from multiprocessing.reduction import ForkingPickler

    assert "<locals>" not in yolo_model_worker_process.__qualname__
    buf = io.BytesIO()
    ForkingPickler(buf).dump(yolo_model_worker_process)
    assert buf.tell() > 0


def test_yolo_worker_module_has_no_heavy_toplevel_imports():
    """OpenMP/NCNN must not initialize before configure_worker_compute() runs."""
    import ast
    from pathlib import Path

    src = Path("src/model/yolo_inference_worker.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module.split(".")[0])
    heavy = {"cv2", "numpy", "torch", "ultralytics", "ncnn", "psutil"}
    assert not (set(imported) & heavy)


def test_configure_worker_compute_sets_thread_env(monkeypatch):
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.delenv("OMP_THREAD_LIMIT", raising=False)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3}, raising=False)
    monkeypatch.setattr(os, "sched_setaffinity", lambda _pid, _mask: None, raising=False)
    cores = configure_worker_compute(1)
    assert os.environ["OMP_NUM_THREADS"] == "1"
    assert os.environ["OMP_THREAD_LIMIT"] == "1"
    assert os.environ["OMP_WAIT_POLICY"] == "PASSIVE"
    assert cores == [0]
