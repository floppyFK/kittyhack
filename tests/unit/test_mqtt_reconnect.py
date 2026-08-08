"""MQTT reconnect / availability recovery (no real broker)."""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mqtt_client(tmp_config_ini, monkeypatch):
    import src.baseconfig as baseconfig
    from src import mqtt as mqtt_mod

    baseconfig.CONFIG["MQTT_DEVICE_ID"] = "kittyhack_test"
    monkeypatch.setattr(mqtt_mod.MQTTConfig, "device_id", "kittyhack_test")

    fake_paho = MagicMock()
    monkeypatch.setattr(mqtt_mod.mqtt, "Client", MagicMock(return_value=fake_paho))

    client = mqtt_mod.MQTTClient("127.0.0.1", 1883, client_name="kittyhack_test")
    return client, fake_paho


def test_on_connect_publishes_online_and_resubscribes(mqtt_client):
    client, fake_paho = mqtt_client
    client.topic_callbacks["kittyhack/kittyhack_test/manual/override"] = lambda _: None

    client._on_connect(fake_paho, None, {"session_present": False}, 0)

    assert client.connected is True
    assert client._connect_count == 1
    fake_paho.publish.assert_any_call("kittyhack/kittyhack_test/status", "online", retain=True)
    fake_paho.subscribe.assert_called_with("kittyhack/kittyhack_test/manual/override")


def test_on_connect_failure_leaves_disconnected(mqtt_client):
    client, fake_paho = mqtt_client
    client._on_connect(fake_paho, None, {}, 5)
    assert client.connected is False
    fake_paho.publish.assert_not_called()


def test_reconnect_invokes_callback_and_republishes_online(mqtt_client):
    client, fake_paho = mqtt_client
    refreshed = []

    client.register_reconnect_callback(lambda: refreshed.append(True))
    client._on_connect(fake_paho, None, {}, 0)  # first connect
    assert refreshed == []

    client._on_disconnect(fake_paho, None, 1)
    assert client.connected is False

    client._on_connect(fake_paho, None, {}, 0)  # reconnect
    assert client.connected is True
    assert client._connect_count == 2
    assert refreshed == [True]
    # online published on both connects
    online_calls = [
        c for c in fake_paho.publish.call_args_list
        if c.args[:2] == ("kittyhack/kittyhack_test/status", "online")
    ]
    assert len(online_calls) == 2


def test_subscribe_defers_until_connected(mqtt_client):
    client, fake_paho = mqtt_client
    cb = MagicMock()
    client.subscribe("topic/a", cb)
    assert client.topic_callbacks["topic/a"] is cb
    fake_paho.subscribe.assert_not_called()

    client._on_connect(fake_paho, None, {}, 0)
    fake_paho.subscribe.assert_called_with("topic/a")


def test_disconnect_publishes_offline(mqtt_client):
    client, fake_paho = mqtt_client
    client.connected = True
    client.disconnect()
    fake_paho.publish.assert_any_call("kittyhack/kittyhack_test/status", "offline", retain=True)
    fake_paho.loop_stop.assert_called_once()
    fake_paho.disconnect.assert_called_once()
    assert client.connected is False
    assert client._intentional_disconnect is True


def test_connect_timeout_stops_loop(mqtt_client, monkeypatch):
    client, fake_paho = mqtt_client
    # Never gets CONNACK; force an immediate timeout.
    monkeypatch.setattr("src.mqtt.tm.monotonic", MagicMock(side_effect=[0.0, 100.0]))
    monkeypatch.setattr("src.mqtt.tm.sleep", lambda *_: None)
    assert client.connect(timeout=1.0) is False
    fake_paho.loop_start.assert_called_once()
    fake_paho.loop_stop.assert_called_once()
    fake_paho.disconnect.assert_called_once()
    assert client.connected is False


def test_state_publisher_refresh_after_reconnect(tmp_config_ini, monkeypatch):
    import src.baseconfig as baseconfig
    from src.mqtt import StatePublisher

    baseconfig.CONFIG["MQTT_DEVICE_ID"] = "kittyhack_test"
    baseconfig.CONFIG["LANGUAGE"] = "en"

    mqtt_client = MagicMock()
    mqtt_client.connected = True
    mqtt_client.client = MagicMock()
    mqtt_client.topic_callbacks = {}

    # Avoid sleeping / heavy discovery side effects where possible
    monkeypatch.setattr("src.mqtt.tm.sleep", lambda *_: None)
    monkeypatch.setattr(
        StatePublisher,
        "publish_discovery_topics",
        MagicMock(),
    )
    monkeypatch.setattr(StatePublisher, "register_config_handlers", MagicMock())

    pub = StatePublisher(
        mqtt_client,
        inside_lock_state=True,
        outside_lock_state=False,
        motion_inside_state=False,
        motion_outside_state=True,
        prey_detected_state=False,
    )
    mqtt_client.register_reconnect_callback.assert_called_once()

    pub.publish_discovery_topics.reset_mock()
    pub.refresh_after_reconnect()
    pub.publish_discovery_topics.assert_called_once()
    # last known lock/motion states republished
    assert mqtt_client.client.publish.call_count >= 4
