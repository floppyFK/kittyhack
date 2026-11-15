"""The reboot dialog must not come back after the device has restarted."""

from __future__ import annotations

import os
import time

from src.server_ui import system_tab


def test_power_action_marker_is_consumed_once(tmp_path, monkeypatch):
    marker = tmp_path / ".power-action"
    monkeypatch.setattr(system_tab, "_power_action_marker_path", lambda: str(marker))

    assert system_tab.consume_local_power_action() is False
    system_tab.arm_local_power_action()
    assert marker.is_file()
    assert system_tab.consume_local_power_action() is True
    assert not marker.exists()
    assert system_tab.consume_local_power_action() is False


def test_stale_power_action_marker_does_not_dismiss(tmp_path, monkeypatch):
    marker = tmp_path / ".power-action"
    marker.write_text("old", encoding="utf-8")
    stale = time.time() - system_tab._POWER_ACTION_MAX_AGE_S - 5
    os.utime(marker, (stale, stale))
    monkeypatch.setattr(system_tab, "_power_action_marker_path", lambda: str(marker))

    assert system_tab.consume_local_power_action() is False
    assert not marker.exists()
