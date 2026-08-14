"""Harness for pumping ``backend_main`` ticks without GPIO / camera / MQTT.

Unit tests use ``build_loop_context`` (all fakes + FakeClock).
On-target hardware tests use ``build_hardware_loop_context`` (real Magnets,
FakePir/FakeRfid, real wall time, injectable camera detections).
"""

from __future__ import annotations

import time as tm
from dataclasses import dataclass, field
from typing import Any, Callable

import src.baseconfig as baseconfig
import src.backend.mqtt_bridge as mqtt_bridge
from src.backend.loop import BackendLoopContext, backend_main
from src.camera import DetectedObject, image_buffer
from src.database import CatsRepo
from src.hardware_sim import FakeMagnets, FakePir, FakeRfid
from src.helper import sigterm_monitor
from src.magnets_rfid import RfidRunState

# Default poll budget for magnet GPIO (queue delay is MAG_RFID_CMD_DELAY=1s).
DEFAULT_MAGNET_WAIT_S = 8.0
DEFAULT_POLL_INTERVAL_S = 0.1


@dataclass
class FakeClock:
    """Controllable monotonic + wall clock; ``sleep`` advances mono instead of blocking."""

    mono: float = 1000.0
    wall: float = 1_700_000_000.0

    def monotonic(self) -> float:
        return float(self.mono)

    def wall_time(self) -> float:
        return float(self.wall)

    def sleep(self, seconds: float) -> None:
        self.advance(float(seconds))

    def advance(self, seconds: float) -> None:
        dt = float(seconds)
        self.mono += dt
        self.wall += dt


@dataclass
class LoopHarness:
    """Ready-to-pump backend loop with fake HW and clock."""

    ctx: BackendLoopContext
    clock: FakeClock | None
    pir: FakePir
    magnets: Any
    rfid: FakeRfid
    db_path: str
    _extra_shutdown: list = field(default_factory=list)
    _use_real_time: bool = False

    def pump(self, n: int = 1) -> None:
        for _ in range(int(n)):
            self.ctx.tick()

    def advance(self, seconds: float) -> None:
        if self._use_real_time or self.clock is None:
            tm.sleep(float(seconds))
        else:
            self.clock.advance(seconds)

    def set_outside(self, active: bool = True) -> None:
        self.pir.trigger_outside(active)

    def set_inside(self, active: bool = True, raw: bool | None = None) -> None:
        self.pir.trigger_inside(active, raw=raw)

    def inject_rfid(self, tag_id: str) -> None:
        ts = tm.time() if (self._use_real_time or self.clock is None) else self.clock.wall_time()
        self.rfid.inject_tag(tag_id, timestamp=ts)

    def clear_rfid(self) -> None:
        self.rfid.set_tag(None, 0.0)

    def set_manual_override(self, **flags: bool) -> None:
        for key, value in flags.items():
            mqtt_bridge.manual_door_override[key] = bool(value)

    def reset_manual_override(self) -> None:
        for key in list(mqtt_bridge.manual_door_override):
            mqtt_bridge.manual_door_override[key] = False

    def inject_detection(
        self,
        *,
        mouse: float = 0.0,
        no_mouse: float = 0.0,
        own_cat: float = 0.0,
        cat_name: str | None = None,
        cat_probability: float | None = None,
        timestamp: float | None = None,
        timestamp_mono: float | None = None,
    ) -> None:
        """Append a fake inference frame to the global ``image_buffer``."""
        wall = float(timestamp if timestamp is not None else tm.time())
        mono = float(timestamp_mono if timestamp_mono is not None else tm.monotonic())
        detected = None
        if cat_name:
            prob = float(cat_probability if cat_probability is not None else own_cat)
            detected = [
                DetectedObject(0.1, 0.1, 0.2, 0.2, str(cat_name), prob),
            ]
        image_buffer.append(
            wall,
            b"",
            b"",
            float(mouse),
            float(no_mouse),
            float(own_cat),
            detected_objects=detected,
            timestamp_mono=mono,
        )

    def wait_until(
        self,
        predicate: Callable[[], bool],
        *,
        timeout: float = DEFAULT_MAGNET_WAIT_S,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
        pump_each_poll: int = 2,
    ) -> bool:
        """Pump the loop and poll until ``predicate`` is true or timeout."""
        deadline = tm.monotonic() + float(timeout)
        while tm.monotonic() < deadline:
            if pump_each_poll:
                self.pump(pump_each_poll)
            if predicate():
                return True
            tm.sleep(float(poll_interval))
        return bool(predicate())

    def wait_inside(self, unlocked: bool = True, *, timeout: float = DEFAULT_MAGNET_WAIT_S) -> bool:
        """Wait until inside magnet matches ``unlocked`` (True = unlocked)."""
        return self.wait_until(
            lambda: self.magnets.get_inside_state() is bool(unlocked),
            timeout=timeout,
        )

    def wait_outside(self, unlocked: bool = True, *, timeout: float = DEFAULT_MAGNET_WAIT_S) -> bool:
        """Wait until outside magnet matches ``unlocked`` (True = unlocked)."""
        return self.wait_until(
            lambda: self.magnets.get_outside_state() is bool(unlocked),
            timeout=timeout,
        )

    def ensure_locked(self, *, timeout: float = DEFAULT_MAGNET_WAIT_S) -> None:
        """Force-lock both directions via empty_queue and wait until locked."""
        try:
            self.magnets.empty_queue(shutdown=False)
        except Exception:
            pass
        self.wait_until(
            lambda: (
                self.magnets.get_inside_state() is False
                and self.magnets.get_outside_state() is False
            ),
            timeout=timeout,
            pump_each_poll=0,
        )

    def run_for(self, seconds: float, *, pump_hz: float = 10.0) -> None:
        """Pump ticks for roughly ``seconds`` of real wall time."""
        deadline = tm.monotonic() + float(seconds)
        interval = 1.0 / max(float(pump_hz), 1.0)
        while tm.monotonic() < deadline:
            self.pump(1)
            tm.sleep(interval)

    def shutdown(self) -> None:
        try:
            try:
                self.ensure_locked()
            except Exception:
                pass
            self.ctx.shutdown()
        finally:
            image_buffer.clear()
            for fn in self._extra_shutdown:
                try:
                    fn()
                except Exception:
                    pass


def reset_sigterm_monitor() -> None:
    """Reset GracefulKiller so tests do not leak stop flags / task counts."""
    with sigterm_monitor.lock:
        sigterm_monitor.stop_now = False
        sigterm_monitor.tasks_count = 0
        sigterm_monitor._shutdown_started = False
        sigterm_monitor.tasks_done.set()


def _apply_default_door_config() -> None:
    """Neutral Door Control defaults for loop scenarios."""
    baseconfig.CONFIG["MQTT_ENABLED"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_MOTION_DETECTION"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = False
    baseconfig.CONFIG["REQUIRE_OUTSIDE_PIR_FOR_CAMERA_ENTRY"] = False
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 0.0
    baseconfig.CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"] = False
    for i in (1, 2, 3):
        baseconfig.CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = False


def _stabilize_aware_sleep(first_stabilize_s: float = 0.1) -> Callable[[float], None]:
    """Real ``sleep`` that shortens backend_main's fixed 5s sensor stabilize wait."""
    seen_stabilize = {"done": False}

    def _sleep(seconds: float) -> None:
        s = float(seconds)
        if not seen_stabilize["done"] and abs(s - 5.0) < 0.01:
            seen_stabilize["done"] = True
            tm.sleep(float(first_stabilize_s))
            return
        tm.sleep(s)

    return _sleep


def build_loop_context(
    *,
    db_path: str,
    cat_name: str = "Mia",
    cat_rfid: str = "CAT001",
    allow_entry: bool = True,
    allow_exit: bool = True,
    enable_prey_detection: bool = True,
    clock: FakeClock | None = None,
) -> LoopHarness:
    """Create a ``BackendLoopContext`` with fakes, seeded cat, model/MQTT/threads off."""
    reset_sigterm_monitor()
    mqtt_bridge.cleanup_mqtt()
    for key in list(mqtt_bridge.manual_door_override):
        mqtt_bridge.manual_door_override[key] = False

    baseconfig.CONFIG["KITTYHACK_DATABASE_PATH"] = db_path
    _apply_default_door_config()

    CatsRepo.db_add_new_cat(
        db_path,
        cat_name,
        cat_rfid,
        "",
        enable_prey_detection=enable_prey_detection,
        allow_entry=allow_entry,
        allow_exit=allow_exit,
    )

    clock = clock or FakeClock()
    pir = FakePir()
    magnets = FakeMagnets()
    rfid = FakeRfid()
    # Avoid tick path restarting RFID worker threads in tests.
    rfid.set_run_state(RfidRunState.running)

    ctx = backend_main(
        True,
        hardware=(pir, magnets, rfid),
        sleep_fn=clock.sleep,
        mono_fn=clock.monotonic,
        wall_fn=clock.wall_time,
        start_model=False,
        start_mqtt=False,
        start_hw_threads=False,
        run_forever=False,
    )
    assert isinstance(ctx, BackendLoopContext)

    return LoopHarness(
        ctx=ctx,
        clock=clock,
        pir=pir,
        magnets=magnets,
        rfid=rfid,
        db_path=db_path,
        _extra_shutdown=[reset_sigterm_monitor],
        _use_real_time=False,
    )


def build_hardware_loop_context(
    *,
    db_path: str,
    cat_name: str = "Mia",
    cat_rfid: str = "CAT001",
    allow_entry: bool = True,
    allow_exit: bool = True,
    enable_prey_detection: bool = True,
    stabilize_sleep_s: float = 0.1,
) -> LoopHarness:
    """Backend loop with real Magnets GPIO + injectable FakePir / FakeRfid.

    Uses real wall/monotonic time (magnet worker uses real ``time.sleep``).
    Call only on a Kittyflap with GPIO available and production services stopped.
    """
    from src.magnets_rfid import Magnets

    reset_sigterm_monitor()
    mqtt_bridge.cleanup_mqtt()
    image_buffer.clear()
    for key in list(mqtt_bridge.manual_door_override):
        mqtt_bridge.manual_door_override[key] = False

    # Clear stale prey lockout from a previous backend_main in this process.
    try:
        from src.backend import loop as loop_mod

        loop_mod.backend_main.prey_detection_tm = 0.0
        loop_mod.backend_main.prey_detection_mono = 0.0
    except Exception:
        pass

    baseconfig.CONFIG["KITTYHACK_DATABASE_PATH"] = db_path
    _apply_default_door_config()

    CatsRepo.db_add_new_cat(
        db_path,
        cat_name,
        cat_rfid,
        "",
        enable_prey_detection=enable_prey_detection,
        allow_entry=allow_entry,
        allow_exit=allow_exit,
    )

    pir = FakePir()
    magnets = Magnets(simulate_kittyflap=False)
    rfid = FakeRfid()
    rfid.set_run_state(RfidRunState.running)

    ctx = backend_main(
        False,
        hardware=(pir, magnets, rfid),
        sleep_fn=_stabilize_aware_sleep(stabilize_sleep_s),
        start_model=False,
        start_mqtt=False,
        start_hw_threads=False,
        run_forever=False,
    )
    assert isinstance(ctx, BackendLoopContext)

    harness = LoopHarness(
        ctx=ctx,
        clock=None,
        pir=pir,
        magnets=magnets,
        rfid=rfid,
        db_path=db_path,
        _extra_shutdown=[reset_sigterm_monitor],
        _use_real_time=True,
    )
    # Init queues lock_inside/lock_outside; wait until both are locked.
    harness.wait_until(
        lambda: (
            magnets.get_inside_state() is False
            and magnets.get_outside_state() is False
        ),
        timeout=DEFAULT_MAGNET_WAIT_S,
        pump_each_poll=0,
    )
    return harness
