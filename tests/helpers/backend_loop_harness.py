"""Harness for pumping ``backend_main`` ticks without GPIO / camera / MQTT."""

from __future__ import annotations

from dataclasses import dataclass, field

import src.baseconfig as baseconfig
import src.backend.mqtt_bridge as mqtt_bridge
from src.backend.loop import BackendLoopContext, backend_main
from src.database import CatsRepo
from src.hardware_sim import FakeMagnets, FakePir, FakeRfid
from src.helper import sigterm_monitor
from src.magnets_rfid import RfidRunState


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
    clock: FakeClock
    pir: FakePir
    magnets: FakeMagnets
    rfid: FakeRfid
    db_path: str
    _extra_shutdown: list = field(default_factory=list)

    def pump(self, n: int = 1) -> None:
        for _ in range(int(n)):
            self.ctx.tick()

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def set_outside(self, active: bool = True) -> None:
        self.pir.trigger_outside(active)

    def set_inside(self, active: bool = True, raw: bool | None = None) -> None:
        self.pir.trigger_inside(active, raw=raw)

    def inject_rfid(self, tag_id: str) -> None:
        self.rfid.inject_tag(tag_id, timestamp=self.clock.wall_time())

    def clear_rfid(self) -> None:
        self.rfid.set_tag(None, 0.0)

    def set_manual_override(self, **flags: bool) -> None:
        for key, value in flags.items():
            mqtt_bridge.manual_door_override[key] = bool(value)

    def reset_manual_override(self) -> None:
        for key in list(mqtt_bridge.manual_door_override):
            mqtt_bridge.manual_door_override[key] = False

    def shutdown(self) -> None:
        try:
            self.ctx.shutdown()
        finally:
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
    baseconfig.CONFIG["MQTT_ENABLED"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_MOTION_DETECTION"] = False
    baseconfig.CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = False
    baseconfig.CONFIG["MOUSE_CHECK_ENABLED"] = False
    baseconfig.CONFIG["MIN_SECONDS_TO_ANALYZE"] = 0.0
    baseconfig.CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"] = False

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
    )
