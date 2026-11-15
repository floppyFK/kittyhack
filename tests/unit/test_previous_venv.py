"""`.venv.old` is removed after startup and credited only for a Python migration."""

import os
import time
from pathlib import Path

import pytest

from src.helper import (
    MIN_HEAVY_UPDATE_FREE_DISK_MB,
    _directory_size_mb,
    discard_previous_venv,
    free_disk_mb_for_heavy_update,
    previous_venv_discard_block_reason,
    previous_venv_size_mb,
)


def _write_required(root: Path, version: str = "3.14") -> None:
    setup = root / "setup"
    setup.mkdir(parents=True, exist_ok=True)
    (setup / "REQUIRED_PYTHON").write_text(version + "\n")


def _write_venv_python(root: Path, version: str, *, as_symlink: bool = False) -> None:
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    script = f"#!/bin/sh\necho {version}\n"
    if as_symlink:
        real = bin_dir / "python-real"
        real.write_text(script)
        real.chmod(0o755)
        (bin_dir / "python").symlink_to(real.name)
        return
    python = bin_dir / "python"
    python.write_text(script)
    python.chmod(0o755)


def _write_old(root: Path, payload: bytes = b"old-runtime") -> Path:
    old = root / ".venv.old"
    old.mkdir()
    (old / "marker").write_bytes(payload)
    return old


def _matching_install(root: Path, *, symlink_python: bool = False) -> Path:
    _write_required(root)
    _write_venv_python(root, "3.14", as_symlink=symlink_python)
    return _write_old(root)


def test_discard_is_noop_when_previous_venv_is_missing(tmp_path):
    assert previous_venv_discard_block_reason(str(tmp_path)) == "missing"
    assert discard_previous_venv(str(tmp_path), background=False) is False


def test_discard_keeps_symlink(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    (tmp_path / ".venv.old").symlink_to(target)

    assert discard_previous_venv(str(tmp_path), background=False) is False
    assert (tmp_path / ".venv.old").is_symlink()
    assert (target / "keep.txt").read_text() == "keep"


def test_discard_keeps_directory_while_new_venv_or_pending_marker_exists(tmp_path):
    old = _matching_install(tmp_path)
    (tmp_path / ".venv.new").mkdir()
    assert discard_previous_venv(str(tmp_path), background=False) is False
    assert old.is_dir()

    (tmp_path / ".venv.new").rmdir()
    (tmp_path / ".runtime-update-pending").write_text("")
    assert discard_previous_venv(str(tmp_path), background=False) is False
    assert old.is_dir()


def test_discard_keeps_directory_when_active_python_mismatches(tmp_path):
    _write_required(tmp_path, "3.14")
    _write_venv_python(tmp_path, "3.12")
    old = _write_old(tmp_path)

    assert "does not match" in (previous_venv_discard_block_reason(str(tmp_path)) or "")
    assert discard_previous_venv(str(tmp_path), background=False) is False
    assert old.is_dir()


def test_discard_keeps_directory_when_realpath_escapes(monkeypatch, tmp_path):
    old = _matching_install(tmp_path)
    real_realpath = os.path.realpath

    def escaped(path):
        if str(path).endswith(f"{os.sep}.venv.old"):
            return f"{os.sep}tmp{os.sep}escaped{os.sep}.venv.old"
        return real_realpath(path)

    monkeypatch.setattr(os.path, "realpath", escaped)
    assert previous_venv_discard_block_reason(str(tmp_path)) == "path escaped install root"
    assert discard_previous_venv(str(tmp_path), background=False) is False
    assert old.is_dir()


def test_discard_removes_matching_venv_including_symlinked_python(tmp_path):
    old = _matching_install(tmp_path, symlink_python=True)
    assert previous_venv_discard_block_reason(str(tmp_path)) is None
    assert discard_previous_venv(str(tmp_path), background=False) is True
    assert not old.exists()


def test_discard_background_removes_matching_venv(tmp_path):
    old = _matching_install(tmp_path)
    assert discard_previous_venv(str(tmp_path), background=True) is True
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and old.exists():
        time.sleep(0.01)
    assert not old.exists()


def test_previous_venv_size_is_cached_and_zero_once_removed(monkeypatch, tmp_path):
    old = _write_old(tmp_path, b"x" * 4096)
    calls = {"n": 0}
    real_size = _directory_size_mb

    def counting(path):
        calls["n"] += 1
        return real_size(path)

    monkeypatch.setattr("src.helper._directory_size_mb", counting)
    root = str(tmp_path)
    first = previous_venv_size_mb(root)
    second = previous_venv_size_mb(root)
    assert first == pytest.approx(4096 / (1024 * 1024))
    assert second == first
    assert calls["n"] == 1

    old.joinpath("marker").unlink()
    old.rmdir()
    assert previous_venv_size_mb(root) == 0.0


def test_free_disk_credits_previous_venv_only_when_python_changes(tmp_path):
    _write_old(tmp_path, b"x" * (1024 * 1024))
    root = str(tmp_path)
    size_mb = previous_venv_size_mb(root)
    assert size_mb == pytest.approx(1.0)

    free_mb = MIN_HEAVY_UPDATE_FREE_DISK_MB - 0.5
    python_change = {
        "setup/REQUIRED_PYTHON": True,
        "requirements.txt": False,
    }
    requirements_only = {
        "setup/REQUIRED_PYTHON": False,
        "requirements.txt": True,
    }
    credited = free_disk_mb_for_heavy_update(free_mb, python_change, root)
    assert credited == pytest.approx(free_mb + size_mb)
    assert credited >= MIN_HEAVY_UPDATE_FREE_DISK_MB
    assert free_disk_mb_for_heavy_update(free_mb, requirements_only, root) == free_mb
    assert free_disk_mb_for_heavy_update(free_mb, None, root) == free_mb

    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "big").write_bytes(b"x" * (1024 * 1024))
    (tmp_path / "linked").mkdir()
    link = tmp_path / "linked" / ".venv.old"
    link.symlink_to(target)
    assert previous_venv_size_mb(str(tmp_path / "linked")) == 0.0
    assert (
        free_disk_mb_for_heavy_update(free_mb, python_change, str(tmp_path / "linked"))
        == free_mb
    )
