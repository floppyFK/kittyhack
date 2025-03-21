from enum import Enum
import logging
import subprocess
import os
import re
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

def manage_and_switch_wlan(ssid, password="", priority=-1, update_password=False):
    """
    Add or update a WLAN connection using NetworkManager and set its priority.

    Parameters:
    - ssid: The SSID of the WLAN network.
    - password: The password for the WLAN network.
    - priority: The priority of the WLAN connection. Use -1 for highest priority (default), or specific value.
    - update_password: Boolean flag to indicate if the password should be updated for an existing connection.

    Returns:
    - True if the WLAN configuration was successful.
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
            ["/usr/bin/nmcli", "-t", "-f", "NAME", "connection", "show"],
            stdout=subprocess.PIPE, text=True
        )
        existing_connections = [conn for conn in result.stdout.splitlines() 
                              if conn not in ["lo", "Wired connection 1"]]
        

        if ssid in existing_connections:
            if update_password:
                # Update the password for the existing connection
                subprocess.run(
                    ["/usr/bin/nmcli", "connection", "modify", ssid, "wifi-sec.psk", password],
                    check=True,
                )
                logging.info(f"[SYSTEM] Updated password for WLAN {ssid}.")
            else:
                logging.info(f"[SYSTEM] Skipping password update for WLAN {ssid}.")
        else:
            subprocess.run(
                [
                    "/usr/bin/nmcli", "connection", "add", "type", "wifi", 
                    "ifname", "wlan0", "con-name", ssid, "ssid", ssid,
                    *wifi_sec_params, "802-11-wireless.hidden", "yes",
                    "connection.autoconnect", "yes",
                ],
                check=True,
            )
            logging.info(f"[SYSTEM] Added WLAN configuration for {ssid}.")

        # Set the priority for the connection
        subprocess.run(
            ["/usr/bin/nmcli", "connection", "modify", ssid, "connection.autoconnect-priority", str(priority)],
            check=True,
        )
        logging.info(f"[SYSTEM] Set priority {priority} for {ssid}.")

        # Restart NetworkManager to apply changes
        subprocess.run(["/usr/bin/systemctl", "restart", "NetworkManager"], check=True)
        logging.info(f"[SYSTEM] Restarted NetworkManager to apply changes.")
        # Wait for the network to be up before returning
        for x in range(20):
            result = subprocess.run(
                ["/usr/bin/nmcli", "-t", "-f", "NETWORKING"],
                stdout=subprocess.PIPE,
                text=True,
                check=True
            )
            if "wlan0: connected to" in result.stdout:
                logging.info(f"[SYSTEM] Network is up and connected.")
                return True
            tm.sleep(2)
        logging.error(f"[SYSTEM] Network did not come up in time.")
        return False

    except subprocess.CalledProcessError as e:
        logging.info(f"[SYSTEM] Error managing WLAN: {e}")
        return False
    
def switch_wlan_connection(ssid: str):
    """
    Switch to an already configured WLAN network.

    Parameters:
    - ssid: The SSID of the WLAN network to connect to.

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
        # Wait for the network to be up before returning
        for x in range(20):
            result = subprocess.run(
                ["/usr/bin/nmcli", "-t", "-f", "NETWORKING"],
                stdout=subprocess.PIPE,
                text=True,
                check=True
            )
            if "wlan0: connected to" in result.stdout:
                logging.info(f"[SYSTEM] Network is up and connected.")
                return True
            tm.sleep(2)
        logging.error(f"[SYSTEM] Network did not come up in time.")
        return False
    except subprocess.CalledProcessError as e:
        logging.error(f"[SYSTEM] Error switching to WLAN network {ssid}: {e.stderr}")
        return False
    
def delete_wlan_connection(ssid):
    """
    Delete a WLAN network configuration.

    Parameters:
    - ssid: The SSID of the WLAN network to delete.

    Returns:
    - True if the deletion was successful
    - False if there was an error
    """
    try:
        subprocess.run(
            ["/usr/bin/nmcli", "connection", "delete", ssid],
            check=True,
            capture_output=True,
            text=True
        )
        logging.info(f"[SYSTEM] Successfully deleted WLAN network configuration: {ssid}")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"[SYSTEM] Error deleting WLAN network configuration {ssid}: {e.stderr}")
        return False
    
def scan_wlan_networks():
    # Added channel to the scan results
    """
    Scans for available WLAN networks using the `nmcli` command-line tool.

    Returns:
        list: A list of dictionaries, each containing information about a WLAN network.
              Each dictionary has the following keys:
              - "ssid" (str): The SSID of the WLAN network (or BSSID if SSID is not available).
              - "signal" (int): The signal strength of the WLAN network.
              - "security" (str): The security type of the WLAN network.
              - "bars" (int): The number of bars indicating signal strength (0-4).
              - "channel" (int): The channel number of the WLAN network.

    Raises:
        subprocess.CalledProcessError: If the `nmcli` command fails to execute.

    Logs:
        An error message if there is an issue scanning WLAN networks.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,BARS,CHAN,BSSID", "device", "wifi", "list"],
            stdout=subprocess.PIPE,
            text=True,
            check=True
        )
        networks = {}
        for line in result.stdout.split('\n'):
            if not line:
                continue
                
            # Use regex to split on unescaped colons
            parts = re.split(r'(?<!\\):', line, maxsplit=5)
            if len(parts) != 6:
                continue
                
            # Unescape any escaped characters
            parts = [p.replace('\\:', ':').replace('\\\\', '\\') for p in parts]
            ssid, signal, security, bars, channel, bssid = parts
            if not ssid or ssid == "":
                ssid = bssid.replace("\\", "")
            bar_count = len([c for c in bars if c not in ('_', ' ')])
            if ssid not in networks:
                networks[ssid] = {
                    "ssid": ssid,
                    "signal": int(signal),
                    "security": security,
                    "bars": bar_count,
                    "channel": str(channel)
                }
            else:
                networks[ssid]["signal"] = max(networks[ssid]["signal"], int(signal))
                networks[ssid]["bars"] = max(networks[ssid]["bars"], bar_count)
                if str(channel) not in networks[ssid]["channel"].split(','):
                    networks[ssid]["channel"] = f"{networks[ssid]['channel']}, {channel}"
        networks = list(networks.values())
        return networks
    except Exception as e:
        logging.error(f"[SYSTEM] Error scanning WLAN networks: {e}")
        return []
    
def get_wlan_connections():
    """
    Get a list of configured WLAN networks.

    Returns:
    - A list of dictionaries, each containing the SSID, connection status, and priority of a WLAN network.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/nmcli", "-t", "-f", "NAME,DEVICE,AUTOCONNECT-PRIORITY,STATE", "connection", "show"],
            stdout=subprocess.PIPE,
            text=True,
            check=True
        )
        connections = []
        for line in result.stdout.split('\n'):
            if line:
                # Use regex to split on unescaped colons
                parts = re.split(r'(?<!\\):', line, maxsplit=3)
                if len(parts) != 4:
                    continue
                
                # Unescape any escaped characters
                parts = [p.replace('\\:', ':').replace('\\\\', '\\') for p in parts]
                ssid, device, priority, state = parts
                # Skip loopback and wired connections
                if ssid not in ["lo", "Wired connection 1"]:
                    connections.append({
                        "ssid": ssid,
                        "connected": state == "activated",
                        "priority": int(priority)
                    })
        return connections
    except subprocess.CalledProcessError as e:
        logging.error(f"[SYSTEM] Error getting WLAN connections: {e.stderr}")
        return []


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