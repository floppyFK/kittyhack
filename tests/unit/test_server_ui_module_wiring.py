"""Regression checks for Shiny module UI wiring (refactor footguns).

Full tab render needs a live Shiny session; these catch the mechanical mistakes
that broke Info / WLAN / AI Training after the class→module split.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER_UI = ROOT / "src" / "server_ui"


def test_module_ui_buttons_accept_id_argument():
    """@module.ui wrappers must accept a module id; bare defs take 0 args."""
    from src.server_ui.wlan_modules import btn_wlan_connect, btn_wlan_modify
    from src.server_ui.yolo_modules import btn_yolo_activate, btn_yolo_modify

    for fn in (
        btn_wlan_modify,
        btn_wlan_connect,
        btn_yolo_modify,
        btn_yolo_activate,
    ):
        try:
            fn("test_module_id")
        except TypeError as e:
            if "takes 0 positional arguments but 1 was given" in str(e):
                pytest.fail(f"{fn.__module__}.{fn.__name__} is missing @module.ui")
        except Exception:
            # Missing Shiny session / reactive context is fine here.
            pass


def test_shiny_input_ids_have_no_dots():
    """Shiny ids may only contain letters, numbers, underscore — no Class.method."""
    pattern = re.compile(
        r"""(?:input_(?:task|action)_button|input_\w+)\(\s*["']([^"']+)["']"""
    )
    bad = []
    for path in SERVER_UI.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            shiny_id = match.group(1)
            if "." in shiny_id or not re.fullmatch(r"[A-Za-z0-9_]+", shiny_id):
                bad.append(f"{path.name}: {shiny_id!r}")
    assert not bad, "Invalid Shiny input id(s):\n" + "\n".join(bad)


def test_update_kittyhack_button_id_is_valid_shiny_id():
    """Info tab update button must use id 'update_kittyhack' (matches reactive.event)."""
    info = (SERVER_UI / "info.py").read_text(encoding="utf-8")
    button_ids = re.findall(r'input_task_button\(\s*"([^"]+)"', info)
    assert "update_kittyhack" in button_ids
    assert all("." not in button_id for button_id in button_ids)
