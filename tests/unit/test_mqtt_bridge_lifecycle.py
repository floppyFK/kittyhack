"""Extra mqtt_bridge coverage without a real broker."""

from unittest.mock import MagicMock

import src.backend.mqtt_bridge as mqtt_bridge


def test_cleanup_mqtt_stops_publisher_and_client():
    pub = MagicMock()
    client = MagicMock()
    mqtt_bridge.mqtt_publisher = pub
    mqtt_bridge.mqtt_client = client
    mqtt_bridge.cleanup_mqtt()
    pub.stop_periodic_image_publishing.assert_called_once()
    client.disconnect.assert_called_once()
    assert mqtt_bridge.mqtt_publisher is None
    assert mqtt_bridge.mqtt_client is None


def test_cleanup_mqtt_handles_disconnect_error():
    client = MagicMock()
    client.disconnect.side_effect = RuntimeError("bye")
    mqtt_bridge.mqtt_publisher = None
    mqtt_bridge.mqtt_client = client
    mqtt_bridge.cleanup_mqtt()
    assert mqtt_bridge.mqtt_client is None


def test_init_mqtt_disabled(tmp_config_ini):
    import src.baseconfig as baseconfig

    baseconfig.CONFIG["MQTT_ENABLED"] = False
    assert mqtt_bridge.init_mqtt_client() is True
    assert mqtt_bridge.mqtt_client is None


def test_init_mqtt_missing_broker(tmp_config_ini):
    import src.baseconfig as baseconfig

    baseconfig.CONFIG["MQTT_ENABLED"] = True
    baseconfig.CONFIG["MQTT_BROKER_ADDRESS"] = ""
    baseconfig.CONFIG["MQTT_BROKER_PORT"] = 1883
    assert mqtt_bridge.init_mqtt_client() is False


def test_init_mqtt_connect_failure(tmp_config_ini, monkeypatch):
    import src.baseconfig as baseconfig

    baseconfig.CONFIG["MQTT_ENABLED"] = True
    baseconfig.CONFIG["MQTT_BROKER_ADDRESS"] = "127.0.0.1"
    baseconfig.CONFIG["MQTT_BROKER_PORT"] = 1883

    fake_client = MagicMock()
    fake_client.connect.return_value = False
    monkeypatch.setattr(mqtt_bridge, "MQTTClient", MagicMock(return_value=fake_client))
    assert mqtt_bridge.init_mqtt_client() is False


def test_init_mqtt_success_with_magnets(tmp_config_ini, monkeypatch, fake_hardware):
    import src.baseconfig as baseconfig

    pir, magnets, _ = fake_hardware
    baseconfig.CONFIG["MQTT_ENABLED"] = True
    baseconfig.CONFIG["MQTT_BROKER_ADDRESS"] = "127.0.0.1"
    baseconfig.CONFIG["MQTT_BROKER_PORT"] = 1883
    baseconfig.CONFIG["LOCK_DURATION_AFTER_PREY_DETECTION"] = 300

    fake_client = MagicMock()
    fake_client.connect.return_value = True
    fake_pub = MagicMock()
    monkeypatch.setattr(mqtt_bridge, "MQTTClient", MagicMock(return_value=fake_client))
    monkeypatch.setattr(mqtt_bridge, "StatePublisher", MagicMock(return_value=fake_pub))
    # Avoid importing heavy loop for prey_mono
    monkeypatch.setattr(
        "src.backend.loop.backend_main",
        type("BM", (), {"prey_detection_mono": 0.0})(),
        raising=False,
    )

    assert mqtt_bridge.init_mqtt_client(magnets_instance=magnets, motion_outside=1, motion_inside=0) is True
    fake_pub.register_manual_override_handler.assert_called()
    fake_pub.start_periodic_image_publishing.assert_called()
    mqtt_bridge.cleanup_mqtt()


def test_update_mqtt_config_publishes(tmp_config_ini, monkeypatch):
    import src.baseconfig as baseconfig

    baseconfig.CONFIG["MQTT_ENABLED"] = True
    pub = MagicMock()
    mqtt_bridge.mqtt_publisher = pub
    mqtt_bridge.update_mqtt_config("ALLOWED_TO_ENTER")
    pub.publish_allowed_to_enter.assert_called_once()
    pub.publish_allowed_to_exit.assert_not_called()

    mqtt_bridge.update_mqtt_config()
    assert pub.publish_allowed_to_exit.called
    mqtt_bridge.update_mqtt_language()
    pub.update_language_dependent_topics.assert_called()
    mqtt_bridge.mqtt_publisher = None


def test_restart_mqtt_uses_instances(monkeypatch, fake_hardware):
    pir, magnets, _ = fake_hardware
    from src.magnets_rfid import Magnets
    from src.pir import Pir

    monkeypatch.setattr(Magnets, "instance", magnets)
    monkeypatch.setattr(Pir, "instance", pir)
    called = {}

    def fake_init(**kwargs):
        called.update(kwargs)
        return True

    monkeypatch.setattr(mqtt_bridge, "init_mqtt_client", fake_init)
    assert mqtt_bridge.restart_mqtt() is True
    assert called.get("magnets_instance") is magnets
