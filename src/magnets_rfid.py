import logging
import random
import select
import re
from enum import Enum
from src.system import Gpio, I2C
from src.helper import sigterm_monitor
import threading
import string
import time as tm
from queue import Queue

# MAGNETS: GPIO pin numbers and directions
MAG_LOCK_TO_OUTSIDE_NUM = 524
MAG_LOCK_TO_OUTSIDE_DIR = "out"
MAG_LOCK_TO_INSIDE_NUM = 525
MAG_LOCK_TO_INSIDE_DIR = "out"

# RFID: GPIO pin numbers and directions
RFID_FIELD_NUM = 529
RFID_FIELD_DIR = "out"
RFID_POWER_NUM = 515
RFID_POWER_DIR = "out"

I2CPORT=0
PE_ADDR=0x20
PE_DIRREG=0x03
PE_OUTREG=0x01

RFID_READ_PATH = "/dev/serial0"

# Safety delay for the magnet command queue
# WARNING: TOO LOW VALUES MAY DAMAGE THE HARDWARE!
MAG_RFID_CMD_DELAY = 1.0

# Create a Gpio instance
gpio = Gpio()

class HardwareCommandQueue:
    """
    Shared command queue for hardware operations requiring safety delays.
    Used by both Magnets and RFID classes to ensure proper timing between operations.
    """
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(HardwareCommandQueue, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self.command_queue = Queue()
        self.queue_lock = threading.Lock()
        self.last_command_time = tm.time() - MAG_RFID_CMD_DELAY  # Allow immediate first command
        self.control_thread = None
        self._initialized = True
        
    def start_command_processor(self):
        """
        Initializes and starts the command processing thread.
        """
        if self.control_thread is None or not self.control_thread.is_alive():
            self.control_thread = threading.Thread(target=self._process_commands)
            self.control_thread.daemon = True
            self.control_thread.start()
            logging.info("[HW_QUEUE] Started shared command queue processor thread.")
    
    def _process_commands(self):
        # Register task in the sigterm_monitor object
        sigterm_monitor.register_task()

        while not sigterm_monitor.stop_now:
            if not self.command_queue.empty():
                current_time = tm.time()
                if current_time - self.last_command_time < MAG_RFID_CMD_DELAY:
                    delay = MAG_RFID_CMD_DELAY - (current_time - self.last_command_time)
                    logging.info(f"[HW_QUEUE] Waiting {delay:.1f} seconds before processing next command.")
                    tm.sleep(delay)

                command = self.command_queue.get()
                cmd_type, func, args = command
                try:
                    func(*args)
                    logging.info(f"[HW_QUEUE] Executed {cmd_type} command.")
                except Exception as e:
                    logging.error(f"[HW_QUEUE] Error executing {cmd_type} command: {e}")
                self.last_command_time = tm.time()
            tm.sleep(0.05)

        # When stop_now is detected, wait briefly for final commands to be enqueued
        logging.info("[HW_QUEUE] Shutdown detected. Waiting 3 seconds for final commands to be enqueued...")
        tm.sleep(3)
        
        # Process all remaining commands in the queue
        if not self.command_queue.empty():
            cmd_count = self.command_queue.qsize()
            logging.info(f"[HW_QUEUE] Processing {cmd_count} remaining commands before shutdown...")
            
            while not self.command_queue.empty():
                current_time = tm.time()
                if current_time - self.last_command_time < MAG_RFID_CMD_DELAY:
                    delay = MAG_RFID_CMD_DELAY - (current_time - self.last_command_time)
                    logging.info(f"[HW_QUEUE] Waiting {delay:.1f} seconds before processing shutdown command.")
                    tm.sleep(delay)
                    
                command = self.command_queue.get()
                cmd_type, func, args = command
                try:
                    func(*args)
                    logging.info(f"[HW_QUEUE] Executed {cmd_type} shutdown command.")
                except Exception as e:
                    logging.error(f"[HW_QUEUE] Error executing {cmd_type} shutdown command: {e}")
                self.last_command_time = tm.time()

        logging.info("[HW_QUEUE] Stopped command queue thread.")
        sigterm_monitor.signal_task_done()
    
    def queue_command(self, cmd_type, func, *args):
        """
        Adds a command to the command queue.
        
        Args:
            cmd_type (str): The type of command ('magnet' or 'rfid')
            func (callable): The function to execute
            args: Arguments to pass to the function
        """
        if self.queue_lock.acquire(timeout=3):
            try:
                self.command_queue.put((cmd_type, func, args))
                logging.info(f"[HW_QUEUE] {cmd_type.upper()} command added to queue.")
            finally:
                self.queue_lock.release()
        else:
            logging.error(f"[HW_QUEUE] Failed to acquire lock for adding {cmd_type} command to queue")
    
    def is_queue_empty(self):
        """
        Checks if the command queue is empty.
        
        Returns:
            bool: True if the queue is empty, False otherwise.
        """
        return self.command_queue.empty()
    
    def empty_queue(self):
        """
        Empties the command queue.
        """
        with self.queue_lock:
            while not self.command_queue.empty():
                self.command_queue.get()
            logging.info("[HW_QUEUE] Command queue emptied.")

class MagnetController:
    def __init__(self):
        self._magnet_state_outside = False  # False = locked, True = unlocked
        self._magnet_state_inside = False  # False = locked, True = unlocked

    @property
    def magnet_state_outside(self):
        return self._magnet_state_outside

    @magnet_state_outside.setter
    def magnet_state_outside(self, state):
        self._magnet_state_outside = state

    @property
    def magnet_state_inside(self):
        return self._magnet_state_inside

    @magnet_state_inside.setter
    def magnet_state_inside(self, state):
        self._magnet_state_inside = state

class Magnets:
    instance = None
    
    def __init__(self, simulate_kittyflap=False):
        self.magnet_controller = MagnetController()
        self.simulate_kittyflap = simulate_kittyflap
        # Use the shared command queue
        self.command_queue = HardwareCommandQueue()

    def init(self):
        Magnets.instance = self

        if self.simulate_kittyflap:
            logging.info("[MAGNETS] Simulation mode enabled. Magnets would be now initialized.")
        else:
            try:
                # Configure GPIO pins for magnets
                gpio.configure(MAG_LOCK_TO_OUTSIDE_NUM, MAG_LOCK_TO_OUTSIDE_DIR)
                gpio.configure(MAG_LOCK_TO_INSIDE_NUM, MAG_LOCK_TO_INSIDE_DIR)

                # Start the command processor
                self.command_queue.start_command_processor()

                # Ensure both magnets are powered off
                self.queue_command("lock_inside")
                self.queue_command("lock_outside")
            except Exception as e:
                logging.error(f"[MAGNETS] Error initializing Magnets: {e}")
            else:
                logging.info("[MAGNETS] Magnets initialized and released.")

    def _unlock_inside(self):
        """
        Unlocks the magnet lock to the inside direction.
        """
        self.magnet_controller.magnet_state_inside = True

        if self.simulate_kittyflap:
            logging.info("[MAGNETS] Simulation: Inside direction would be now unlocked.")
            return

        try:
            gpio.set(MAG_LOCK_TO_INSIDE_NUM, 1)
        except Exception as e:
            logging.error(f"[MAGNETS] Error unlocking inside direction: {e}")
        else:
            logging.info("[MAGNETS] Inside direction is now unlocked.")

    def _lock_inside(self):
        """
        Locks the magnet lock to the inside direction.
        """
        self.magnet_controller.magnet_state_inside = False

        if self.simulate_kittyflap:
            logging.info("[MAGNETS] Simulation: Inside direction would be now locked.")
            return

        try:
            gpio.set(MAG_LOCK_TO_INSIDE_NUM, 0)
        except Exception as e:
            logging.error(f"[MAGNETS] Error locking inside direction: {e}")
        else:
            logging.info("[MAGNETS] Inside direction is now locked.")

    def _unlock_outside(self):
        """
        Unlocks the magnet lock to the outside direction.
        """
        self.magnet_controller.magnet_state_outside = True

        if self.simulate_kittyflap:
            logging.info("[MAGNETS] Simulation: Outside direction would be now unlocked.")
            return

        try:
            gpio.set(MAG_LOCK_TO_OUTSIDE_NUM, 1)
        except Exception as e:
            logging.error(f"[MAGNETS] Error unlocking outside direction: {e}")
        else:
            logging.info("[MAGNETS] Outside direction is now unlocked.")

    def _lock_outside(self):
        """
        Locks the magnet lock to the outside direction.
        """
        self.magnet_controller.magnet_state_outside = False

        if self.simulate_kittyflap:
            logging.info("[MAGNETS] Simulation: Outside direction would be now locked.")
            return

        try:
            gpio.set(MAG_LOCK_TO_OUTSIDE_NUM, 0)
        except Exception as e:
            logging.error(f"[MAGNETS] Error locking outside direction: {e}")
        else:
            logging.info("[MAGNETS] Outside direction is now locked.")

    def get_outside_state(self) -> bool:
        """
        Returns the current state of the magnet lock to the outside direction.

        Returns:
            bool: True if the outside direction is unlocked, False if the outside direction is locked.
        """
        return self.magnet_controller.magnet_state_outside

    def get_inside_state(self) -> bool:
        """
        Returns the current state of the magnet lock to the inside direction.

        Returns:
            bool: True if the inside direction is unlocked, False if the inside direction is locked.
        """
        return self.magnet_controller.magnet_state_inside
    
    def start_magnet_control(self):
        """
        Initializes and starts the magnet control thread.
        """
        # Start the shared command processor
        self.command_queue.start_command_processor()

    def queue_command(self, command):
        """
        Adds a command to the command queue.

        Args:
            command (str): The command to be added to the queue:
                - "unlock_inside": Unlocks the magnet lock to the inside direction.
                - "lock_inside": Locks the magnet lock to the inside direction.
                - "unlock_outside": Unlocks the magnet lock to the outside direction.
                - "lock_outside": Locks the magnet lock to the outside direction.
        """
        func_map = {
            "unlock_inside": self._unlock_inside,
            "lock_inside": self._lock_inside,
            "unlock_outside": self._unlock_outside,
            "lock_outside": self._lock_outside
        }
        
        if command in func_map:
            self.command_queue.queue_command("magnet", func_map[command])
            logging.info(f"[MAGNETS] Command '{command}' added to shared queue.")
        else:
            logging.error(f"[MAGNETS] Unknown command: {command}")

    def check_queued(self, command):
        """
        Checks whether a command is already in the command queue.

        Args:
            command (str): The command to be checked:
                - "unlock_inside": Unlocks the magnet lock to the inside direction.
                - "lock_inside": Locks the magnet lock to the inside direction.
                - "unlock_outside": Unlocks the magnet lock to the outside direction.
                - "lock_outside": Locks the magnet lock to the outside direction.

        Returns:
            bool: True if the command is already in the queue, False otherwise.
        """
        func_map = {
            "unlock_inside": self._unlock_inside,
            "lock_inside": self._lock_inside,
            "unlock_outside": self._unlock_outside,
            "lock_outside": self._lock_outside
        }
        
        if command not in func_map:
            return False
        
        # Get the corresponding function for the command
        target_func = func_map[command]
        
        # Check the queue items
        with self.command_queue.queue_lock:
            queue_items = list(self.command_queue.command_queue.queue)
            for item in queue_items:
                # Each item is (cmd_type, func, args)
                if item[1] == target_func:
                    return True
        
        return False

    def empty_queue(self, shutdown=False):
        """
        Checks for remaining commands in the command queue and empties it to return magnets to idle state.
        """
        try:
            # Empty the shared queue
            self.command_queue.empty_queue()
            
            # Get the current time to calculate delays
            current_time = tm.time()
            
            # Check if we need to respect a delay from the last hardware command
            if current_time - self.command_queue.last_command_time < MAG_RFID_CMD_DELAY:
                delay = MAG_RFID_CMD_DELAY - (current_time - self.command_queue.last_command_time)
                logging.info(f"[MAGNETS] Waiting {delay:.1f} seconds before locking directions after emptying queue.")
                tm.sleep(delay)
                    
            if self.get_outside_state():
                if shutdown:
                    logging.info("[MAGNETS] Shutdown detected! Locking outside direction.")
                else:
                    logging.info("[MAGNETS] Emptying queue! Locking outside direction.")
                # Directly call _lock_outside without queuing to ensure immediate action during shutdown
                self._lock_outside()
                
                # Update the last command time to track this hardware operation
                self.command_queue.last_command_time = tm.time()
                
                # Ensure delay between locking outside and inside if both are needed
                if self.get_inside_state():
                    tm.sleep(MAG_RFID_CMD_DELAY)
                
            if self.get_inside_state():
                if shutdown:
                    logging.info("[MAGNETS] Shutdown detected! Locking inside direction.")
                else:
                    logging.info("[MAGNETS] Emptying queue! Locking inside direction.")
                # Directly call _lock_inside without queuing to ensure immediate action during shutdown
                self._lock_inside()
                
                # Update the last command time for this hardware operation
                self.command_queue.last_command_time = tm.time()
        except Exception as e:
            logging.error(f"[MAGNETS] Error emptying queue: {e}")

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
        self.field_state = False
        self.thread_lock = threading.Lock()
        # Use the shared command queue
        self.command_queue = HardwareCommandQueue()
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

                # Start the command processor if not already started
                self.command_queue.start_command_processor()

                tm.sleep(0.25)

                # PCA6408AHKX setup
                i2c = I2C()
                i2c.enable_gate(self)

                # Ensure RFID is powered off to avoid unnecessary interference
                self.set_power(False)
                self.set_field(False)
                tm.sleep(1.0)
                self.set_power(True)

            except Exception as e:
                logging.error(f"[RFID] Error initializing RFID: {e}")
            else:
                logging.info("[RFID] RFID initialized.")

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

        # FIXME - Test: Do not enable power at all (Issue #134)
        if state:
            logging.info(f"[RFID] RFID power enabling is disabled in this build. Ignoring request.")
            return
        
        # Use the shared command queue for setting the RFID power
        self.command_queue.queue_command("rfid", self._set_power_hardware, state)
        logging.info(f"[RFID] RFID power {'enable' if state else 'disable'} command queued.")

    def _set_power_hardware(self, state: bool):
        """
        Hardware implementation of setting the RFID power state.
        This method is called by the command queue processor.
        
        Args:
            state (bool): Desired state of the RFID power. True to enable, False to disable.
        """
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
            self.field_state = state
            return

        # FIXME - Test: Do not enable field at all (Issue #134)
        if state:
            logging.info(f"[RFID] RFID field enabling is disabled in this build. Ignoring request.")
            return
        
        # Use the shared command queue for setting the RFID field
        self.command_queue.queue_command("rfid", self._set_field_hardware, state)
        # Update the field state immediately for status queries
        self.field_state = state
        logging.info(f"[RFID] RFID field {'enable' if state else 'disable'} command queued.")

    def _set_field_hardware(self, state: bool):
        """
        Hardware implementation of setting the RFID field state.
        This method is called by the command queue processor.
        
        Args:
            state (bool): Desired state of the RFID field. True to enable, False to disable.
        """
        try:
            gpio.set(RFID_FIELD_NUM, 1 if state else 0)
        except Exception as e:
            logging.error(f"[RFID] Error setting RFID field: {e}")
        else:
            logging.info(f"[RFID] RFID field {'enabled' if state else 'disabled'}.")

    def get_field(self):
        """
        Returns the current state of the RFID field.
        """
        return self.field_state
    
    def remove_non_printable_chars(self, line):
        # Remove all non-printable characters
        return ''.join(filter(lambda x: x in string.printable, line))

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
            self.set_power(True)
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
                line = None
                duplicate_count = 0
                with open(RFID_READ_PATH, "rb") as f:
                    cycle = 0
                    while (read_cycles == 0 or cycle < read_cycles) and (self.get_run_state() != RfidRunState.stop_requested):
                        #logging.debug(f"[RFID] Cycle {cycle+1}/{read_cycles if read_cycles != 0 else '∞'} | RFID run state: {self.get_run_state()}")
                        ready, __, __ = select.select([f], [], [], 1.0)
                        if ready:
                            line = f.readline().decode('utf-8', errors='ignore').strip()
                            line = self.remove_non_printable_chars(line)
                            # Skip empty lines and initialization messages from the RFID reader
                            match = re.search(r'([0-9A-Fa-f]{16})', line)
                            if not match:
                                logging.info(f"[RFID] Skipping line: '{line}' - No valid 16-char hex substring found")
                                continue

                            # We found a valid 16-char hex substring
                            tag_id = match.group(1)
                            timestamp = tm.time()
                            last_tag, last_tm = self.get_tag()
                            self.set_tag(tag_id, timestamp)
                            
                            if tag_id == last_tag:
                                logging.debug(f"[RFID] Skipping duplicate tag: '{tag_id}'")
                                duplicate_count += 1
                                continue
                            if duplicate_count > 0:
                                logging.info(f"[RFID] Skipped {duplicate_count} previous duplicate tags of '{last_tag}'")
                                duplicate_count = 0
                            logging.info(f"[RFID] Tag ID: '{tag_id}' (raw Tag ID: '{line}') detected at {timestamp} (read cycle {cycle+1}/{read_cycles if read_cycles != 0 else '∞'})")

                        #tm.sleep(0.1)
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
        Returns the time delta in seconds to the last successful RFID read operation.
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