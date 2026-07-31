"""MQTT bridge manual override with fake magnets (no broker)."""

import src.backend.mqtt_bridge as mqtt_bridge
from src.hardware_sim import FakeMagnets


def _reset_override():
    mqtt_bridge.manual_door_override.update(
        {
            "unlock_inside": False,
            "unlock_outside": False,
            "lock_inside": False,
            "lock_outside": False,
        }
    )


def test_toggle_inside_unlocks_when_locked(monkeypatch):
    _reset_override()
    magnets = FakeMagnets()
    magnets.init()
    assert magnets.get_inside_state() is False

    from src.magnets_rfid import Magnets

    monkeypatch.setattr(Magnets, "instance", magnets)
    mqtt_bridge.handle_manual_override("toggle_inside")
    assert mqtt_bridge.manual_door_override["unlock_inside"] is True
    assert mqtt_bridge.manual_door_override["lock_inside"] is False


def test_toggle_inside_locks_when_unlocked(monkeypatch):
    _reset_override()
    magnets = FakeMagnets()
    magnets.init()
    magnets.queue_command("unlock_inside")

    from src.magnets_rfid import Magnets

    monkeypatch.setattr(Magnets, "instance", magnets)
    mqtt_bridge.handle_manual_override({"command": "toggle_inside"})
    assert mqtt_bridge.manual_door_override["lock_inside"] is True
    assert mqtt_bridge.manual_door_override["unlock_inside"] is False


def test_toggle_inside_noop_without_magnets(monkeypatch):
    _reset_override()
    from src.magnets_rfid import Magnets

    monkeypatch.setattr(Magnets, "instance", None)
    mqtt_bridge.handle_manual_override("toggle_inside")
    assert mqtt_bridge.manual_door_override["unlock_inside"] is False
    assert mqtt_bridge.manual_door_override["lock_inside"] is False


def test_update_mqtt_config_skips_when_disabled(tmp_config_ini, monkeypatch):
    import src.baseconfig as baseconfig

    baseconfig.CONFIG["MQTT_ENABLED"] = False
    mqtt_bridge.mqtt_publisher = object()  # would fail if called
    mqtt_bridge.update_mqtt_config()  # no exception
    mqtt_bridge.update_mqtt_language()
