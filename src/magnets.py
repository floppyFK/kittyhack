import logging
from src.system import Gpio

# GPIO pin numbers and directions
MAG_LOCK_TO_OUTSIDE_NUM = 524
MAG_LOCK_TO_OUTSIDE_DIR = "out"
MAG_LOCK_TO_INSIDE_NUM = 525
MAG_LOCK_TO_INSIDE_DIR = "out"

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
                self.lock_inside()
                self.lock_outside()
            except Exception as e:
                logging.error(f"[MAGNETS] Error initializing Magnets: {e}")
            else:
                logging.info("[MAGNETS] Magnets initialized and released.")

    def unlock_inside(self):
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

    def lock_inside(self):
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

    def unlock_outside(self):
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

    def lock_outside(self):
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