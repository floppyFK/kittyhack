"""Remote-mode stand-ins for Pir / Magnets / Rfid — proxy to the Kittyflap via RemoteControlClient."""
import logging
import time as tm

from src.helper import sigterm_monitor

from src.remote.control_client import RemoteControlClient


class Pir:
    """Remote PIR facade: motion states come from the target over the control channel."""

    instance = None

    def __init__(self, simulate_kittyflap: bool = False):
        """Bind to the shared remote control client (``simulate_kittyflap`` unused)."""
        self.simulate_kittyflap = simulate_kittyflap
        self._client = RemoteControlClient.instance()

    def init(self):
        """Register singleton and ensure the remote control client is started."""
        Pir.instance = self
        self._client.ensure_started()
        if not self._client.wait_until_ready(timeout=30.0):
            logging.warning("[PIR_REMOTE] Remote control not ready yet; PIR states may remain 0.")

    def read(self):
        """Idle loop so the backend can treat PIR as a long-lived thread (no local GPIO)."""
        # Keep thread alive so backend can join/stop cleanly.
        self._client.ensure_started()
        while not sigterm_monitor.stop_now:
            tm.sleep(0.2)

    def get_states(self):
        """Return ``(outside, inside, outside_raw, inside_raw)`` from the latest remote snapshot."""
        s = self._client.get_states()
        return s.pir_outside, s.pir_inside, s.pir_outside_raw, s.pir_inside_raw

    def update_state(self, pir, state):
        """No-op in remote mode (PIR state is owned by the target)."""
        pass


class Magnets:
    """Remote magnet/door facade: commands and lock state go through RemoteControlClient."""

    instance = None

    def __init__(self, simulate_kittyflap: bool = False):
        """Bind to the shared remote control client (``simulate_kittyflap`` unused)."""
        self.simulate_kittyflap = simulate_kittyflap
        self._client = RemoteControlClient.instance()
        self._magnet_state_inside = False
        self._magnet_state_outside = False

    def init(self):
        """Register singleton and ensure the remote control client is started."""
        Magnets.instance = self
        self._client.ensure_started()
        if not self._client.wait_until_ready(timeout=30.0):
            logging.warning("[MAGNETS_REMOTE] Remote control not ready yet; commands may be dropped.")

    def start_magnet_control(self):
        """No local queue thread; the target enforces GPIO safety timing."""
        pass

    def get_outside_state(self) -> bool:
        """True if the outside direction is unlocked (remote snapshot)."""
        return self._client.get_states().lock_outside_unlocked

    def get_inside_state(self) -> bool:
        """True if the inside direction is unlocked (remote snapshot)."""
        return self._client.get_states().lock_inside_unlocked

    def queue_command(self, command: str):
        """Forward a magnet command (``unlock_inside`` / ``lock_inside`` / …) to the target."""
        self._client.queue_magnet_command(command)

    def check_queued(self, command: str):
        """True if the remote client still has a pending intent for this magnet command."""
        return self._client.is_magnet_command_pending(command)

    def empty_queue(self, shutdown: bool = False):
        """Clear local pending magnet intents (remote target queue is not cancelable)."""
        # Remote target queue is not directly introspectable/cancelable.
        # Clear local pending intents so backend logic can issue the next action cleanly.
        self._client.clear_pending_magnet_commands()


class RfidRunState:
    """Run-state constants matching on-device ``RfidRunState`` (stopped / running / stop_requested)."""

    stopped = 0
    running = 1
    stop_requested = 2


class Rfid:
    """Remote RFID facade: field/power/tag state is controlled on the Kittyflap target."""

    def __init__(self, simulate_kittyflap: bool = False):
        """Bind to the shared remote control client and start it."""
        self.simulate_kittyflap = simulate_kittyflap
        self._client = RemoteControlClient.instance()
        self._client.ensure_started()

    def init(self):
        """Ensure the remote control client is running."""
        self._client.ensure_started()

    def run(self, read_cycles: int = 0):
        """Idle loop; the target performs RFID reads and streams tag state."""
        self._client.ensure_started()
        while not sigterm_monitor.stop_now:
            tm.sleep(0.2)

    def get_tag(self):
        """Return ``(tag_id, timestamp)`` from the latest remote RFID snapshot."""
        return self._client.get_tag()

    def set_tag(self, tag_id, timestamp):
        """Clear the target's stored tag when ``tag_id`` is empty; ignore non-empty spoof sets."""
        # Backend uses this to clear tags after timeouts / block ends.
        # In remote-mode the RFID reader lives on the target device, so we
        # must instruct the target to clear its stored tag state.
        if tag_id is None or str(tag_id).strip() == "":
            self._client.clear_rfid_tag()
        # Ignore non-empty tag sets (remote controller must not spoof tags).
        return

    def get_run_state(self):
        """Always report running; local RFID thread is only a keepalive."""
        return RfidRunState.running

    def set_run_state(self, __):
        """No-op; run state is owned by the target reader."""
        return

    def stop_read(self, wait_for_stop: bool = False):
        """Ask the target to stop its RFID read loop."""
        self._client.stop_read()

    def set_field(self, state: bool):
        """Enable or disable the RFID antenna field on the target."""
        self._client.set_rfid_field(state)

    def get_field(self):
        """Return whether the remote RFID field is reported enabled."""
        return self._client.get_field()

    def set_power(self, state: bool):
        """Enable or disable RFID module power on the target."""
        self._client.set_rfid_power(state)
