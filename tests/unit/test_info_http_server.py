"""Info HTTP server on the target must release port 80 while /video is open."""

from __future__ import annotations

import asyncio
import socket
import time

import pytest

import src.kittyhack_control as control


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _reset_info_http_state() -> None:
    control.STATE.http_server = None
    control._info_http_stop = None
    control._info_http_restart_not_before = 0.0


async def _read_http_headers(reader: asyncio.StreamReader) -> bytes:
    status = await asyncio.wait_for(reader.readline(), timeout=2)
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=2)
        if line in (b"\r\n", b"\n", b""):
            break
    return status


@pytest.fixture
def info_http_port(monkeypatch):
    _reset_info_http_state()
    port = _free_tcp_port()
    monkeypatch.setattr(control, "_INFO_HTTP_PORT", port)
    monkeypatch.setattr(control, "_ensure_internal_camera_stream", lambda: (True, ""))
    monkeypatch.setattr(control, "_internal_camera_latest_frame", lambda: None)
    yield port
    _reset_info_http_state()


def test_stop_info_http_server_returns_while_video_client_sleeps(info_http_port):
    """A /video client with no frames must not pin wait_closed()."""

    async def scenario() -> None:
        await control._start_info_http_server()
        assert control._info_http_server_is_listening()
        reader, writer = await asyncio.open_connection("127.0.0.1", info_http_port)
        try:
            writer.write(b"GET /video HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            status = await _read_http_headers(reader)
            assert status.startswith(b"HTTP/1.1 200")
            # Handler is in the frame-is-None wait, the state that used to hang release.
            await asyncio.sleep(0.1)

            started = time.monotonic()
            await control._stop_info_http_server()
            elapsed = time.monotonic() - started

            assert elapsed < 2.0
            assert control.STATE.http_server is None
            assert not control._info_http_server_is_listening()
            # The listen socket is gone, and the sleeping handler was woken.
            with pytest.raises(OSError):
                _probe = socket.create_connection(("127.0.0.1", info_http_port), timeout=1)
                _probe.close()
        finally:
            writer.close()
            await writer.wait_closed()
            await control._stop_info_http_server()

    asyncio.run(scenario())


def test_start_replaces_closed_server_and_accepts_connections(info_http_port, caplog):
    """A closed server object must not make the next start a silent no-op."""

    async def scenario() -> None:
        await control._start_info_http_server()
        stale = control.STATE.http_server
        assert stale is not None
        stale.close()
        await stale.wait_closed()
        assert control.STATE.http_server is stale
        assert not control._info_http_server_is_listening()

        with caplog.at_level("WARNING"):
            await control._start_info_http_server()

        assert control._info_http_server_is_listening()
        assert control.STATE.http_server is not stale
        assert "Info page server is not listening; restarting it." in caplog.text

        reader, writer = await asyncio.open_connection("127.0.0.1", info_http_port)
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            await writer.drain()
            status = await _read_http_headers(reader)
            assert status.startswith(b"HTTP/1.1 200")
        finally:
            writer.close()
            await writer.wait_closed()
            await control._stop_info_http_server()

    asyncio.run(scenario())


def test_watchdog_heal_restarts_dead_server_but_rate_limits(info_http_port):
    """The watchdog rebinds a dead server object, then waits before trying again."""

    async def scenario() -> None:
        await control._start_info_http_server()
        first = control.STATE.http_server
        assert first is not None
        first.close()
        await first.wait_closed()

        await control._heal_info_http_server_if_dead(now=1_000.0)
        assert control._info_http_server_is_listening()
        healed = control.STATE.http_server
        assert healed is not None and healed is not first

        healed.close()
        await healed.wait_closed()
        assert control.STATE.http_server is healed
        assert not control._info_http_server_is_listening()

        await control._heal_info_http_server_if_dead(now=1_001.0)
        assert control.STATE.http_server is healed
        assert not control._info_http_server_is_listening()

        await control._heal_info_http_server_if_dead(now=1_005.0)
        assert control._info_http_server_is_listening()
        assert control.STATE.http_server is not healed
        await control._stop_info_http_server()

    asyncio.run(scenario())
