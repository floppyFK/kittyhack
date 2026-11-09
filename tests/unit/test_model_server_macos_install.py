"""macOS install script for the model server and its command in the UI."""

import re
import subprocess
from pathlib import Path

import pytest

from src.baseconfig import CONFIG

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "setup" / "install_model_server_macos.sh"


def test_script_is_valid_bash_and_executable():
    assert SCRIPT.stat().st_mode & 0o111
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_script_copies_only_files_that_exist():
    text = SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"for name in \\\n(.*?)\n    do", text, re.S).group(1)
    names = block.replace("\\", " ").split()
    assert "requirements-torch-macos.txt" in names
    for name in names:
        assert (REPO_ROOT / "model-server" / name).is_file(), name


def test_macos_torch_requirements_use_pypi_wheels():
    lines = [
        line.strip()
        for line in (REPO_ROOT / "model-server" / "requirements-torch-macos.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines == ["torch==2.9.1", "torchvision==0.24.1"]


@pytest.mark.parametrize("lang", ["de", "en"])
def test_ui_macos_command_points_at_the_script(monkeypatch, lang):
    from src.server_ui import ai_training

    monkeypatch.setitem(CONFIG, "LANGUAGE", lang)
    command = ai_training._model_server_macos_command()

    assert "setup/install_model_server_macos.sh" in command
    assert f"install --lang {lang}" in command
    assert "sudo" not in command
