"""Fake hardware behavior and factory."""

from src.hardware_sim import FakeMagnets, FakePir, FakeRfid, create_hardware
from src.magnets_rfid import RfidRunState
from src.runtime_flags import is_simulate_mode, set_simulate_mode


def test_is_simulate_mode_from_env(monkeypatch):
    monkeypatch.setattr("src.runtime_flags._FORCE_SIMULATE", None)
    monkeypatch.setenv("KITTYHACK_SIMULATE", "1")
    assert is_simulate_mode() is True
    monkeypatch.setenv("KITTYHACK_SIMULATE", "0")
    assert is_simulate_mode() is False


def test_set_simulate_mode_overrides_env(monkeypatch):
    monkeypatch.setenv("KITTYHACK_SIMULATE", "0")
    set_simulate_mode(True)
    assert is_simulate_mode() is True
    set_simulate_mode(False)
    assert is_simulate_mode() is False


def test_fake_pir_triggers(fake_hardware):
    pir, magnets, rfid = fake_hardware
    pir.trigger_outside(True)
    pir.trigger_inside(False)
    outside, inside, outside_raw, inside_raw = pir.get_states()
    assert outside == 1
    assert inside == 0
    assert outside_raw == 1
    assert inside_raw == 0


def test_fake_magnets_queue(fake_hardware):
    _, magnets, _ = fake_hardware
    assert magnets.get_inside_state() is False
    magnets.queue_command("unlock_inside")
    assert magnets.get_inside_state() is True
    magnets.queue_command("lock_inside")
    assert magnets.get_inside_state() is False
    magnets.queue_command("unlock_outside")
    assert magnets.get_outside_state() is True
    magnets.empty_queue()
    assert magnets.get_inside_state() is False
    assert magnets.get_outside_state() is False


def test_fake_rfid_inject_tag(fake_hardware):
    _, _, rfid = fake_hardware
    rfid.inject_tag("AABBCC", timestamp=123.0)
    tag, ts = rfid.get_tag()
    assert tag == "AABBCC"
    assert ts == 123.0
    rfid.set_field(True)
    assert rfid.get_field() is True


def test_create_hardware_simulate_returns_fakes(monkeypatch):
    monkeypatch.setattr("src.mode.is_remote_mode", lambda: False)
    pir, magnets, rfid = create_hardware(simulate=True)
    assert isinstance(pir, FakePir)
    assert isinstance(magnets, FakeMagnets)
    assert isinstance(rfid, FakeRfid)


def test_fake_rfid_run_state_compatible():
    rfid = FakeRfid()
    assert rfid.get_run_state() == RfidRunState.stopped
    rfid.set_run_state(RfidRunState.running)
    assert rfid.get_run_state() == RfidRunState.running
