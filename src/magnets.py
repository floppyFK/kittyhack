import logging
from src.system import Gpio
from src.helper import sigterm_monitor
import threading
import time as tm
from queue import Queue

# GPIO pin numbers and directions
MAG_LOCK_TO_OUTSIDE_NUM = 524
MAG_LOCK_TO_OUTSIDE_DIR = "out"
MAG_LOCK_TO_INSIDE_NUM = 525
MAG_LOCK_TO_INSIDE_DIR = "out"

# Safety delay for the magnet command queue
# WARNING: TOO LOW VALUES MAY DAMAGE THE HARDWARE!
MAGNET_COMMAND_DELAY = 1.0

# Create a Gpio instance
gpio = Gpio()

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
    def __init__(self, simulate_kittyflap=False):
        self.magnet_controller = MagnetController()
        self.simulate_kittyflap = simulate_kittyflap

    def init(self):
        if self.simulate_kittyflap:
            logging.info("[MAGNETS] Simulation mode enabled. Magnets would be now initialized.")
        else:
            try:
                # Configure GPIO pins for magnets
                gpio.configure(MAG_LOCK_TO_OUTSIDE_NUM, MAG_LOCK_TO_OUTSIDE_DIR)
                gpio.configure(MAG_LOCK_TO_INSIDE_NUM, MAG_LOCK_TO_INSIDE_DIR)

                # Ensure both magnets are powered off
                self._lock_inside()
                tm.sleep(MAGNET_COMMAND_DELAY)
                self._lock_outside()
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
        self.command_queue = Queue()
        self.control_thread = threading.Thread(target=self._process_commands)
        self.control_thread.daemon = True
        self.control_thread.start()

    def _process_commands(self):
        last_command_time = tm.time() - MAGNET_COMMAND_DELAY  # Initialize to allow immediate first command

        # Register task in the sigterm_monitor object
        sigterm_monitor.register_task()

        while not sigterm_monitor.stop_now:
            if not self.command_queue.empty():
                current_time = tm.time()
                if current_time - last_command_time < MAGNET_COMMAND_DELAY:
                    magnet_delay = MAGNET_COMMAND_DELAY - (current_time - last_command_time)
                    logging.info(f"[MAGNETS] Waiting {magnet_delay:.1f} seconds before processing next command.")
                    tm.sleep(magnet_delay)

                command = self.command_queue.get()
                if command == "unlock_inside":
                    self._unlock_inside()
                elif command == "lock_inside":
                    self._lock_inside()
                elif command == "unlock_outside":
                    self._unlock_outside()
                elif command == "lock_outside":
                    self._lock_outside()
                last_command_time = tm.time()
            tm.sleep(0.05)

        # Lock the outside and inside direction if the thread is stopped
        if self.get_outside_state() == True:
            magnet_delay = MAGNET_COMMAND_DELAY - (tm.time() - last_command_time)
            if magnet_delay > 0:
                logging.info(f"[MAGNETS] Shutdown detected! Waiting {magnet_delay:.1f} seconds before finally locking outside direction.")
                tm.sleep(magnet_delay)
            self._lock_outside()
            last_command_time = tm.time()
        if self.get_inside_state() == True:
            magnet_delay = MAGNET_COMMAND_DELAY - (tm.time() - last_command_time)
            if magnet_delay > 0:
                logging.info(f"[MAGNETS] Shutdown detected! Waiting {magnet_delay:.1f} seconds before finally locking inside direction.")
                tm.sleep(magnet_delay)
            self._lock_inside()

        logging.info("[MAGNETS] Stopped magnet command queue thread.")
        sigterm_monitor.signal_task_done()

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
        self.command_queue.put(command)
        logging.info(f"[MAGNETS] Command '{command}' added to queue.")

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
        return command in list(self.command_queue.queue)

