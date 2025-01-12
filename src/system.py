from enum import Enum
import logging
import subprocess
import os
import time as tm

GPIO_BASE_PATH = "/sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/"

def systemctl(mode: str, service: str, simulate_operations=False):
    """
    Start, stop or restart a service using systemctl.

    Parameters:
    - mode: start, stop, restart
    - service: The name of the service, e.g. 'kwork'

    Returns:
    - True, if the action succeeded
    - False in case of an exception
    """
    # stop kwork process
    if simulate_operations == True:
        logging.info(f"kittyhack is in development mode. Skip 'systemctl {mode} {service}'.")
    else:
        try:
            result = subprocess.run(
                ["/usr/bin/systemctl", mode, service],
                check=True,
                text=True,
                capture_output=True
            )
            logging.info(f"service {service} {mode}: {result.stdout}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to {mode} {service}: {e.stderr}")
            return False
    
    return True

def systemcmd(command: list[str], simulate_operations=False):
    """
    Run any command on the system shell.

    Parameters:
    - commands: list of (tokenized) commands

    Returns:
    - True, if the action succeeded
    - False in case of an exception
    """
    
    cString = ' '.join(command)
    # run command
    if simulate_operations == True:
        logging.info(f"kittyhack is in development mode. Skip 'systemcmd {cString}'.")
    else:
        try:
            result = subprocess.run(
                command,
                check=True,
                text=True,
                capture_output=True
            )
            logging.info(f"systemcmd '{cString}': {result.stdout}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to run command '{cString}': {e.stderr}")
            return False
    
    return True

class Gpio:
    BASE_PATH = "/sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/"

    def __init__(self, base_path=BASE_PATH):
        self.base_path = base_path

    def configure(self, gpio_number, gpio_direction="out"):
        """
        Configures a GPIO pin with the specified parameters.

        Args:
            gpio_number (int): The GPIO number to configure.
            gpio_direction (str): Direction of the GPIO ('in' or 'out'). Defaults to 'out'.

        Returns:
            bool: True if the configuration is successful, False otherwise.
        """
        try:
            # Export GPIO
            with open("/sys/class/gpio/export", "w") as export_file:
                export_file.write(str(gpio_number))
                tm.sleep(0.1) # Wait for the GPIO to be exported
        except IOError:
            # Ignore errors if the GPIO is already exported
            pass

        # Configure GPIO direction
        direction_path = os.path.join(self.base_path, f"gpio{gpio_number}", "direction")
        try:
            with open(direction_path, "w") as direction_file:
                direction_file.write(gpio_direction)

        except IOError as e:
            logging.error(f"Error setting direction for GPIO{gpio_number}: {e}")
            return False

        # Set default value if direction is 'out'
        if gpio_direction == "out":
            value_path = os.path.join(self.base_path, f"gpio{gpio_number}", "value")
            try:
                with open(value_path, "w") as value_file:
                    value_file.write("0")
            except IOError as e:
                logging.error(f"Error setting default value for GPIO{gpio_number}: {e}")
                return False

        logging.info(f"GPIO{gpio_number} configured successfully as {gpio_direction}")
        return True

    def set(self, gpio_number, value):
        """
        Sets the value of a GPIO pin.

        Args:
            gpio_number (int): The GPIO number to set.
            value (int): The value to set (0 or 1).

        Returns:
            bool: True if the operation is successful, False otherwise.
        """
        value_path = os.path.join(self.base_path, f"gpio{gpio_number}", "value")
        try:
            with open(value_path, "w") as value_file:
                value_file.write(str(value))
        except IOError as e:
            logging.error(f"Error setting value for GPIO{gpio_number}: {e}")
            return False

        logging.debug(f"GPIO{gpio_number} set to {value}")
        return True

    def get(self, gpio_number):
        """
        Gets the value of a GPIO pin.

        Args:
            gpio_number (int): The GPIO number to read.

        Returns:
            int: The value of the GPIO pin (0 or 1), or None on error.
        """
        value_path = os.path.join(self.base_path, f"gpio{gpio_number}", "value")
        try:
            with open(value_path, "r") as value_file:
                value = int(value_file.read().strip())
                return value
        except IOError as e:
            logging.error(f"Error reading value for GPIO{gpio_number}: {e}")
            return None