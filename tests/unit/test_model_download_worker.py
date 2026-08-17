"""Unit tests for ``src.model_download_worker`` (no real network)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.model_download_worker as worker


def _make_model_zip(path: Path, *, with_required: bool = True, model_name: str = "TestModel") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = {
            "MODEL_NAME": model_name,
            "TIMESTAMP_UTC": "2024-06-15T12:00:00+00:00",
            "JOB_ID": "job-1",
        }
        zf.writestr("info.json", json.dumps(info))
        zf.writestr("labels.txt", "prey\nmia\n")
        if with_required:
            zf.writestr("model.pt", b"pt-bytes")
            zf.writestr("best_ncnn_model/model.ncnn.bin", b"bin")
            zf.writestr("best_ncnn_model/model.ncnn.param", b"param")
    data = buf.getvalue()
    path.write_bytes(data)
    return data


def _patch_tmp_and_models(monkeypatch, tmp_path: Path):
    """Redirect temp zip path and models_yolo_root into tmp_path.

    Patch tempfile.gettempdir() rather than os.path.join("/tmp", ...): on
    Python 3.14 pathlib rebuilds absolute paths via os.path.join starting
    at "/tmp", so a global join rewrite doubles pytest tmp_path.
    """
    tmp_dir = tmp_path / "tmp"
    models_dir = tmp_path / "models"
    tmp_dir.mkdir()
    models_dir.mkdir()

    monkeypatch.setattr(worker.tempfile, "gettempdir", lambda: str(tmp_dir))
    monkeypatch.setattr(worker, "models_yolo_root", lambda: str(models_dir))
    return tmp_dir, models_dir


class _FakeResp:
    def __init__(self, content: bytes, *, status=200, headers=None):
        self.status_code = status
        self._content = content
        self.headers = headers or {}
        self.text = "ok"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]


# --- Pure helpers ------------------------------------------------------------


def test_atomic_and_merge_json(tmp_path):
    path = str(tmp_path / "state.json")
    worker._atomic_write_json(path, {"a": 1})
    assert worker._read_json(path) == {"a": 1}

    merged = worker._merge_write_json(path, {"b": 2, "status": "downloading"})
    assert merged["a"] == 1 and merged["b"] == 2
    assert worker._read_json(path)["status"] == "downloading"

    assert worker._read_json(str(tmp_path / "missing.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    assert worker._read_json(str(bad)) == {}


def test_sanitize_directory_name():
    assert worker._sanitize_directory_name("My Model!!") == "my_model_"
    assert worker._sanitize_directory_name("hello__world") == "hello_world"
    assert worker._sanitize_directory_name("...")  # fallback timestamp prefix
    name = worker._sanitize_directory_name("")
    assert name.startswith("model_")


def test_determine_model_name_from_info(tmp_path):
    zpath = tmp_path / "m.zip"
    _make_model_zip(zpath, model_name="FromInfo")
    name, creation = worker._determine_model_name_from_info(str(zpath))
    assert name == "FromInfo"
    assert "2024" in creation

    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("other.txt", "x")
    name, _ = worker._determine_model_name_from_info(str(empty))
    assert name is None

    name, _ = worker._determine_model_name_from_info(str(tmp_path / "no.zip"))
    assert name is None


def test_sha256_file(tmp_path):
    p = tmp_path / "f.bin"
    data = b"abc123"
    p.write_bytes(data)
    assert worker._sha256_file(str(p)) == hashlib.sha256(data).hexdigest()


def test_acknowledge_download_ok_and_errors(monkeypatch):
    calls = {}

    def fake_post(url, json=None, verify=True, timeout=None, **kwargs):
        calls["url"] = url
        calls["json"] = json
        calls["headers"] = kwargs.get("headers")
        return SimpleNamespace(status_code=200, text="acked")

    monkeypatch.setattr(worker.requests, "post", fake_post)
    worker._acknowledge_download(
        base_url="https://example.test",
        result_id="rid",
        delete_token="tok",
        sha256="abc",
        size_bytes=10,
    )
    assert calls["url"].endswith("/download/rid/ack")
    assert calls["json"]["delete_token"] == "tok"

    monkeypatch.setattr(
        worker.requests,
        "post",
        lambda *a, **k: SimpleNamespace(status_code=500, text="nope"),
    )
    worker._acknowledge_download(
        base_url="https://example.test",
        result_id="rid",
        delete_token="tok",
        sha256="abc",
        size_bytes=10,
    )

    monkeypatch.setattr(
        worker.requests, "post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net"))
    )
    worker._acknowledge_download(
        base_url="https://example.test",
        result_id="rid",
        delete_token="tok",
        sha256="abc",
        size_bytes=10,
    )


# --- download_and_extract ----------------------------------------------------


def test_download_and_extract_success(monkeypatch, tmp_path):
    _tmp_dir, models_dir = _patch_tmp_and_models(monkeypatch, tmp_path)
    zip_bytes = _make_model_zip(tmp_path / "src.zip")
    sha = hashlib.sha256(zip_bytes).hexdigest()
    state_path = str(tmp_path / "state.json")

    def fake_get(url, headers=None, stream=True, verify=True, timeout=None):
        return _FakeResp(
            zip_bytes,
            headers={
                "content-length": str(len(zip_bytes)),
                "X-Model-SHA256": sha,
                "X-Model-Size": str(len(zip_bytes)),
                "X-Delete-Token": "del-tok",
            },
        )

    ack = {}

    def fake_ack(**kwargs):
        ack.update(kwargs)

    monkeypatch.setattr(worker.requests, "get", fake_get)
    monkeypatch.setattr(worker, "_acknowledge_download", fake_ack)

    worker.download_and_extract(
        base_url="https://example.test",
        result_id="result1",
        model_name="MyModel",
        token="secret",
        state_path=state_path,
    )

    state = worker._read_json(state_path)
    assert state["status"] == "done"
    assert state["target_dir"]
    assert Path(state["target_dir"]).is_dir()
    assert (Path(state["target_dir"]) / "model.pt").exists()
    assert ack.get("delete_token") == "del-tok"
    # Zip cleaned up from redirected /tmp
    assert not list(_tmp_dir.glob("*.zip"))


def test_download_and_extract_404(monkeypatch, tmp_path):
    _patch_tmp_and_models(monkeypatch, tmp_path)
    state_path = str(tmp_path / "state.json")

    def fake_get(*a, **k):
        return _FakeResp(b"", status=404)

    monkeypatch.setattr(worker.requests, "get", fake_get)

    with pytest.raises(FileNotFoundError):
        worker.download_and_extract(
            base_url="https://example.test",
            result_id="missing",
            model_name="",
            token=None,
            state_path=state_path,
        )
    assert worker._read_json(state_path)["status"] == "error"


def test_download_and_extract_sha_mismatch(monkeypatch, tmp_path):
    _patch_tmp_and_models(monkeypatch, tmp_path)
    zip_bytes = _make_model_zip(tmp_path / "src.zip")
    state_path = str(tmp_path / "state.json")

    monkeypatch.setattr(
        worker.requests,
        "get",
        lambda *a, **k: _FakeResp(
            zip_bytes,
            headers={
                "X-Model-SHA256": "deadbeef",
                "X-Model-Size": str(len(zip_bytes)),
            },
        ),
    )

    with pytest.raises(RuntimeError, match="sha256"):
        worker.download_and_extract(
            base_url="https://example.test",
            result_id="badsha",
            model_name="x",
            token=None,
            state_path=state_path,
        )
    assert worker._read_json(state_path)["status"] == "error"


def test_download_and_extract_missing_required_files(monkeypatch, tmp_path):
    _patch_tmp_and_models(monkeypatch, tmp_path)
    zip_bytes = _make_model_zip(tmp_path / "src.zip", with_required=False)
    sha = hashlib.sha256(zip_bytes).hexdigest()
    state_path = str(tmp_path / "state.json")

    monkeypatch.setattr(
        worker.requests,
        "get",
        lambda *a, **k: _FakeResp(
            zip_bytes,
            headers={"X-Model-SHA256": sha, "X-Model-Size": str(len(zip_bytes))},
        ),
    )

    with pytest.raises(RuntimeError, match="missing_files"):
        worker.download_and_extract(
            base_url="https://example.test",
            result_id="incomplete",
            model_name="Incomplete",
            token=None,
            state_path=state_path,
        )
    assert worker._read_json(state_path)["status"] == "error"


def test_download_and_extract_unique_dir_on_collision(monkeypatch, tmp_path):
    _tmp_dir, models_dir = _patch_tmp_and_models(monkeypatch, tmp_path)
    # Pre-create sanitized dir so worker must pick _1
    (models_dir / "mymodel").mkdir()
    zip_bytes = _make_model_zip(tmp_path / "src.zip")
    sha = hashlib.sha256(zip_bytes).hexdigest()
    state_path = str(tmp_path / "state.json")

    monkeypatch.setattr(
        worker.requests,
        "get",
        lambda *a, **k: _FakeResp(
            zip_bytes,
            headers={"X-Model-SHA256": sha, "X-Model-Size": str(len(zip_bytes))},
        ),
    )
    monkeypatch.setattr(worker, "_acknowledge_download", lambda **k: None)

    worker.download_and_extract(
        base_url="https://example.test",
        result_id="r2",
        model_name="MyModel",
        token=None,
        state_path=state_path,
    )
    target = worker._read_json(state_path)["target_dir"]
    assert target.endswith("mymodel_1") or "mymodel_1" in target


def test_main_success_and_failure(monkeypatch, tmp_path):
    calls = {}

    def ok(**kwargs):
        calls["ok"] = True

    monkeypatch.setattr(worker, "download_and_extract", ok)
    rc = worker.main(
        [
            "--result-id",
            "r",
            "--state-path",
            str(tmp_path / "s.json"),
            "--base-url",
            "https://example.test",
            "--model-name",
            "n",
            "--token",
            "t",
        ]
    )
    assert rc == 0 and calls.get("ok")

    def boom(**kwargs):
        raise RuntimeError("fail")

    monkeypatch.setattr(worker, "download_and_extract", boom)
    rc = worker.main(
        [
            "--result-id",
            "r",
            "--state-path",
            str(tmp_path / "s.json"),
            "--base-url",
            "https://example.test",
        ]
    )
    assert rc == 1
