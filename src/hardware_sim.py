"""Injectable fake Pir / Magnets / Rfid for simulate mode and unit tests."""

from __future__ import annotations

import logging
import threading
import time as tm

from src.helper import sigterm_monitor
from src.magnets_rfid import RfidRunState


class FakePir:
    """Controllable PIR stand-in (no GPIO)."""

    instance = None

    def __init__(self, simulate_kittyflap: bool = True, stop_event: threading.Event | None = None):
        self.simulate_kittyflap = True
        self._stop_event = stop_event
        self.state_outside = 0
        self.state_inside = 0
        self.state_outside_raw = 0
        self.state_inside_raw = 0
        self.thread_lock = threading.Lock()

    def init(self):
        """Register as singleton (also on real ``Pir.instance`` for MQTT/API)."""
        FakePir.instance = self
        try:
            from src.pir import Pir
            Pir.instance = self
        except Exception:
            pass
        logging.info("[PIR_FAKE] Fake PIR initialized.")

    def trigger_outside(self, active: bool = True, raw: bool | None = None):
        """Set outside motion for tests (filtered + raw unless ``raw`` overridden)."""
        with self.thread_lock:
            self.state_outside = 1 if active else 0
            self.state_outside_raw = self.state_outside if raw is None else (1 if raw else 0)

    def trigger_inside(self, active: bool = True, raw: bool | None = None):
        """Set inside motion for tests."""
        with self.thread_lock:
            self.state_inside = 1 if active else 0
            self.state_inside_raw = self.state_inside if raw is None else (1 if raw else 0)

    def read(self):
        """Idle until shutdown (states are set via ``trigger_*``)."""
        sigterm_monitor.register_task()
        while not sigterm_monitor.stop_now and not (self._stop_event and self._stop_event.is_set()):
            tm.sleep(0.05)
        logging.info("[PIR_FAKE] Stopped fake PIR monitoring thread.")
        sigterm_monitor.signal_task_done()

    def update_state(self, pir, state):
        """Thread-safe write of filtered state (``OUTSIDE`` / ``INSIDE``)."""
        with self.thread_lock:
            if pir == "OUTSIDE":
                self.state_outside = state
                self.state_outside_raw = state
            elif pir == "INSIDE":
                self.state_inside = state
                self.state_inside_raw = state

    def get_states(self):
        """Return ``(outside, inside, outside_raw, inside_raw)``."""
        with self.thread_lock:
            return (
                self.state_outside,
                self.state_inside,
                self.state_outside_raw,
                self.state_inside_raw,
            )


class FakeMagnets:
    """In-memory magnet stand-in (immediate commands, no GPIO queue delay)."""

    instance = None

    def __init__(self, simulate_kittyflap: bool = True):
        self.simulate_kittyflap = True
        self._inside_unlocked = False
        self._outside_unlocked = False
        self._pending: list[str] = []
        self._lock = threading.Lock()

    def init(self):
        """Register singleton and start locked (also on real ``Magnets.instance``)."""
        FakeMagnets.instance = self
        try:
            from src.magnets_rfid import Magnets
            Magnets.instance = self
        except Exception:
            pass
        self._inside_unlocked = False
        self._outside_unlocked = False
        logging.info("[MAGNETS_FAKE] Fake magnets initialized (locked).")

    def start_magnet_control(self):
        """No-op (commands apply immediately)."""
        return

    def get_outside_state(self) -> bool:
        """True if outside is unlocked."""
        with self._lock:
            return self._outside_unlocked

    def get_inside_state(self) -> bool:
        """True if inside is unlocked."""
        with self._lock:
            return self._inside_unlocked

    def queue_command(self, command: str):
        """Apply unlock/lock immediately."""
        with self._lock:
            if command == "unlock_inside":
                self._inside_unlocked = True
            elif command == "lock_inside":
                self._inside_unlocked = False
            elif command == "unlock_outside":
                self._outside_unlocked = True
            elif command == "lock_outside":
                self._outside_unlocked = False
            else:
                logging.error(f"[MAGNETS_FAKE] Unknown command: {command}")
                return
            logging.info(f"[MAGNETS_FAKE] Applied '{command}'.")

    def check_queued(self, command: str) -> bool:
        """Always False (commands are synchronous)."""
        return False

    def empty_queue(self, shutdown: bool = False):
        """Lock both directions."""
        with self._lock:
            self._inside_unlocked = False
            self._outside_unlocked = False
            self._pending.clear()
        logging.info("[MAGNETS_FAKE] Queue emptied; both directions locked.")


class FakeRfid:
    """RFID stand-in with injectable tags (no serial/GPIO)."""

    def __init__(self, simulate_kittyflap: bool = True):
        self.simulate_kittyflap = True
        self.tag_id = None
        self.timestamp = 0.0
        self.rfid_run_state = RfidRunState.stopped
        self.field_state = False
        self.thread_lock = threading.Lock()
        self._injected: list[tuple[str, float]] = []

    def init(self):
        """No hardware setup."""
        logging.info("[RFID_FAKE] Fake RFID initialized.")

    def inject_tag(self, tag_id: str, timestamp: float | None = None):
        """Queue a tag for the next ``get_tag`` / idle ``run`` loop."""
        ts = float(timestamp if timestamp is not None else tm.time())
        with self.thread_lock:
            self._injected.append((str(tag_id), ts))
            self.tag_id = str(tag_id)
            self.timestamp = ts

    def set_power(self, state: bool):
        """No-op power control."""
        logging.info(f"[RFID_FAKE] Power {'on' if state else 'off'} (simulated).")

    def set_field(self, state: bool):
        """Set software field flag."""
        self.field_state = bool(state)
        logging.info(f"[RFID_FAKE] Field {'on' if state else 'off'} (simulated).")

    def get_field(self):
        """Return software field flag."""
        return self.field_state

    def run(self, read_cycles: int = 0):
        """Idle loop; tags come from ``inject_tag`` / ``set_tag``."""
        if self.get_run_state() in [RfidRunState.running, RfidRunState.stop_requested]:
            logging.error("[RFID_FAKE] Another RFID read operation is already running.")
            return
        try:
            self.set_run_state(RfidRunState.running)
            cycle = 0
            while read_cycles == 0 or cycle < read_cycles:
                if self.get_run_state() == RfidRunState.stop_requested:
                    break
                with self.thread_lock:
                    if self._injected:
                        tag_id, ts = self._injected.pop(0)
                        self.tag_id = tag_id
                        self.timestamp = ts
                tm.sleep(0.05)
                cycle += 1
        finally:
            self.set_field(False)
            self.set_run_state(RfidRunState.stopped)

    def stop_read(self, wait_for_stop: bool = True):
        """Request stop of the idle read loop (no-op wait without a ``run`` thread)."""
        self.set_run_state(RfidRunState.stop_requested)
        # FakeRfid does not require a live ``run()`` thread; always settle to stopped
        # so shutdown paths (backend_main) never block.
        self.set_run_state(RfidRunState.stopped)

    def time_delta_to_last_read(self):
        """Seconds since last tag timestamp."""
        with self.thread_lock:
            return tm.time() - self.timestamp

    def set_run_state(self, state: RfidRunState):
        """Set run state."""
        with self.thread_lock:
            if state == RfidRunState.stop_requested and self.rfid_run_state == RfidRunState.stopped:
                return
            self.rfid_run_state = state

    def get_run_state(self):
        """Current run state."""
        with self.thread_lock:
            return self.rfid_run_state

    def get_tag(self):
        """Return ``(tag_id, timestamp)``."""
        with self.thread_lock:
            return self.tag_id, self.timestamp

    def set_tag(self, tag_id, timestamp):
        """Set or clear the current tag."""
        with self.thread_lock:
            self.tag_id = tag_id
            self.timestamp = timestamp


def create_hardware(simulate: bool | None = None, stop_event: threading.Event | None = None):
    """Return ``(Pir, Magnets, Rfid)`` classes/instances appropriate for this process.

    In remote mode, always use remote facades. In simulate mode, use fakes.
    Otherwise use on-device GPIO drivers.

    Returns:
        tuple: ``(pir, magnets, rfid)`` already constructed (not yet necessarily ``init()``'d).
    """
    from src.mode import is_remote_mode
    from src.runtime_flags import is_simulate_mode

    if simulate is None:
        simulate = is_simulate_mode()

    if is_remote_mode():
        from src.remote.hardware import Magnets, Pir, Rfid

        return (
            Pir(simulate_kittyflap=False),
            Magnets(simulate_kittyflap=False),
            Rfid(simulate_kittyflap=False),
        )

    if simulate:
        return (
            FakePir(stop_event=stop_event),
            FakeMagnets(),
            FakeRfid(),
        )

    from src.magnets_rfid import Magnets, Rfid
    from src.pir import Pir

    return (
        Pir(simulate_kittyflap=False, stop_event=stop_event),
        Magnets(simulate_kittyflap=False),
        Rfid(simulate_kittyflap=False),
    )
