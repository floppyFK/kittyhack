"""Shiny 1.2.1 workarounds for a shared reactive lock and session teardown.

Shiny holds the process-wide reactive lock across ``await session._flush()``, and
``_flush`` awaits the websocket write. A client that stops reading blocks every
other UI session until that socket dies. ``invalidate_later`` then prints
``CancelledError`` if those timer tasks were queued on the lock when the session
ended. Cancellation during ``asyncio.sleep`` is already silent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import traceback
from collections.abc import Callable
from typing import Optional

import shiny.reactive as reactive_api
from shiny.reactive import _core as reactive_core
from shiny.session._session import AppSession
from shiny.types import MISSING, MISSING_TYPE

logger = logging.getLogger(__name__)

# A client that stops reading must not queue output forever. Live status is
# every 0.25s, so 32 messages is a few seconds of backlog. 1 MiB bounds large
# live-view frames. Past either cap the session is closed and its timers stop.
MAX_QUEUED_MESSAGES = 32
MAX_QUEUED_BYTES = 1024 * 1024
_CLOSE_TIMEOUT_S = 2.0

_installed = False


def install() -> None:
    """Patch Shiny once. Safe to call again."""
    global _installed
    if _installed:
        return
    _installed = True
    _install_send_chain()
    _install_invalidate_later()


def _install_send_chain() -> None:
    AppSession._send_message = _send_message  # type: ignore[method-assign]


async def _send_message(self: AppSession, message: dict[str, object]) -> None:
    """Serialize ``message`` and write it without holding the reactive lock.

    Callers that already hold the lock (the output flush) return as soon as the
    payload is queued. Callers that do not (the initial config message) still
    wait until this payload has been written.
    """
    message_str = json.dumps(message)
    if self._debug:
        print(
            "SEND: "
            + re.sub(
                "(?m)base64,[a-zA-Z0-9+/=]+", "[base64 data]", message_str + "\n"
            ),
            end="",
            flush=True,
        )
    write_done = _enqueue_send(self, message_str)
    if reactive_core.lock().locked():
        return
    await write_done


def _ensure_send_state(session: AppSession) -> None:
    if getattr(session, "_kh_send_ready", False):
        return
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    done.set_result(None)
    session._kh_send_chain = done  # type: ignore[attr-defined]
    session._kh_send_pending_count = 0  # type: ignore[attr-defined]
    session._kh_send_pending_bytes = 0  # type: ignore[attr-defined]
    session._kh_send_overflow = False  # type: ignore[attr-defined]
    session._kh_send_ready = True  # type: ignore[attr-defined]


def _enqueue_send(session: AppSession, message_str: str) -> asyncio.Future[None]:
    _ensure_send_state(session)
    if session._kh_send_overflow:  # type: ignore[attr-defined]
        return _completed_future()

    nbytes = len(message_str.encode("utf-8"))
    next_count = session._kh_send_pending_count + 1  # type: ignore[attr-defined]
    next_bytes = session._kh_send_pending_bytes + nbytes  # type: ignore[attr-defined]
    if next_count > MAX_QUEUED_MESSAGES or next_bytes > MAX_QUEUED_BYTES:
        session._kh_send_overflow = True  # type: ignore[attr-defined]
        asyncio.create_task(
            _close_for_overflow(session),
            name="kittyhack-ws-overflow-close",
        )
        return _completed_future()

    session._kh_send_pending_count = next_count  # type: ignore[attr-defined]
    session._kh_send_pending_bytes = next_bytes  # type: ignore[attr-defined]
    prev: asyncio.Future[None] = session._kh_send_chain  # type: ignore[attr-defined]

    async def _write() -> None:
        nonlocal prev
        try:
            try:
                await prev
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[SESSION] Previous websocket write failed", exc_info=True)
            prev = _completed_future()
            if session._kh_send_overflow:  # type: ignore[attr-defined]
                return
            await session._conn.send(message_str)
        finally:
            session._kh_send_pending_count -= 1  # type: ignore[attr-defined]
            session._kh_send_pending_bytes -= nbytes  # type: ignore[attr-defined]

    task = asyncio.create_task(_write(), name="kittyhack-ws-send")
    session._kh_send_chain = task  # type: ignore[attr-defined]
    return task


def _completed_future() -> asyncio.Future[None]:
    fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    fut.set_result(None)
    return fut


async def _close_for_overflow(session: AppSession) -> None:
    logger.warning(
        "[SESSION] Closing UI session: outbound websocket queue is full "
        "(queued %s messages / %s bytes)",
        getattr(session, "_kh_send_pending_count", 0),
        getattr(session, "_kh_send_pending_bytes", 0),
    )
    conn = getattr(session, "_conn", None)
    # Close the transport before marking the wrapper closed. StarletteConnection.close
    # returns immediately once _closed is set and would leave the socket open.
    underlying = getattr(conn, "conn", None)
    if underlying is not None and hasattr(underlying, "close"):
        try:
            await asyncio.wait_for(underlying.close(1001), timeout=_CLOSE_TIMEOUT_S)
        except Exception:
            logger.debug(
                "[SESSION] Websocket close during overflow failed", exc_info=True
            )
    if conn is not None and hasattr(conn, "_closed"):
        conn._closed = True
    close = getattr(session, "close", None)
    if close is None:
        return
    try:
        await asyncio.wait_for(close(), timeout=_CLOSE_TIMEOUT_S)
    except Exception:
        logger.debug("[SESSION] Overflow close did not finish cleanly", exc_info=True)
        end = getattr(session, "_run_session_end_tasks", None)
        if end is not None:
            try:
                await end()
            except Exception:
                logger.debug(
                    "[SESSION] Session end tasks failed after overflow",
                    exc_info=True,
                )


def _install_invalidate_later() -> None:
    reactive_core.invalidate_later = _invalidate_later  # type: ignore[assignment]
    reactive_api.invalidate_later = _invalidate_later  # type: ignore[assignment]


def _invalidate_later(
    delay: float, *, session: object = MISSING
) -> None:
    """``invalidate_later`` that treats lock-queue cancellation as normal.

    Shiny 1.2.1 already returns on ``CancelledError`` during ``asyncio.sleep``.
    The same cancellation while waiting for the reactive lock is a
    ``BaseException`` and is printed. Session teardown does that whenever a
    flush still holds the lock.
    """
    if isinstance(session, MISSING_TYPE):
        from shiny.session import get_current_session

        session = get_current_session()

    ctx = reactive_core.get_current_context()
    deadline = time.monotonic() + delay

    cancellable = True
    unsub: Optional[Callable[[], None]] = None

    async def _task(ctx: reactive_core.Context, deadline: float) -> None:
        nonlocal cancellable
        try:
            remaining = deadline - time.monotonic()
            try:
                await asyncio.sleep(remaining)
            except asyncio.CancelledError:
                return

            try:
                async with reactive_core.lock():
                    # Once the lock is held, invalidating this context must not
                    # cancel the task that is about to flush it.
                    cancellable = False
                    ctx.invalidate()
                    await reactive_core.flush()
            except asyncio.CancelledError:
                return
        except BaseException:
            traceback.print_exc()
            raise
        finally:
            if unsub:
                unsub()

    task = asyncio.create_task(_task(ctx, deadline))

    def cancel_task() -> None:
        if cancellable and not task.cancelled():
            task.cancel()

    ctx.on_invalidate(cancel_task)
    if session is not None and not isinstance(session, MISSING_TYPE):
        unsub = session.on_ended(cancel_task)  # type: ignore[attr-defined]
