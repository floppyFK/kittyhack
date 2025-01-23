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
    - mode: start, stop, restart, disable, enable, mask
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

def is_service_running(service: str, simulate_operations=False):
    """
    Check if a service is running.

    Parameters:
    - service: The name of the service, e.g. 'kwork'

    Returns:
    - True, if the service is running
    - False, if the service is not running
    """
    if simulate_operations == True:
        logging.info(f"kittyhack is in development mode. Skip 'is_service_running {service}'.")
        return True
    
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", service],
            check=True,
            text=True,
            capture_output=True
        )
        logging.info(f"service {service} is active. {result.stdout}")
        return True
    except subprocess.CalledProcessError as e:
        logging.info(f"service {service} is not active. {e.stderr}")
        return False
    
def is_service_masked(service: str, simulate_operations=False):
    """
    Check if a service is masked.

    Parameters:
    - service: The name of the service, e.g. 'kwork'

    Returns:
    - True, if the service is masked
    - False, if the service is not masked
    """
    if simulate_operations:
        logging.info(f"kittyhack is in development mode. Skip 'is_service_running {service}'.")
        return True

    try:
        # Run the `systemctl is-enabled` command
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-enabled", service],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False
        )
        # Check the output for "masked"
        if result.returncode != 0 or "masked" in result.stdout:
            return True
        return False
    except Exception as e:
        logging.error(f"Failed to check if service {service} is masked: {e}")
        return False

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

def manage_and_switch_wifi(ssid, password="", priority=-1):
    """
    Add or update a Wi-Fi connection using NetworkManager and set its priority.

    Parameters:
    - ssid: The SSID of the Wi-Fi network.
    - password: The password for the Wi-Fi network.
    - priority: The priority of the Wi-Fi connection. Use -1 for highest priority (default), or specific value.

    Returns:
    - True if the Wi-Fi configuration was successful.
    - False if there was an error.
    """
    if priority == -1:
        try:
            result = subprocess.run(
                ["/usr/bin/nmcli", "-g", "AUTOCONNECT-PRIORITY", "connection", "show"],
                stdout=subprocess.PIPE,
                text=True,
                check=True
            )
            existing_priorities = [int(p) for p in result.stdout.split('\n') if p.strip()]
            priority = max(existing_priorities, default=0) + 1
        except subprocess.CalledProcessError:
            priority = 999

    if password == "":
        wifi_sec_params = []
    else:
        wifi_sec_params = ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]

    try:
        # Check if the connection already exists
        result = subprocess.run(
            ["/usr/bin/nmcli", "-t", "-f", "NAME", "connection", "show"], stdout=subprocess.PIPE, text=True
        )
        if ssid in result.stdout:
            # Update the password for the existing connection
            subprocess.run(
                ["/usr/bin/nmcli", "connection", "modify", ssid, "wifi-sec.psk", password],
                check=True,
            )
            subprocess.run(
                [
                    "/usr/bin/nmcli", "connection", "add", "type", "wifi", 
                    "con-name", ssid, "ifname", "*", "ssid", ssid,
                    *wifi_sec_params, "--hidden", "yes",
                    "connection.autoconnect", "yes",
                ],
                check=True,
            )
            logging.info(f"[SYSTEM] Added Wi-Fi configuration for {ssid}.")

        # Set the priority for the connection
        subprocess.run(
            ["/usr/bin/nmcli", "connection", "modify", ssid, "connection.autoconnect-priority", str(priority)],
            check=True,
        )
        logging.info(f"[SYSTEM] Set priority {priority} for {ssid}.")

        # Restart NetworkManager to apply changes (optional)
        subprocess.run(["/usr/bin/systemctl", "restart", "NetworkManager"], check=True)
        logging.info(f"[SYSTEM] Restarted NetworkManager to apply changes.")

    except subprocess.CalledProcessError as e:
        logging.info(f"[SYSTEM] Error managing Wi-Fi: {e}")
        return False
    
def switch_wifi_connection(ssid):
    """
    Switch to an already configured Wi-Fi network.

    Parameters:
    - ssid: The SSID of the Wi-Fi network to connect to.

    Returns:
    - True if the switch was successful
    - False if there was an error
    """
    try:
        subprocess.run(
            ["/usr/bin/nmcli", "connection", "up", ssid],
            check=True,
            capture_output=True,
            text=True
        )
        logging.info(f"[SYSTEM] Successfully switched to Wi-Fi network: {ssid}")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"[SYSTEM] Error switching to Wi-Fi network {ssid}: {e.stderr}")
        return False


class I2C:
    # Fixed constants
    I2C_PORT = "0"
    PE_ADDR = "0x20"
    PE_DIRREG = "0x03"
    PE_OUTREG = "0x01"

    def __init__(self, i2c_port=I2C_PORT, pe_addr=PE_ADDR, pe_dirreg=PE_DIRREG, pe_outreg=PE_OUTREG):
        self.I2C_PORT = i2c_port
        self.PE_ADDR = pe_addr
        self.PE_DIRREG = pe_dirreg
        self.PE_OUTREG = pe_outreg

    def enable_gate(self, simulate_operations=False):
        """
        Enable the gate logic by configuring the PCA6408AHKX.
        Sets the direction and output registers to open the logic gate.
        """
        commands = [
            ["/usr/sbin/i2cset", "-y", self.I2C_PORT, self.PE_ADDR, self.PE_DIRREG, "0x00"],
            ["/usr/sbin/i2cset", "-y", self.I2C_PORT, self.PE_ADDR, self.PE_OUTREG, "0x00"]
        ]
        for command in commands:
            if not systemcmd(command, simulate_operations=simulate_operations):
                logging.error("[I2C] Failed to enable gate")
                return
        logging.info("[I2C] Gate enabled: logic gate to periphery is open")

    def disable_gate(self, simulate_operations=False):
        """
        Disable the gate logic by configuring the PCA6408AHKX.
        Sets the direction and output registers to close the logic gate.
        """
        commands = [
            ["/usr/sbin/i2cset", "-y", self.I2C_PORT, self.PE_ADDR, self.PE_DIRREG, "0x00"],
            ["/usr/sbin/i2cset", "-y", self.I2C_PORT, self.PE_ADDR, self.PE_OUTREG, "0x01"]
        ]
        for command in commands:
            if not systemcmd(command, simulate_operations=simulate_operations):
                logging.error("[I2C] Failed to disable gate")
                return
        logging.info("[I2C] Gate disabled: logic gate to periphery is closed")

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