"""Switching the inference device installs the matching runtime."""

from __future__ import annotations

from pathlib import Path

from src.system import DependencyInstaller


def _patch_quiet(monkeypatch):
    monkeypatch.setattr(DependencyInstaller, "_uninstall_torch_stack", staticmethod(lambda: None))
    monkeypatch.setattr(DependencyInstaller, "_ensure_headless_opencv", staticmethod(lambda: True))
    monkeypatch.setattr(DependencyInstaller, "ensure_openvino_installed", staticmethod(lambda: True))


def test_intel_switch_exports_when_ir_is_missing(tmp_path, monkeypatch):
    _patch_quiet(monkeypatch)
    (tmp_path / "model.pt").write_bytes(b"pt")
    pip_args: list[list[str]] = []

    def _pip(args, timeout):
        pip_args.append(list(args))
        return True

    def _export(pt_path, export_format, imgsz):
        assert export_format == "openvino"
        assert imgsz == 480
        ir = Path(pt_path).parent / "model_openvino_model"
        ir.mkdir()
        (ir / "model.xml").write_text("<net/>", encoding="utf-8")
        return True

    monkeypatch.setattr(DependencyInstaller, "_pip_install", staticmethod(_pip))
    monkeypatch.setattr(DependencyInstaller, "_export_yolo", staticmethod(_export))
    monkeypatch.setattr(DependencyInstaller, "_package_installed", staticmethod(lambda name: False))

    ok, code = DependencyInstaller.prepare_inference_device("intel:gpu", str(tmp_path), 480)
    assert ok is True
    assert code == ""
    assert any(DependencyInstaller.CPU_TORCH_INDEX in args for args in pip_args)
    assert (tmp_path / "model_openvino_model" / "model.xml").is_file()


def test_intel_switch_skips_export_when_ir_exists(tmp_path, monkeypatch):
    _patch_quiet(monkeypatch)
    ir = tmp_path / "model_openvino_model"
    ir.mkdir()
    (ir / "model.xml").write_text("<net/>", encoding="utf-8")
    monkeypatch.setattr(
        DependencyInstaller,
        "_pip_install",
        staticmethod(lambda args, timeout: (_ for _ in ()).throw(AssertionError(args))),
    )
    monkeypatch.setattr(
        DependencyInstaller,
        "_export_yolo",
        staticmethod(lambda *args: (_ for _ in ()).throw(AssertionError("export"))),
    )

    ok, code = DependencyInstaller.prepare_inference_device("gpu", str(tmp_path), 320)
    assert ok is True
    assert code == ""


def test_cuda_switch_installs_cuda_requirements_before_download(tmp_path, monkeypatch):
    pip_args: list[list[str]] = []
    uninstalled: list[list[str]] = []
    monkeypatch.setattr(
        DependencyInstaller,
        "_pip_install",
        staticmethod(lambda args, timeout: pip_args.append(list(args)) or True),
    )
    monkeypatch.setattr(
        DependencyInstaller,
        "_pip_uninstall",
        staticmethod(lambda names: uninstalled.append(list(names))),
    )
    monkeypatch.setattr(
        DependencyInstaller,
        "_uninstall_torch_stack",
        staticmethod(lambda: (_ for _ in ()).throw(AssertionError("uninstall torch"))),
    )

    ok, code = DependencyInstaller.prepare_inference_device("cuda:0", str(tmp_path), 320)
    assert ok is False
    assert code == "model_pt_missing"
    assert pip_args == []

    (tmp_path / "model.pt").write_bytes(b"pt")
    ok, code = DependencyInstaller.prepare_inference_device("cuda:0", str(tmp_path), 320)
    assert ok is True
    assert code == ""
    assert uninstalled == [["opencv-python-headless"]]
    assert any("requirements_remote_cuda.txt" in arg for arg in pip_args[0])


def test_cpu_switch_exports_ncnn_and_drops_torch(tmp_path, monkeypatch):
    _patch_quiet(monkeypatch)
    (tmp_path / "model.pt").write_bytes(b"pt")
    dropped = {"n": 0}

    def _export(pt_path, export_format, imgsz):
        assert export_format == "ncnn"
        produced = Path(pt_path).parent / "model_ncnn_model"
        produced.mkdir()
        (produced / "model.ncnn.param").write_text("param", encoding="utf-8")
        return True

    monkeypatch.setattr(DependencyInstaller, "_package_installed", staticmethod(lambda name: False))
    monkeypatch.setattr(DependencyInstaller, "_pip_install", staticmethod(lambda args, timeout: True))
    monkeypatch.setattr(DependencyInstaller, "_export_yolo", staticmethod(_export))
    monkeypatch.setattr(
        DependencyInstaller,
        "_uninstall_torch_stack",
        staticmethod(lambda: dropped.__setitem__("n", dropped["n"] + 1)),
    )

    ok, code = DependencyInstaller.prepare_inference_device("cpu", str(tmp_path), 320)
    assert ok is True
    assert code == ""
    assert dropped["n"] == 1
    assert (tmp_path / "best_ncnn_model" / "model.ncnn.param").is_file()
