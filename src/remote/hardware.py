import logging
import time as tm

from src.helper import sigterm_monitor

from src.remote.control_client import RemoteControlClient


class Pir:
    instance = None

    def __init__(self, simulate_kittyflap: bool = False):
        self.simulate_kittyflap = simulate_kittyflap
        self._client = RemoteControlClient.instance()

    def init(self):
        Pir.instance = self
        self._client.ensure_started()
        if not self._client.wait_until_ready(timeout=30.0):
            logging.warning("[PIR_REMOTE] Remote control not ready yet; PIR states may remain 0.")

    def read(self):
        # Keep thread alive so backend can join/stop cleanly.
        self._client.ensure_started()
        while not sigterm_monitor.stop_now:
            tm.sleep(0.2)

    def get_states(self):
        s = self._client.get_states()
        return s.pir_outside, s.pir_inside, s.pir_outside_raw, s.pir_inside_raw

    def update_state(self, pir, state):
        # Not used in remote mode
        pass


class Magnets:
    instance = None

    def __init__(self, simulate_kittyflap: bool = False):
        self.simulate_kittyflap = simulate_kittyflap
        self._client = RemoteControlClient.instance()
        self._magnet_state_inside = False
        self._magnet_state_outside = False

    def init(self):
        Magnets.instance = self
        self._client.ensure_started()
        if not self._client.wait_until_ready(timeout=30.0):
            logging.warning("[MAGNETS_REMOTE] Remote control not ready yet; commands may be dropped.")

    def start_magnet_control(self):
        # no local thread; target enforces its own GPIO safety queue
        pass

    def get_outside_state(self) -> bool:
        return self._client.get_states().lock_outside_unlocked

    def get_inside_state(self) -> bool:
        return self._client.get_states().lock_inside_unlocked

    def queue_command(self, command: str):
        self._client.queue_magnet_command(command)

    def check_queued(self, command: str):
        # We don't have target queue introspection yet. Keep behavior simple.
        return False

    def empty_queue(self, shutdown: bool = False):
        # Best-effort: do nothing. Target queue is responsible for safe sequencing.
        return


class RfidRunState:
    stopped = 0
    running = 1
    stop_requested = 2


class Rfid:
    def __init__(self, simulate_kittyflap: bool = False):
        self.simulate_kittyflap = simulate_kittyflap
        self._client = RemoteControlClient.instance()
        self._client.ensure_started()

    def init(self):
        self._client.ensure_started()

    def run(self, read_cycles: int = 0):
        # Keep thread alive; target reads RFID and streams tag state.
        self._client.ensure_started()
        while not sigterm_monitor.stop_now:
            tm.sleep(0.2)

    def get_tag(self):
        return self._client.get_tag()

    def set_tag(self, tag_id, timestamp):
        # Backend uses this to clear; we ignore and rely on target stream.
        return

    def get_run_state(self):
        return RfidRunState.running

    def set_run_state(self, __):
        return

    def stop_read(self, wait_for_stop: bool = False):
        self._client.stop_read()

    def set_field(self, state: bool):
        self._client.set_rfid_field(state)

    def get_field(self):
        return self._client.get_field()

    def set_power(self, state: bool):
        self._client.set_rfid_power(state)
