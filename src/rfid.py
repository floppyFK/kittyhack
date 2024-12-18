import os
import threading
import select
import time as tm
import logging
import random
import re
from enum import Enum
from src.system import Gpio

# GPIO pin numbers and directions
RFID_FIELD_NUM = 529
RFID_FIELD_DIR = "out"
RFID_POWER_NUM = 515
RFID_POWER_DIR = "out"

# Create a Gpio instance
gpio = Gpio()

RFID_READ_PATH = "/dev/serial0"

class RfidRunState(Enum):
    stopped = 0
    running = 1
    stop_requested = 2

class Rfid:
    def __init__(self, simulate_kittyflap=False):
        self.simulate_kittyflap = simulate_kittyflap
        self.tag_id = None
        self.timestamp = 0.0
        self.rfid_run_state = RfidRunState.stopped
        self.thread_lock = threading.Lock()
        self.init()

    def init(self):
        """
        Enable RFID reader.
        """
        if self.simulate_kittyflap:
            logging.info("[RFID] Simulation mode enabled. RFID is not powered on.")
        else:
            try:
                # Configure GPIO pins for RFID
                gpio.configure(RFID_POWER_NUM, RFID_POWER_DIR)
                gpio.configure(RFID_FIELD_NUM, RFID_FIELD_DIR)

                # Ensure RFID is powered off to avoid unnecessary interference
                gpio.set(RFID_POWER_NUM, 0)
                gpio.set(RFID_FIELD_NUM, 0)
            except Exception as e:
                logging.error(f"[RFID] Error initializing RFID: {e}")
            else:
                logging.info("[RFID] RFID initialized and powered on.")

    def set_power(self, state: bool):
        """
        Sets the power state of the RFID module.
        
        Args:
            state (bool): Desired power state of the RFID module. 
                          True to enable power, False to disable power.
        """
        if self.simulate_kittyflap:
            logging.info(f"[RFID] Simulation: RFID power would be {'enabled' if state else 'disabled'}.")
            return

        try:
            gpio.set(RFID_POWER_NUM, 1 if state else 0)
        except Exception as e:
            logging.error(f"[RFID] Error setting RFID power: {e}")
        else:
            logging.info(f"[RFID] RFID power {'enabled' if state else 'disabled'}.")

    def set_field(self, state: bool):
        """
        Sets the RFID field state by controlling the GPIO pin.
        
        Args:
            state (bool): Desired state of the RFID field. True to enable, False to disable.
        """
        if self.simulate_kittyflap:
            logging.info(f"[RFID] Simulation: RFID field would be {'enabled' if state else 'disabled'}.")
            return

        try:
            gpio.set(RFID_FIELD_NUM, 1 if state else 0)
        except Exception as e:
            logging.error(f"[RFID] Error setting RFID field: {e}")
        else:
            logging.info(f"[RFID] RFID field {'enabled' if state else 'disabled'}.")

    def run(self, read_cycles=0):
        """
        Reads RFID tags either from a simulated environment or from a real RFID reader.
        
        Args:
            read_cycles (int): The number of read cycles to perform. If set to 0, the function will read indefinitely.
        
        Simulated Mode:
            If SIMULATE_KITTYFLAP is True, the function will simulate reading an RFID tag by generating a fixed tag ID
            and waiting for a random delay between 0.5 and 15.0 seconds between reads.
        
        Real Mode:
            If SIMULATE_KITTYFLAP is False, the function will read from the RFID reader specified by RFID_READ_PATH.
            It waits for a tag to be detected and reads the tag ID, removing any non-hexadecimal characters from the ID.
            The function logs the tag ID and the timestamp of each read operation.
        
        Raises:
            Exception: If an error occurs while reading from the RFID reader, it logs the error and returns None.
        """
        if self.get_run_state() in [RfidRunState.running, RfidRunState.stop_requested]:
            logging.error("[RFID] Another RFID read operation is already running.")
            return
        else:
            logging.info(f"[RFID] Starting RFID read operation (read cycles: {read_cycles if read_cycles != 0 else '∞'})")

        try:
            self.set_field(True)
            self.set_run_state(RfidRunState.running)

            if self.simulate_kittyflap:
                tag_id = "BAADF00DBAADFEED"
                cycle = 0
                while read_cycles == 0 or cycle < read_cycles:
                    delay = random.uniform(0.5, 15.0)
                    tm.sleep(delay)
                    logging.info(f"[RFID] Simulation: Tag ID: {tag_id} (read cycle {cycle+1}/{read_cycles if read_cycles != 0 else '∞'})")
                    timestamp = tm.time()
                    self.set_tag(tag_id, timestamp)
                    cycle += 1
                    if self.get_run_state() == RfidRunState.stop_requested:
                        break
                return

            logging.info(f"[RFID] Waiting for RFID tag... (max {read_cycles if read_cycles != 0 else '∞'} cycles)")
            try:
                with open(RFID_READ_PATH, "r") as f:
                    cycle = 0
                    while read_cycles == 0 or cycle < read_cycles:
                        logging.debug(f"[RFID] Cycle {cycle+1}/{read_cycles if read_cycles != 0 else '∞'} | RFID run state: {self.get_run_state()}")
                        if self.get_run_state() == RfidRunState.stop_requested:
                            break
                        ready, _, _ = select.select([f], [], [], 1.0)
                        if ready:
                            tag_id = f.readline().strip()
                            if tag_id:
                                # Remove non-hex characters from the tag ID
                                tag_id = re.sub(r'[^0-9A-Fa-f]', '', tag_id)
                                timestamp = tm.time()
                                self.set_tag(tag_id, tm.time())
                                logging.info(f"[RFID] Tag ID: '{tag_id}' detected at {timestamp} (read cycle {cycle+1}/{read_cycles if read_cycles != 0 else '∞'})")

                        tm.sleep(0.1)
                        cycle += 1
            except Exception as e:
                logging.error(f"[RFID] Error reading RFID: {e}")
                return
            if read_cycles != 0:
                logging.info(f"[RFID] Max read cycles reached. Ending RFID read.")
            return

        finally:
            # Power off the RFID field
            self.set_field(False)
            self.set_run_state(RfidRunState.stopped)
            logging.info("[RFID] RFID read operation stopped.")

    def stop_read(self, wait_for_stop=True):
        """
        Stops the RFID read operation.
        """
        self.set_run_state(RfidRunState.stop_requested)
        logging.info("[RFID] Requested stop of the RFID read operation.")

        if wait_for_stop:
            while True:
                if self.get_run_state() == RfidRunState.stopped:
                    break
                tm.sleep(0.1)

    def time_delta_to_last_read(self):
        """
        Returns the time delta in seconds to the last RFID read operation.
        """
        with self.thread_lock:
            return tm.time() - self.timestamp

    def set_run_state(self, state: RfidRunState):
        """
        Sets the RFID run state to the specified state.
        """
        with self.thread_lock:
            if state == RfidRunState.stop_requested and self.rfid_run_state == RfidRunState.stopped:
                logging.warning("[RFID] RFID run state is already stopped. Ignoring stop request.")
            else:
                self.rfid_run_state = state

    def get_run_state(self):
        """
        Returns the current RFID run state.
        """
        with self.thread_lock:
            return self.rfid_run_state
        
    def get_tag(self):
        """
        Thread-safe method to read the current tag id with the according timestamp.

        Returns:
            tuple: A tuple containing: tag_id, timestamp
        """
        with self.thread_lock:
            return self.tag_id, self.timestamp
        
    def set_tag(self, tag_id, timestamp):
        """
        Thread-safe method to set the current tag id with the according timestamp.

        Args:
            tag_id (str): The tag id to set.
            timestamp (float): The timestamp to set.
        """
        with self.thread_lock:
            self.tag_id = tag_id
            self.timestamp = timestamp