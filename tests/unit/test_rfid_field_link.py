"""RFID field requests must not flood a stuck remote-control socket."""

from __future__ import annotations

import asyncio
import json
import threading

from src.clock import monotonic_time
from src.magnets_rfid import Rfid
from src.remote.control_client import RemoteControlClient, _RFID_FIELD_RESEND_S


def test_set_field_queues_each_gpio_state_once():
    rfid = object.__new__(Rfid)
    rfid.simulate_kittyflap = False
    rfid.field_state = False
    rfid.thread_lock = threading.Lock()
    queued: list = []

    class Queue:
        def queue_command(self, *args):
            queued.append(args)

    rfid.command_queue = Queue()
    rfid.set_field(True)
    rfid.set_field(True)
    assert len(queued) == 1
    assert rfid.get_field() is True
    rfid.set_field(False)
    rfid.set_field(False)
    assert len(queued) == 2
    assert rfid.get_field() is False


def test_remote_field_request_is_not_repeated_within_the_retry_interval(monkeypatch):
    sent: list[dict] = []
    client = RemoteControlClient()
    client._send_async = sent.append

    client.set_rfid_field(True)
    client.set_rfid_field(True)
    assert sent == [{"type": "rfid", "field": True}]

    client._rfid_field_sent_mono = monotonic_time() - _RFID_FIELD_RESEND_S - 0.1
    client.set_rfid_field(True)
    assert len(sent) == 2


def test_field_echo_allows_a_fresh_request():
    sent: list[dict] = []
    client = RemoteControlClient()
    client._send_async = sent.append
    client.set_rfid_field(True)

    asyncio.run(client._handle_json(json.dumps({"type": "state", "rfid_field": True})))
    client.set_rfid_field(True)
    assert len(sent) == 2


def test_hung_send_drops_the_socket(monkeypatch):
    monkeypatch.setattr("src.remote.control_client._WS_SEND_TIMEOUT_S", 0.2)
    client = RemoteControlClient()

    class Socket:
        def __init__(self):
            self.closed = False

        async def send(self, _payload: str):
            await asyncio.sleep(30)

        async def close(self):
            self.closed = True

    socket = Socket()
    client._ws = socket
    asyncio.run(client._send({"type": "ping"}))
    assert client._ws is None
    assert socket.closed is True
