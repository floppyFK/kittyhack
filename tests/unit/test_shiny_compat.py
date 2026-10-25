"""Shiny 1.2.1 patches: websocket sends must not hold the reactive lock."""

from __future__ import annotations

import asyncio
import json
from contextlib import redirect_stderr
from io import StringIO

import pytest

from src import shiny_compat
from src.shiny_compat import _send_message, install


@pytest.fixture(autouse=True)
def _installed_and_fresh_lock():
    install()
    yield
    from shiny.reactive._core import _reactive_environment

    # asyncio.Lock is bound to the loop that created it. Each test uses its own loop.
    _reactive_environment._lock = None


class _Conn:
    def __init__(self, *, block: bool) -> None:
        self.block = block
        self.messages: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._closed = False

    async def send(self, message: str) -> None:
        self.messages.append(message)
        self.started.set()
        if self.block:
            await self.release.wait()


class _Session:
    _debug = False

    def __init__(self, *, block: bool) -> None:
        self._conn = _Conn(block=block)
        self.close_called = asyncio.Event()

    async def close(self, code: int = 1001) -> None:
        self.close_called.set()


def _lock_waiter_count() -> int:
    from shiny.reactive._core import _reactive_environment

    lock = _reactive_environment._lock
    waiters = getattr(lock, "_waiters", None)
    if not waiters:
        return 0
    return sum(1 for waiter in waiters if not waiter.done())


def test_blocked_send_does_not_hold_the_reactive_lock():
    """A full socket must not keep the process-wide lock, so another session can flush."""

    async def scenario() -> None:
        from shiny import reactive

        stuck = _Session(block=True)
        other = _Session(block=True)
        async with reactive.lock():
            queued = asyncio.create_task(_send_message(stuck, {"n": 1}))
            await asyncio.wait_for(queued, timeout=1)
            await asyncio.wait_for(stuck._conn.started.wait(), timeout=1)
            assert stuck._conn.messages == [json.dumps({"n": 1})]

            queued_other = asyncio.create_task(_send_message(other, {"n": 2}))
            await asyncio.wait_for(queued_other, timeout=1)

        # Lock is released while the first socket write is still blocked.
        await asyncio.wait_for(other._conn.started.wait(), timeout=1)
        assert other._conn.messages == [json.dumps({"n": 2})]
        assert not stuck._conn.release.is_set()
        stuck._conn.release.set()
        other._conn.release.set()
        # Let the write tasks finish so they are not destroyed while pending.
        for _ in range(5):
            await asyncio.sleep(0)
        assert stuck._kh_send_pending_count == 0
        assert other._kh_send_pending_count == 0

    asyncio.run(scenario())


def test_send_waits_for_the_socket_when_the_lock_is_free():
    async def scenario() -> None:
        session = _Session(block=True)
        send_task = asyncio.create_task(_send_message(session, {"n": 1}))
        await asyncio.wait_for(session._conn.started.wait(), timeout=1)
        assert not send_task.done()
        session._conn.release.set()
        await asyncio.wait_for(send_task, timeout=1)
        assert session._conn.messages == [json.dumps({"n": 1})]

    asyncio.run(scenario())


def test_send_chain_keeps_message_order():
    async def scenario() -> None:
        from shiny import reactive

        session = _Session(block=False)
        async with reactive.lock():
            await _send_message(session, {"n": 1})
            await _send_message(session, {"n": 2})
        for _ in range(10):
            if session._kh_send_pending_count == 0 and len(session._conn.messages) == 2:
                break
            await asyncio.sleep(0)
        assert session._conn.messages == [json.dumps({"n": 1}), json.dumps({"n": 2})]

    asyncio.run(scenario())


def test_full_outbound_queue_closes_the_session(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shiny_compat, "MAX_QUEUED_MESSAGES", 2)

    async def scenario() -> None:
        from shiny import reactive

        session = _Session(block=True)
        async with reactive.lock():
            await _send_message(session, {"n": 1})
            await _send_message(session, {"n": 2})
            await _send_message(session, {"n": 3})
        await asyncio.wait_for(session.close_called.wait(), timeout=1)
        assert session._conn._closed is True
        session._conn.release.set()
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_oversized_payload_closes_without_sending(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shiny_compat, "MAX_QUEUED_BYTES", 8)

    async def scenario() -> None:
        from shiny import reactive

        session = _Session(block=False)
        async with reactive.lock():
            await _send_message(session, {"n": "too-large"})
        await asyncio.wait_for(session.close_called.wait(), timeout=1)
        assert session._conn.messages == []

    asyncio.run(scenario())


def test_invalidate_later_cancellation_on_the_lock_is_silent():
    async def scenario() -> None:
        from shiny import reactive
        from shiny.reactive._core import Context

        release = asyncio.Event()

        async def hold_lock() -> None:
            async with reactive.lock():
                await release.wait()

        holder = asyncio.create_task(hold_lock())
        for _ in range(10):
            await asyncio.sleep(0)
            if reactive.lock().locked():
                break
        assert reactive.lock().locked()

        ctx = Context()
        with ctx():
            reactive.invalidate_later(0)

        for _ in range(20):
            if _lock_waiter_count() >= 1:
                break
            await asyncio.sleep(0)
        assert _lock_waiter_count() >= 1

        stderr = StringIO()
        with redirect_stderr(stderr):
            ctx.invalidate()
            for _ in range(10):
                await asyncio.sleep(0)
        release.set()
        await holder

        logged = stderr.getvalue()
        assert "CancelledError" not in logged
        assert "Traceback" not in logged

    asyncio.run(scenario())


def test_invalidate_later_still_fires_when_not_cancelled():
    async def scenario() -> None:
        from shiny.reactive._core import Context

        ctx = Context()
        with ctx():
            from shiny import reactive

            reactive.invalidate_later(0)
        for _ in range(20):
            if ctx._invalidated:
                break
            await asyncio.sleep(0)
        assert ctx._invalidated is True

    asyncio.run(scenario())
