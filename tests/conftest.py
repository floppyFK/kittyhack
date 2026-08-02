"""Shared fixtures for local unit tests (no real GPIO)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _force_simulate_env(request, monkeypatch):
    """Default unit tests to simulate mode; hardware tests keep real GPIO."""
    from src import runtime_flags

    if request.node.get_closest_marker("hardware"):
        monkeypatch.delenv("KITTYHACK_SIMULATE", raising=False)
        monkeypatch.setattr(runtime_flags, "_FORCE_SIMULATE", None)
        return

    monkeypatch.setenv("KITTYHACK_SIMULATE", "1")
    monkeypatch.setattr(runtime_flags, "_FORCE_SIMULATE", None)


@pytest.fixture
def tmp_config_ini(tmp_path, monkeypatch):
    """Point baseconfig CONFIGFILE at a fresh temp ini and reload defaults."""
    import src.baseconfig as baseconfig

    cfg = tmp_path / "config.ini"
    monkeypatch.setattr(baseconfig, "CONFIGFILE", str(cfg))
    monkeypatch.chdir(tmp_path)
    baseconfig.create_default_config()
    baseconfig.load_config()
    return cfg


@pytest.fixture
def fake_hardware():
    """Construct FakePir / FakeMagnets / FakeRfid via the factory."""
    from src.hardware_sim import create_hardware

    pir, magnets, rfid = create_hardware(simulate=True)
    pir.init()
    magnets.init()
    return pir, magnets, rfid


@pytest.fixture
def tmp_kittyhack_db(tmp_path):
    """Empty kittyhack SQLite DB with cats + events tables."""
    from src.database import DbMigrations

    db = str(tmp_path / "kittyhack_test.db")
    DbMigrations.create_kittyhack_cats_table(db)
    DbMigrations.create_kittyhack_events_table(db)
    DbMigrations.create_motion_timeline_table(db)
    return db
