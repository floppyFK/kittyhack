from enum import Enum
import logging
import subprocess
import os
import re
import sys
import requests
import time as tm

from src.baseconfig import CONFIG, set_language

GPIO_BASE_PATH = "/sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/"

LABELSTUDIO_PATH = "/root/labelstudio/"
LABELSTUDIO_VENV = "venv/"

_ = set_language(CONFIG['LANGUAGE'])

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

def run_with_progress(command, progress_callback, step, message, detail):
    """
    Run a command and stream its stdout to the progress_callback.
    """
    logging.info(f"Running command: {' '.join(command)}")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    output_lines = []
    # Limit the length of callback messages to avoid flooding the UI
    max_detail_length = 120
    for line in process.stdout:
        line_stripped = line.strip()
        output_lines.append(line)
        logging.info(f"[SYSTEM] {line_stripped}")
        if progress_callback:
            # Send the latest line as detail, truncated if necessary
            if len(line_stripped) > max_detail_length:
                truncated_detail = line_stripped[:max_detail_length] + "..."
                progress_callback(step, message, truncated_detail)
            else:
                progress_callback(step, message, line_stripped)
    process.wait()
    if process.returncode != 0:
        logging.error(f"Command failed with return code {process.returncode}")
    return process.returncode == 0, ''.join(output_lines)

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
    
def get_labelstudio_installed_version():
    """
    Get the installed version of Label Studio.
    
    Returns:
        str | None: The installed version of Label Studio, or None if not found.
    """
    try:
        venv_python = os.path.join(LABELSTUDIO_PATH, LABELSTUDIO_VENV, "bin", "python")
        
        # Check if Label Studio is installed
        if not os.path.exists(LABELSTUDIO_PATH) or not os.path.exists(venv_python):
            logging.info("[SYSTEM] Label Studio is not installed.")
            return None
        
        # Get the installed version using the venv Python binary
        result = subprocess.run(
            [venv_python, "-m", "pip", "show", "label-studio"],
            stdout=subprocess.PIPE,
            text=True,
            check=True
        )
        
        # Parse the output to find the version
        for line in result.stdout.splitlines():
            if line.startswith("Version:"):
                version = line.split(":", 1)[1].strip()
                logging.info(f"[SYSTEM] Label Studio version: {version}")
                return version
        
        logging.info("[SYSTEM] Label Studio version not found in pip output.")
        return None
    except Exception as e:
        logging.error(f"[SYSTEM] Error getting Label Studio version: {e}")
        return None
    
def get_labelstudio_latest_version():
    """
    Get the latest available version of Label Studio from PyPI.

    Returns:
        str | None: The latest version of Label Studio, or None if not found.
    """
    try:
        response = requests.get("https://pypi.org/pypi/label-studio/json")
        response.raise_for_status()
        data = response.json()
        version = data.get("info", {}).get("version", None)
        if version:
            return version
        else:
            logging.info("[SYSTEM] Latest Label Studio version not found in PyPI response.")
            return None
    except Exception as e:
        logging.error(f"[SYSTEM] Error fetching latest Label Studio version: {e}")
        return None
    
def get_labelstudio_status():
    """
    Check if Label Studio is running.

    Returns:
        bool: True if Label Studio is running, False otherwise.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "labelstudio"],
            stdout=subprocess.PIPE,
            text=True,
            check=True
        )
        return result.stdout.strip() == "active"
    except subprocess.CalledProcessError:
        return False

def update_kittyhack(progress_callback=None, latest_version=None, current_version=None):
    """
    Update Kittyhack to the latest version, reporting progress via callback.

    Args:
        progress_callback (callable): Function(step, message, detail) for UI progress.
        latest_version (str): The version to update to.
        current_version (str): The current version (for rollback).

    Returns:
        bool: True if update succeeded, False otherwise.
    """

    steps = [
        ("Reverting local changes", ["/bin/git", "restore", "."]),
        ("Cleaning untracked files", ["/bin/git", "clean", "-fd"]),
        (f"Fetching latest version {latest_version}", ["/bin/git", "fetch", "--all", "--tags"]),
        (f"Checking out {latest_version}", ["/bin/git", "checkout", latest_version]),
        ("Updating python dependencies", ["/bin/bash", "-c", "source /root/kittyhack/.venv/bin/activate && pip install --timeout 120 --retries 10 -r /root/kittyhack/requirements.txt"]),
        ("Updating systemd service file", ["/bin/cp", "/root/kittyhack/setup/kittyhack.service", "/etc/systemd/system/kittyhack.service"]),
        ("Reloading systemd daemon", ["/bin/systemctl", "daemon-reload"]),
    ]
    for i, (msg, cmd) in enumerate(steps, 1):
        if progress_callback:
            progress_callback(i, msg, "")
        logging.info(msg)
        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            output_lines = []
            # Limit the length of callback messages to avoid flooding the UI
            max_detail_length = 120
            for line in process.stdout:
                line_stripped = line.strip()
                output_lines.append(line)
                logging.info(f"[UPDATE] {line_stripped}")
                if progress_callback:
                    # Send the latest line as detail, truncated if necessary
                    if len(line_stripped) > max_detail_length:
                        truncated_detail = line_stripped[:max_detail_length] + "..."
                        progress_callback(i, msg, truncated_detail)
                    else:
                        progress_callback(i, msg, line_stripped)
            process.wait()
            if process.returncode != 0:
                output = ''.join(output_lines)
                logging.error(f"Command failed with return code {process.returncode}:\n{output}")
                raise subprocess.CalledProcessError(process.returncode, cmd, output=output)
        except Exception as e:
            logging.error(f"Update step failed: {msg}: {e}")
            # Rollback logic
            if current_version:
                try:
                    subprocess.run(["/bin/git", "checkout", current_version], check=True)
                    subprocess.run(["/bin/bash", "-c", "source /root/kittyhack/.venv/bin/activate && pip install --timeout 120 --retries 10 -r /root/kittyhack/requirements.txt"], check=True)
                    subprocess.run(["/bin/cp", "/root/kittyhack/setup/kittyhack.service", "/etc/systemd/system/kittyhack.service"], check=True)
                    subprocess.run(["/bin/systemctl", "daemon-reload"], check=True)
                except Exception as rollback_e:
                    logging.error(f"Rollback failed: {rollback_e}")
            return False, str(e)
    return True, "Update completed"

def get_hostname():
    """
    Get the hostname of the system.

    Returns:
        str: The hostname.
    """
    try:
        hostname = subprocess.check_output(["hostname"], text=True).strip()
        logging.info(f"[SYSTEM] Hostname: {hostname}")
        return hostname
    except Exception as e:
        logging.error(f"[SYSTEM] Error getting hostname: {e}")
        return ""

def set_hostname(hostname):
    """
    Set the hostname of the system.

    Args:
        hostname (str): The new hostname to set.

    Returns:
        bool: True if the hostname was set successfully, False otherwise.
    """
    try:
        subprocess.run(["hostnamectl", "set-hostname", hostname], check=True)
        # Update /etc/hosts file
        with open("/etc/hosts", "r") as f:
            lines = f.readlines()
        with open("/etc/hosts", "w") as f:
            for line in lines:
                if "127.0.1.1" in line:
                    f.write(f"127.0.1.1\t{hostname}\n")
                else:
                    f.write(line)
        # Update /etc/hostname file
        with open("/etc/hostname", "w") as f:
            f.write(f"{hostname}\n")
        logging.info(f"[SYSTEM] Hostname set to: {hostname}")
        return True
    except Exception as e:
        logging.error(f"[SYSTEM] Error setting hostname: {e}")
        return False

def install_labelstudio(progress_callback=None):
    """
    Install Label Studio by creating a virtual environment, installing the package,
    and setting up a systemd service.

    Args:
        progress_callback (callable, optional): A callback function to report progress.
            The function should accept (step, message, detail) parameters.

    Returns:
        bool: True if installation is successful, False otherwise.
    """
    venv_path = os.path.join(LABELSTUDIO_PATH, LABELSTUDIO_VENV)
    venv_python = os.path.join(venv_path, "bin", "python")
    service_template_path = "/root/kittyhack/setup/labelstudio.service"
    service_file_path = "/etc/systemd/system/labelstudio.service"

    # Stop the service if it's running
    try:
        if os.path.exists(service_file_path):
            if is_service_running("labelstudio"):
                logging.info("[SYSTEM] Stopping existing Label Studio service...")
                if progress_callback:
                    progress_callback(0, _("Stopping existing Label Studio service..."), "")
                systemctl("stop", "labelstudio")
            subprocess.run(["rm", "-f", service_file_path], check=True)
            logging.info("[SYSTEM] Label Studio systemd service file removed.")
    except Exception as e:
        logging.error(f"[SYSTEM] Error stopping Label Studio service: {e}")
    
    # Remove the installation directory if it exists
    if os.path.exists(LABELSTUDIO_PATH):
        logging.info("[SYSTEM] Removing existing Label Studio installation...")
        if progress_callback:
            progress_callback(0, _("Removing existing Label Studio installation..."), "")
        try:
            subprocess.run(["rm", "-rf", LABELSTUDIO_PATH], check=True)
        except Exception as e:
            logging.error(f"[SYSTEM] Error removing existing Label Studio installation: {e}")

    if progress_callback:
        progress_callback(0, _("Starting Label Studio installation..."), _("This may take a few minutes..."))
        logging.info("[SYSTEM] Starting Label Studio installation...")
    
    try:
        # Step 1: Create a virtual environment        
        if not os.path.exists(venv_path):
            ok, output = run_with_progress(
                ["python3", "-m", "venv", venv_path],
                progress_callback,
                1,
                _("Creating virtual environment..."),
                _("This may take a moment...")
            )
            if not ok:
                logging.error("[SYSTEM] Virtual environment creation failed:\n" + output)
                return False
            logging.info("[SYSTEM] Label Studio virtual environment created.")
        else:
            logging.info("[SYSTEM] Virtual environment already exists, skipping creation.")
            if progress_callback:
                progress_callback(1, _("Virtual environment already exists"), _("Skipping creation..."))

        # Step 2: Install pip and dependencies
        ok, pip_upgrade_output = run_with_progress(
            [venv_python, "-m", "pip", "install", "--upgrade", "pip"],
            progress_callback,
            2,
            _("Upgrading pip..."),
            _("This may take a few minutes...")
        )
        if not ok:
            logging.error("[SYSTEM] Pip upgrade failed:\n" + pip_upgrade_output)
            return False
        logging.info("[SYSTEM] Pip upgraded successfully.")
        
        # Step 3: Install Label Studio
        ok, pip_output = run_with_progress(
            [venv_python, "-m", "pip", "install", "label-studio"],
            progress_callback,
            3,
            _("Installing Label Studio..."),
            _("This takes several minutes... Do not turn off the power or reload the page!")
        )
        if not ok:
            logging.error("[SYSTEM] Label Studio installation failed:\n" + pip_output)
            return False
        logging.info("[SYSTEM] Label Studio installed in virtual environment.")

        # Step 4: Create a systemd service file        
        if progress_callback:
            progress_callback(4, _("Creating systemd service..."), _("Almost done..."))
            
        if os.path.exists(service_template_path):
            try:
                with open(service_template_path, "r") as template_file:
                    service_content = template_file.read().replace("{{VENV_PATH}}", venv_path)
                with open(service_file_path, "w") as service_file:
                    service_file.write(service_content)
                logging.info("[SYSTEM] Label Studio systemd service file created.")
                if progress_callback:
                    progress_callback(4, _("Creating systemd service..."), _("Service file created successfully."))
            except Exception as e:
                logging.error(f"[SYSTEM] Error creating systemd service file: {e}")
                if progress_callback:
                    progress_callback(4, _("Creating systemd service..."), f"Error: {str(e)}")
                return False
        else:
            logging.error("[SYSTEM] Service template file not found.")
            if progress_callback:
                progress_callback(4, _("Creating systemd service..."), _("Error: Template file not found."))
            return False

        # Step 5: Reload systemd daemon
        ok, systemd_output = run_with_progress(
            ["/usr/bin/systemctl", "daemon-reload"],
            progress_callback,
            5,
            _("Reloading systemd services..."),
            _("Almost done...")
        )
        if not ok:
            logging.error("[SYSTEM] Systemd daemon reload failed:\n" + systemd_output)
            return False
        logging.info("[SYSTEM] Label Studio systemd daemon reloaded.")

        return True
    except Exception as e:
        logging.error(f"[SYSTEM] Error installing Label Studio: {e}")
        return False
    
def update_labelstudio(progress_callback=None):
    """
    Update Label Studio by upgrading the package in the virtual environment.

    Args:
        progress_callback (callable, optional): A callback function to report progress.
            The function should accept (step, message, detail) parameters.

    Returns:
        bool: True if update is successful, False otherwise.
    """

    if progress_callback:
        progress_callback(0, _("Starting Label Studio update..."), _("This may take a few minutes..."))

    try:
        systemctl("stop", "labelstudio")
        logging.info("[SYSTEM] Label Studio service stopped.")
    except Exception as e:
        logging.error(f"[SYSTEM] Error stopping Label Studio service: {e}")

    try:
        # Step 1: Upgrade Label Studio
        venv_python = os.path.join(LABELSTUDIO_PATH, LABELSTUDIO_VENV, "bin", "python")
        ok, pip_output = run_with_progress(
            [venv_python, "-m", "pip", "install", "--upgrade", "label-studio"],
            progress_callback,
            1,
            _("Upgrading Label Studio..."),
            _("This takes several minutes... Do not turn off the power or reload the page!")
        )
        if not ok:
            logging.error("[SYSTEM] Label Studio upgrade failed:\n" + pip_output)
            return False
        logging.info("[SYSTEM] Label Studio upgraded in virtual environment.")

        return True
    except Exception as e:
        logging.error(f"[SYSTEM] Error updating Label Studio: {e}")
        return False
    
def remove_labelstudio():
    """
    Remove Label Studio by deleting the virtual environment and systemd service.

    Returns:
        bool: True if removal is successful, False otherwise.
    """
    try:
        systemctl("stop", "labelstudio")
        logging.info("[SYSTEM] Label Studio service stopped.")
    except Exception as e:
        logging.error(f"[SYSTEM] Error stopping Label Studio service: {e}")
    
    try:
        # Remove the virtual environment
        venv_path = os.path.join(LABELSTUDIO_PATH, LABELSTUDIO_VENV)
        if os.path.exists(venv_path):
            subprocess.run(["rm", "-rf", venv_path], check=True)
            logging.info("[SYSTEM] Label Studio virtual environment removed.")

        # Remove the systemd service
        service_file_path = "/etc/systemd/system/labelstudio.service"
        if os.path.exists(service_file_path):
            subprocess.run(["rm", "-f", service_file_path], check=True)
            logging.info("[SYSTEM] Label Studio systemd service file removed.")

        return True
    except Exception as e:
        logging.error(f"[SYSTEM] Error removing Label Studio: {e}")
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