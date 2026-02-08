from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
import subprocess
import logging
import signal
import os
import threading
import requests
import shlex
import cv2
import numpy as np
import socket
import sys
import re
import htmltools
import fcntl
import struct
import uuid
import time as tm
from faicons import icon_svg
from src.baseconfig import set_language, update_single_config_parameter, CONFIG, AllowedToExit
from src.system import (
    is_service_running, 
    systemctl, 
    is_service_masked
)


_ = set_language(CONFIG['LANGUAGE'])

@dataclass
class Result:
    success: bool
    message: str

class EventType:
    MOTION_OUTSIDE_ONLY = "motion_outside_only"
    MOTION_OUTSIDE_WITH_MOUSE = "motion_outside_with_mouse"
    CAT_WENT_INSIDE = "cat_went_inside"
    CAT_WENT_PROBABLY_INSIDE = "cat_went_probably_inside"
    CAT_WENT_INSIDE_WITH_MOUSE = "cat_went_inside_with_mouse"
    CAT_WENT_OUTSIDE = "cat_went_outside"
    MANUALLY_UNLOCKED = "manually_unlocked"
    MANUALLY_LOCKED = "manually_locked"
    MAX_UNLOCK_TIME_EXCEEDED = "max_unlock_time_exceeded"
    # Per-cat mode informational flags (appended as additional verdict infos)
    PER_CAT_PREY_DISABLED = "per_cat_prey_detection_disabled"
    ENTRY_PER_CAT_ALLOWED = "entry_per_cat_allowed"
    ENTRY_PER_CAT_DENIED = "entry_per_cat_denied"
    EXIT_PER_CAT_ALLOWED = "exit_per_cat_allowed"
    EXIT_PER_CAT_DENIED = "exit_per_cat_denied"

    @staticmethod
    def to_pretty_string(event_type):
        return {
            EventType.MOTION_OUTSIDE_ONLY: _("Motion outside only"),
            EventType.MOTION_OUTSIDE_WITH_MOUSE: _("Motion outside with mouse"),
            EventType.CAT_WENT_INSIDE: _("Cat went inside"),
            EventType.CAT_WENT_PROBABLY_INSIDE: _("Cat went probably inside (no motion inside detected, but the flap was unlocked)"),
            EventType.CAT_WENT_INSIDE_WITH_MOUSE: _("Cat went inside with mouse"),
            EventType.CAT_WENT_OUTSIDE: _("Cat went outside"),
            EventType.MANUALLY_UNLOCKED: _("Manually unlocked flap"),
            EventType.MANUALLY_LOCKED: _("Manually locked flap"),
            EventType.MAX_UNLOCK_TIME_EXCEEDED: _("Maximum unlock time exceeded"),
            EventType.PER_CAT_PREY_DISABLED: _("Per-cat mode: prey detection disabled for this cat"),
            EventType.ENTRY_PER_CAT_ALLOWED: _("Per-cat mode: entry allowed for this cat"),
            EventType.ENTRY_PER_CAT_DENIED: _("Per-cat mode: entry denied for this cat"),
            EventType.EXIT_PER_CAT_ALLOWED: _("Per-cat mode: exit allowed for this cat"),
            EventType.EXIT_PER_CAT_DENIED: _("Per-cat mode: exit denied for this cat"),
        }.get(event_type, _("Unknown event"))

    @staticmethod
    def to_icons(event_type):
        return {
            EventType.MOTION_OUTSIDE_ONLY: [str(icon_svg("eye"))],
            EventType.MOTION_OUTSIDE_WITH_MOUSE: [str(icon_svg("hand")), icon_svg_local("mouse")],
            EventType.CAT_WENT_INSIDE: [str(icon_svg("circle-down"))],
            EventType.CAT_WENT_PROBABLY_INSIDE: [str(icon_svg("circle-down")), str(icon_svg("circle-question"))],
            EventType.CAT_WENT_INSIDE_WITH_MOUSE: [str(icon_svg("circle-down"))],
            EventType.CAT_WENT_OUTSIDE: [str(icon_svg("circle-up"))],
            EventType.MANUALLY_UNLOCKED: [str(icon_svg("lock-open"))],
            EventType.MANUALLY_LOCKED: [str(icon_svg("lock"))],
            EventType.MAX_UNLOCK_TIME_EXCEEDED: [str(icon_svg("clock"))],
            # Use information icons for per-cat notes
            EventType.PER_CAT_PREY_DISABLED: [str(icon_svg("circle-info"))],
            EventType.ENTRY_PER_CAT_ALLOWED: [str(icon_svg("circle-check"))],
            EventType.ENTRY_PER_CAT_DENIED: [str(icon_svg("circle-xmark"))],
            EventType.EXIT_PER_CAT_ALLOWED: [str(icon_svg("circle-check"))],
            EventType.EXIT_PER_CAT_DENIED: [str(icon_svg("circle-xmark"))],
        }.get(event_type, [str(icon_svg("circle-question"))])

def icon_svg_local(svg: str, margin_left: str | None = "auto", margin_right: str | None = "0.2em",) -> htmltools.TagChild:
    """
    Creates an HTML img tag with the path to a local SVG file.
    
    Args:
        svg (str): Name of the SVG file without extension
        
    Returns:
        htmltools.TagChild: HTML img tag with the SVG file as source
    """
    # NOTE: <img src="...svg"> does not inherit `currentColor` from the page,
    # so it won't follow light/dark theme colors. Using an SVG as a CSS mask
    # makes it reliably tintable via `background-color: currentColor`.
    return htmltools.span(
        role="img",
        aria_label=svg,
        style=f"""
        display:inline-block;
        background-color: currentColor;
        height:1em;
        width:1em;
        -webkit-mask: url('icons/{svg}.svg') no-repeat center / contain;
        mask: url('icons/{svg}.svg') no-repeat center / contain;
        margin-left: {margin_left};
        margin-right: {margin_right};
        position:relative;
        vertical-align:-0.125em;
        overflow:visible;
        outline-width: 0px;
        margin-top: 0;
        margin-bottom: 0;
        """,
    )


def filter_release_notes_for_language(markdown_text: str, language: str) -> str:
    """If release notes contain both 'Deutsch' and 'English' sections, return only the configured one."""
    if not isinstance(markdown_text, str):
        return markdown_text
    text = markdown_text.strip("\n")
    if not text:
        return markdown_text

    if language not in ("de", "en"):
        return markdown_text

    wanted_label = "Deutsch" if language == "de" else "English"

    # Expected style:
    #   # vX.Y.Z - Deutsch
    #   ...
    #   --------
    #   # vX.Y.Z - English
    #   ...
    header_re = re.compile(
        r"^\s*#+\s*v?\d+(?:\.\d+)*\s*-\s*(Deutsch|English)\s*$",
        flags=re.IGNORECASE,
    )

    lines = text.splitlines()
    blocks: dict[str, tuple[int, int]] = {}

    header_positions: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        m = header_re.match(line)
        if m:
            label = m.group(1)
            label = "Deutsch" if label.lower().startswith("de") else "English"
            header_positions.append((idx, label))

    labels_present = {label for _, label in header_positions}
    if not {"Deutsch", "English"}.issubset(labels_present):
        return markdown_text

    for i, (start_idx, label) in enumerate(header_positions):
        end_idx = header_positions[i + 1][0] if i + 1 < len(header_positions) else len(lines)
        blocks.setdefault(label, (start_idx, end_idx))

    if wanted_label not in blocks:
        return markdown_text

    start, end = blocks[wanted_label]
    chosen = lines[start:end]

    def is_separator(s: str) -> bool:
        s2 = (s or "").strip()
        if re.fullmatch(r"[-_]{3,}", s2):
            return True
        if re.fullmatch(r"(?:-\s*){3,}", s2):
            return True
        if re.fullmatch(r"(?:_\s*){3,}", s2):
            return True
        if re.fullmatch(r"\*{3,}", s2):
            return True
        return False

    def is_blank(s: str) -> bool:
        return not (s or "").strip()

    changed = True
    while chosen and changed:
        changed = False
        while chosen and is_blank(chosen[0]):
            chosen.pop(0)
            changed = True
        while chosen and is_blank(chosen[-1]):
            chosen.pop()
            changed = True
        while chosen and is_separator(chosen[0]):
            chosen.pop(0)
            changed = True
        while chosen and is_separator(chosen[-1]):
            chosen.pop()
            changed = True

    return "\n".join(chosen).strip("\n") or markdown_text

class GracefulKiller:
    def __init__(self):
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)
        self.stop_now = False
        self.tasks_done = threading.Event()
        self.tasks_count = 0
        self.lock = threading.Lock()
        self._shutdown_started = False

    def _get_tasks_count(self) -> int:
        with self.lock:
            return int(self.tasks_count)

    def _wait_for_tasks(self, timeout_s: float | None) -> bool:
        """Wait until all registered tasks are done.

        Returns True if all tasks finished before the timeout, else False.
        """
        deadline = None
        if timeout_s is not None:
            try:
                deadline = tm.monotonic() + float(timeout_s)
            except Exception:
                deadline = None

        while True:
            count = self._get_tasks_count()
            if count <= 0:
                return True

            if deadline is not None:
                try:
                    if tm.monotonic() >= deadline:
                        return False
                except Exception:
                    # If monotonic fails for some reason, fall back to best-effort waiting.
                    deadline = None

            # Wait a bit, then re-check.
            self.tasks_done.wait(timeout=0.5)

    def exit_gracefully(self, signum, frame):
        """
        Handles graceful shutdown (except for the shiny process itself) of the process upon 
        receiving a termination signal.

        This method sets a flag to indicate that the process should terminate, logs the
        intention to wait for all tasks to finish, and waits for the tasks to complete
        before forcefully killing the process.
        """
        if self._shutdown_started:
            return
        self._shutdown_started = True

        self.stop_now = True
        logging.info("Waiting for all tasks to finish...")

        with self.lock:
            if self.tasks_count <= 0:
                self.tasks_done.set()
            else:
                self.tasks_done.clear()

        # Keep this comfortably below systemd's TimeoutStopSec=30.
        all_tasks_done = self._wait_for_tasks(timeout_s=20.0)
        if not all_tasks_done:
            pending = self._get_tasks_count()
            logging.warning(f"Graceful shutdown timeout reached with {pending} pending task(s). Proceeding with forced shutdown to avoid systemd timeout.")

        # Set the shutdown flag (even if tasks were stuck) to avoid false NOT_GRACEFUL_SHUTDOWN increments.
        try:
            CONFIG['STARTUP_SHUTDOWN_FLAG'] = False
            update_single_config_parameter("STARTUP_SHUTDOWN_FLAG")
            logging.info("Updated STARTUP_SHUTDOWN_FLAG in the configfile to: False")
        except Exception as e:
            logging.error(f"Failed to update STARTUP_SHUTDOWN_FLAG during shutdown: {e}")

        # Best-effort: terminate child processes quickly (e.g., libcamera-vid).
        try:
            subprocess.run(["/usr/bin/pkill", "-TERM", "-P", str(os.getpid())], check=False)
        except Exception:
            pass

        logging.info("All tasks finished. Exiting now." if all_tasks_done else "Exiting now (forced).")
        try:
            subprocess.run(["/usr/bin/pkill", "-9", "-f", "shiny"], check=False)  # Ensure shiny exits
        finally:
            os._exit(0)

    def halt_backend(self):
        """
        Halts the backend by setting the stop_now flag to True.
        """
        self.stop_now = True
        with self.lock:
            if self.tasks_count <= 0:
                self.tasks_done.set()
            else:
                self.tasks_done.clear()
        self._wait_for_tasks(timeout_s=10.0)
        

    def signal_task_done(self):
        """
        Signals that a task has been completed.

        This method decrements the tasks_count by 1. If the tasks_count reaches 0,
        it sets the tasks_done event to indicate that all tasks have been completed.
        """
        with self.lock:
            self.tasks_count -= 1
            if self.tasks_count <= 0:
                self.tasks_done.set()

    def register_task(self):
        """
        Registers a new task by incrementing the tasks_count attribute.
        """
        with self.lock:
            self.tasks_count += 1
            if self.tasks_count > 0:
                self.tasks_done.clear()

sigterm_monitor = GracefulKiller()

def format_date_minmax(date: datetime, to_start=True):
    """
    Format a date at the start or end of the day.

    Parameters:
    - date (datetime): The date to format.
    - to_start (bool): If True, returns the start of the day (00:00:00).
                       If False, returns the end of the day (23:59:59.999999).

    Returns:
    - str: The formatted datetime string in 'YYYY-MM-DD HH:MM:SS'.
    """
    dt_time = time.min if to_start else time.max
    return datetime.combine(date, dt_time).strftime('%Y-%m-%d %H:%M:%S')

def get_git_version():
    """
    Retrieves the current Git version of the repository.

    This function attempts to get the current Git tag if the current commit
    has an exact match with a tag. If no tag is found, it returns the short
    commit hash of the current commit.

    Returns:
        str: The current Git tag if available, otherwise the short commit hash.
    """
    git_command = "/usr/bin/git" if os.name == "posix" else "git"

    try:
        # Check if the current commit has a tag
        tag = subprocess.check_output(
            [git_command, "describe", "--tags", "--exact-match"],
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        return tag
    except subprocess.CalledProcessError:
        # If no tag is found, return the short commit hash
        commit_hash = subprocess.check_output(
            [git_command, "rev-parse", "--short", "HEAD"],
            text=True
        ).strip()
        return commit_hash
    
def normalize_version(version_str):
    """
    Normalizes a version string by removing the 'v' prefix and commit hash suffix.
    """
    # Remove 'v' prefix if present
    if version_str.startswith('v'):
        version_str = version_str[1:]
    # Remove commit hash if present
    if '-' in version_str:
        version_str = version_str.split('-')[0]
    return version_str
    
def get_timezone():
    """
    Returns the timezone object from the configuration.
    Falls back to UTC if the timezone is unknown.
    """
    try:
        timezone = ZoneInfo(CONFIG['TIMEZONE'])
    except Exception:
        logging.error(f"Unknown timezone '{CONFIG['TIMEZONE']}'. Falling back to UTC.")
        timezone = ZoneInfo('UTC')
    return timezone

def get_utc_date_string(time: float):
    # Convert the time to a datetime object in UTC
    utc_datetime = datetime.fromtimestamp(time, tz=timezone.utc)
    
    # Format the datetime object to the specified string format with UTC offset
    utc_date_string = utc_datetime.strftime('%Y-%m-%d %H:%M:%S.%f')[:-2] + "+00:00"
    
    return utc_date_string

def get_local_date_from_utc_date(utc_date_string: str):
    """
    Converts a UTC date string to a local date string in the specified format.
    Args:
        utc_date_string (str): The UTC date string in the format '%Y-%m-%d %H:%M:%S.%f'.
    Returns:
        str: The local date string in the format '%Y-%m-%d %H:%M:%S.%f' (without the last two microseconds digits).
    """
    # Truncate the microseconds to 4 decimal places if necessary
    if '.' in utc_date_string:
        date_part, microseconds_part = utc_date_string.split('.')
        microseconds_part = microseconds_part[:4]
        utc_date_string = f"{date_part}.{microseconds_part}"

    # Convert the UTC date string to a datetime object
    utc_datetime = datetime.strptime(utc_date_string, '%Y-%m-%d %H:%M:%S.%f')
    
    # Convert the UTC datetime object to the local timezone
    local_datetime = utc_datetime.astimezone(get_timezone())
    
    # Format the local datetime object to the specified string format
    local_date_string = local_datetime.strftime('%Y-%m-%d %H:%M:%S.%f')[:-2]
    
    return local_date_string

def read_latest_kittyhack_version(timeout=10) -> str:
    """
    Reads the latest version of Kittyhack from the GitHub repository.
    If the version cannot be fetched, it returns 'unknown'.
    """
    try:
        ts_pre = tm.time()
        response = requests.get("https://api.github.com/repos/floppyFK/kittyhack/releases/latest", timeout=timeout)
        ts_post = tm.time()
        latest_version = str(response.json().get("tag_name", "unknown"))
        logging.info(f"GitHub latest version fetch took {ts_post - ts_pre:.3f} seconds. Latest version: {latest_version}")
        return latest_version
    except Exception as e:
        logging.error(f"Failed to fetch the latest version from GitHub: {e}")
        return "unknown"
    
def fetch_github_release_notes(version: str) -> str:
    """
    Fetches the release notes for a specific version from the Kittyhack GitHub repository.
    Args:
        version (str): The version tag (e.g., 'v2.0.0' or '2.0.0').
    Returns:
        str: The release notes (body) or an error message.
    """
    url = "https://api.github.com/repos/floppyFK/kittyhack/releases"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        releases = response.json()
        # Accept both 'vX.Y.Z' and 'X.Y.Z' as tags
        for release in releases:
            tag = release.get("tag_name", "")
            if tag == version or tag == f"v{version}" or tag.lstrip("v") == version.lstrip("v"):
                return release.get("body", "No release notes found.")
        return f"No release notes found for version {version}."
    except Exception as e:
        return f"Failed to fetch release notes: {e}"
    
def get_free_disk_space():
    """
    Returns the remaining disk space on the filesystem in MB.
    """
    try:
        stat = os.statvfs('/')
        return (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
    except Exception as e:
        logging.error(f"Failed to get the remaining disk space: {e}")
        return 0
    
def get_total_disk_space():
    """
    Returns the total disk space on the filesystem in MB.
    """
    try:
        stat = os.statvfs('/')
        return (stat.f_blocks * stat.f_frsize) / (1024 * 1024)
    except Exception as e:
        logging.error(f"Failed to get the total disk space: {e}")
        return 0
    
def get_used_ram_space():
    """
    Returns the used RAM space in MB.
    """
    try:
        with open('/proc/meminfo', 'r') as f:
            meminfo = f.readlines()
        for line in meminfo:
            if 'MemAvailable' in line:
                available_ram = int(line.split()[1]) / 1024  # Convert kB to MB
                break
        total_ram = get_total_ram_space()
        used_ram = total_ram - available_ram
        return used_ram
    except Exception as e:
        logging.error(f"Failed to get the used RAM space: {e}")
        return 0

def get_total_ram_space():
    """
    Returns the total RAM space in MB.
    """
    try:
        with open('/proc/meminfo', 'r') as f:
            meminfo = f.readlines()
        for line in meminfo:
            if 'MemTotal' in line:
                total_ram = int(line.split()[1]) / 1024  # Convert kB to MB
                return total_ram
    except Exception as e:
        logging.error(f"Failed to get the total RAM space: {e}")
        return 0
    
def get_database_size():
    """
    Returns the size of the Kittyhack database in MB.
    """
    try:
        db_path = CONFIG['KITTYHACK_DATABASE_PATH']
        if os.path.exists(db_path):
            return os.path.getsize(db_path) / (1024 * 1024)  # size in MB
        else:
            logging.error(f"Database file '{db_path}' does not exist.")
            return 0
    except Exception as e:
        logging.error(f"Failed to get the size of the Kittyhack database: {e}")
        return 0
    
def get_file_size(file_path):
    """
    Returns the size of a file in MB.
    """
    try:
        if os.path.exists(file_path):
            return os.path.getsize(file_path) / (1024 * 1024)  # size in MB
        else:
            logging.error(f"File '{file_path}' does not exist.")
            return 0
    except Exception as e:
        logging.error(f"Failed to get the size of the file '{file_path}': {e}")
        return 0
    
def execute_update_step(command: str, step_description: str) -> bool:
    """Execute a shell command and log its output."""
    try:
        cmd_list = shlex.split(command)
        result = subprocess.run(cmd_list, check=True, capture_output=True, text=True)
        if result.stdout:
            logging.info(f"[{step_description}] {result.stdout}")
        if result.stderr:
            logging.warning(f"[{step_description}] {result.stderr}")
        return True
    except subprocess.CalledProcessError as e:
        error_msg = f"[{step_description}] {str(e)}"
        logging.error(error_msg)
        return False

def resize_image_to_square(img: cv2.typing.MatLike, size: int = 800, quality: int = 85) -> bytes:
    """
    This function resizes an image to a square of the given size and returns the image as a byte array.
    
    :param img: Image array from cv2.imread().
    :param size: Size of the square image (default is 800).
    :param quality: Quality of the output image (default is 85).
    :return: Resized image as a byte array or None an error occurs.
    """
    try:
        if img is not None:
            # Crop and resize the image to the specified size
            height, width, _ = img.shape
            if height > width:
                diff = (height - width) // 2
                img_cropped = img[diff:diff + width, :]
            else:
                diff = (width - height) // 2
                img_cropped = img[:, diff:diff + height]
            
            img_resized = cv2.resize(img_cropped, (size, size))
            # Encode the image to jpg format
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
            _, img_blob = cv2.imencode('.jpg', img_resized, encode_param)
            return img_blob.tobytes()
    except:
        return None
    
def process_image(image_blob, target_width, target_height, quality):
    """
    Processes an image by resizing it to the target dimensions while maintaining the aspect ratio and encoding it with the specified quality.

    Args:
        image_blob (bytes): The image data in binary format.
        target_width (int): The target width for the resized image.
        target_height (int): The target height for the resized image.
        quality (int): The quality of the output JPEG image (0 to 100).

    Returns:
        bytes: The processed image as a bytes object, or None if an error occurs.
    """    
    try:
        # Convert the blob to a numpy array for OpenCV
        nparr = np.frombuffer(image_blob, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        # Resize the image while maintaining aspect ratio
        height, width = img.shape[:2]
        aspect = width / height
        if aspect > (target_width / target_height):
            new_width = target_width
            new_height = int(target_width / aspect)
        else:
            new_height = target_height
            new_width = int(target_height * aspect)
        resized = cv2.resize(img, (new_width, new_height), interpolation=cv2.INTER_AREA)
        # Encode the resized image with reduced quality
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
        _, encoded_img = cv2.imencode('.jpg', resized, encode_param)
        return encoded_img.tobytes()
    except Exception as e:
        logging.error(f"Failed to process image: {e}")
        return None
    
def check_and_stop_kittyflap_services(simulate_operations=False):
    """
    Validates if the Kittyflap services are running and stops them if necessary.
    Also masks services and renames executables to prevent them from running.
    
    Args:
        simulate_operations (bool): If True, only simulates the operations
    """

    # Remove 'manager' entries from the cron jobs
    try:
        # Check if the cron configuration contains 'manager' entries
        result = subprocess.run("crontab -l 2>/dev/null | grep 'manager'", shell=True, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout:
            # Remove 'manager' entries from the cron jobs
            subprocess.run("crontab -l 2>/dev/null | grep -v 'manager' | crontab -", shell=True, check=True)
            logging.info("Removed 'manager' entries from the cron jobs.")
        else:
            logging.info("No 'manager' entries found in the cron jobs.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to check or remove 'manager' entries from the cron jobs: {e}")

    services_to_manage = {
        'kwork': {'mask': False, 'delete': False},
        'manager': {'mask': True, 'delete': True},
        'setup': {'mask': True, 'delete': False},
        'mqtt': {'mask': True, 'delete': False}
    }

    # Stop and mask services
    # Check the 'delete' flag and remove the service file if set to True
    for service_name, options in services_to_manage.items():
        service_file = f"/etc/systemd/system/{service_name}.service"
        if options['delete'] and os.path.exists(service_file):
            try:
                os.remove(service_file)
                logging.info(f"Service file {service_file} deleted.")
            except Exception as e:
                logging.warning(f"Failed to delete service file {service_file}: {e}")
    for service_name, options in services_to_manage.items():
        if is_service_running(service_name, simulate_operations):
            logging.warning(f"The {service_name} service is running! Stopping it now.")
            try:
                systemctl("stop", service_name, simulate_operations)
                systemctl("disable", service_name, simulate_operations)
            except Exception as e:
                logging.error(f"Failed to stop the {service_name} service: {e}")
        
        if options['mask'] and not is_service_masked(service_name, simulate_operations):
            logging.warning(f"The {service_name} service is not masked! Masking it now.")
            try:
                systemctl("mask", service_name, simulate_operations)
            except Exception as e:
                logging.warning(f"Failed to mask the {service_name} service: {e}")

    # Rename executables
    import glob

    executables_to_disable = {
        "/root/kittyflap_versions/*/manager",
        "/root/kittyflap_versions/*/dependencies",
        "/root/kittyflap_versions/latest/main",
        "/root/manager"
    }

    for pattern in executables_to_disable:
        for path in glob.glob(pattern):
            if os.path.isfile(path):
                try:
                    os.rename(path, f"{path}_disabled")
                    logging.info(f"{os.path.basename(path)} executable renamed to {path}_disabled.")
                except Exception as e:
                    logging.error(f"Failed to rename {path}: {e}")
            else:
                logging.info(f"{os.path.basename(path)} executable not found. Skipping.")

def wait_for_network(timeout: int = 120) -> bool:
    interval = 1
    attempts = 0
    
    while attempts < timeout:
        try:
            # Check NTP synchronization status with timeout
            result = subprocess.run(['/usr/bin/timedatectl', 'status'], 
                                 capture_output=True, 
                                 text=True, 
                                 timeout=5)
            
            # Check if command was successful and contains sync info
            if result.returncode == 0 and 'System clock synchronized: yes' in result.stdout:
                # Test network connectivity
                socket.create_connection(("8.8.8.8", 53), timeout=1).close()
                logging.info("Network connectivity and time synchronization established")
                return True
            
        except (subprocess.TimeoutExpired, socket.error, subprocess.SubprocessError) as e:
            logging.debug(f"Network check attempt failed: {str(e)}")
        except Exception:
            pass
        
        attempts += interval
        tm.sleep(interval)
    logging.error(f"Failed to establish network connectivity after {timeout} seconds")
    return False

def log_relevant_deb_packages():
    """
    Logs the currently installed deb packages that are relevant based on the package name.
    """
    relevant_packages = ["libcamera", "gstreamer", "libpisp", "rpicam", "raspi"]
    try:
        result = subprocess.run(["dpkg", "-l"], capture_output=True, text=True, check=True)
        installed_packages = result.stdout.splitlines()
        for package in installed_packages:
            if any(relevant in package for relevant in relevant_packages):
                logging.info(f"Installed software package: {package}")
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to retrieve installed packages: {e}")

def log_system_information():
    """
    Logs relevant system information.
    """
    info_lines = []
    info_lines.append("\n---- System information: ------------------------------------------------")
    info_lines.append(f"System: {os.uname().sysname} {os.uname().release} {os.uname().machine}")
    info_lines.append(f"Python version: {sys.version}")
    info_lines.append(f"Git version: {get_git_version()}")
    info_lines.append(f"Kittyhack version: {CONFIG['LATEST_VERSION']}")
    info_lines.append(f"Free disk space: {get_free_disk_space():.2f} / {get_total_disk_space():.2f} MB")
    info_lines.append(f"Database size: {get_database_size():.2f} MB")
    
    # Memory information
    try:
        with open('/proc/meminfo', 'r') as f:
            mem_total = int(next(line for line in f if 'MemTotal' in line).split()[1]) // 1024
            f.seek(0)
            mem_available = int(next(line for line in f if 'MemAvailable' in line).split()[1]) // 1024
            info_lines.append(f"Memory: {mem_available}MB free of {mem_total}MB")
    except Exception as e:
        info_lines.append(f"Failed to get memory info: {e}")

    # CPU usage and temperature
    try:
        cpu_temp = subprocess.check_output(['vcgencmd', 'measure_temp']).decode().strip()
        info_lines.append(f"CPU Temperature: {cpu_temp}")
        
        # Get top processes sorted by CPU usage
        ps_cmd = ['ps', '-eo', 'pid,ppid,%mem,%cpu,args', '--sort=-%cpu', '--columns', '200']
        ps_output = subprocess.Popen(ps_cmd, stdout=subprocess.PIPE)
        grep_output = subprocess.Popen(['grep', '-v', 'ps -eo'], 
                                     stdin=ps_output.stdout,
                                     stdout=subprocess.PIPE)
        ps_output.stdout.close()
        head_output = subprocess.check_output(['head', '-n', '10'], 
                                            stdin=grep_output.stdout).decode()
        grep_output.stdout.close()
        info_lines.append(f"Top processes by CPU:\n{head_output}")
    except Exception as e:
        info_lines.append(f"Failed to get CPU info: {e}")

    # Network information
    try:
        wifi_info = subprocess.check_output(['iwconfig', 'wlan0']).decode()
        info_lines.append(f"WiFi status:\n{wifi_info}")
    except Exception as e:
        info_lines.append(f"Failed to get network info: {e}")
    
    # Check internet connectivity
    try:
        ping_result = subprocess.run(['ping', '-c', '1', '-W', '2', '8.8.8.8'], capture_output=True, text=True)
        info_lines.append(f"Internet connectivity: {'Connected' if ping_result.returncode == 0 else 'Disconnected'}")
    except Exception as e:
        info_lines.append(f"Failed to check internet connectivity: {e}")
    
    # System uptime
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.readline().split()[0])
            uptime_days = uptime_seconds / 86400  # Convert seconds to days
            info_lines.append(f"System uptime: {uptime_days:.1f} days")
    except Exception as e:
        info_lines.append(f"Failed to get uptime: {e}")

    # Journal errors from the last periodic interval
    try:
        interval_seconds = CONFIG['PERIODIC_JOBS_INTERVAL'] + 5
        journal_errors = subprocess.check_output(
            ['journalctl', '-p', 'err', '--since', f"{interval_seconds} seconds ago", '--no-pager'],
            stderr=subprocess.STDOUT
        ).decode()
        if journal_errors.strip():
            info_lines.append(f"System errors from the last {interval_seconds} seconds:\n{journal_errors}")
        else:
            info_lines.append("No systen errors in the specified time period")
    except Exception as e:
        info_lines.append(f"Failed to get journal errors: {e}")
    info_lines.append("-------------------------------------------------------------------------")

    # Log all information at once
    logging.info('\n'.join(info_lines))

def get_changelogs(after_version: str = "v1.0.0", language: str = "en") -> str:
    """
    Returns the changelogs from local files that match the specified language and are newer than the specified version.
    
    Args:
        after_version (str): Only return changelogs for versions newer than this one. Allowed formats: "X.Y.Z", "vX.Y.Z", "vX.Y.Z-1234abcd"
        language (str): The language code for the changelogs (defaults to "en")
    
    Returns:
        str: A string containing all changelog entries separated by horizontal lines
    """
    
    changelog_dir = "doc/changelogs/"
    changelog_entries = []
    
    # If the directory doesn't exist, return an empty list
    if not os.path.exists(changelog_dir):
        logging.warning(f"Changelog directory '{changelog_dir}' not found")
        return []
    
    # Get all changelog files
    try:
        files = os.listdir(changelog_dir)
    except Exception as e:
        logging.error(f"Failed to list changelog directory: {e}")
        return []
    
    # First try to find files in the requested language
    matching_files = [f for f in files if f.startswith("changelog_v") and f.endswith(f"_{language}.md")]
    
    # If no files found in the requested language, fall back to English
    if not matching_files and language != "en":
        matching_files = [f for f in files if f.startswith("changelog_v") and f.endswith("_en.md")]
        logging.info(f"No changelogs found for language '{language}', falling back to English")
    
    if not matching_files:
        logging.warning("No changelog files found")
        return []
    
    # Extract version from filename pattern "changelog_vX.Y.Z_lang.md"
    version_pattern = re.compile(r"changelog_v([0-9]+\.[0-9]+\.[0-9]+)_")
    
    # Filter files for versions newer than after_version
    newer_files = []
    after_version_tuple = parse_version(after_version) if after_version != "unknown" else (0, 0, 0)
    
    for file in matching_files:
        match = version_pattern.search(file)
        if match:
            file_version = match.group(1)
            file_version_tuple = parse_version(file_version)
            
            # Only include if this version is newer than after_version
            if after_version == "unknown" or file_version_tuple > after_version_tuple:
                newer_files.append((file_version_tuple, file))
    
    # Sort files by version (newest first)
    newer_files.sort(reverse=True)
    
    # Read contents of each file and add to changelog entries
    for _, filename in newer_files:
        try:
            with open(os.path.join(changelog_dir, filename), 'r', encoding='utf-8') as f:
                changelog_entries.append(f.read())
        except Exception as e:
            logging.error(f"Failed to read changelog file {filename}: {e}")
    
    # Join all entries with a horizontal line separator
    separator = "\n\n" + "-" * 80 + "\n\n"
    return separator.join(changelog_entries)

def parse_version(v_str):
    """
    Parse a version string to a comparable tuple.
    Handles formats X.Y.Z, vX.Y.Z, VX.Y.Z, and also git hashes like vX.Y.Z-1234abcd.
    The "v" prefix and the git hash suffix are ignored for comparison.
    """
    try:
        # Remove 'v' or 'V' prefix if present
        if v_str and v_str[0].lower() == 'v':
            v_str = v_str[1:]
            
        # Remove git hash suffix if present
        v_str = v_str.split('-')[0]
        
        # Parse the version components
        return tuple(map(int, v_str.split('.')))
    except:
        return (0, 0, 0)  # Default for unparseable versions
    
def check_allowed_to_exit():
    """
    Checks if the cat is allowed to exit based on the current time and the configured ranges and the general allowed_to_exit setting.
    Returns:
        bool: True if the cat is allowed to exit, False otherwise.
    """
    # If exit is globally denied, return False immediately
    if CONFIG['ALLOWED_TO_EXIT'] == AllowedToExit.DENY:
        logging.info("[CAT_EXIT_CHECK] Not allowed to exit, as the setting is disabled.")
        return False

    # For ALLOW and CONFIGURE_PER_CAT we evaluate time ranges (if any). In per-cat mode,
    # this function provides the base schedule; per-cat filtering happens in backend.
    if CONFIG['ALLOWED_TO_EXIT'] in (AllowedToExit.ALLOW, AllowedToExit.CONFIGURE_PER_CAT):
        now = datetime.now(get_timezone())
        current_time = now.strftime("%H:%M")

        # Check if the cat is allowed to exit based on the configured ranges
        # If any configured range allows exit, then exit is permitted
        any_range_configured = False
        allowed = False
        
        for i in [1, 2, 3]:
            # Check if the range is configured
            if CONFIG[f'ALLOWED_TO_EXIT_RANGE{i}']:
                any_range_configured = True
                start_time = CONFIG[f'ALLOWED_TO_EXIT_RANGE{i}_FROM']
                end_time = CONFIG[f'ALLOWED_TO_EXIT_RANGE{i}_TO']
                
                # Handle overnight ranges (e.g., 23:00-01:00)
                if start_time > end_time:  # This is an overnight range
                    if current_time >= start_time or current_time <= end_time:
                        logging.info(f"[CAT_EXIT_CHECK] Allowed to exit in overnight range {i} from {start_time} to {end_time} (current time: {current_time})")
                        allowed = True
                        break  # One allowed range is enough
                    else:
                        logging.info(f"[CAT_EXIT_CHECK] Not allowed to exit in overnight range {i} from {start_time} to {end_time} (current time: {current_time})")
                else:  # Regular range within same day
                    if start_time <= current_time <= end_time:
                        logging.info(f"[CAT_EXIT_CHECK] Allowed to exit in range {i} from {start_time} to {end_time} (current time: {current_time})")
                        allowed = True
                        break  # One allowed range is enough
                    else:
                        logging.info(f"[CAT_EXIT_CHECK] Not allowed to exit in range {i} from {start_time} to {end_time} (current time: {current_time})")
        
        # If no ranges are configured, default to allowed
        if not any_range_configured:
            allowed = True
            
        if allowed:
            logging.info("[CAT_EXIT_CHECK] Allowed to exit based on configured ranges.")
            return True
        else:
            logging.info("[CAT_EXIT_CHECK] Not allowed to exit based on configured ranges.")
            return False
    # Fallback safety
    return False
    
def get_current_ip(interface: str = "wlan0") -> str:
    """
    Returns the current IP address of the device.
    Args:
        interface (str): The network interface to check (default is 'wlan0').
    Returns:
        str: The current IP address of the device.
    """
    try:
        def get_ip_address(ifname):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            return socket.inet_ntoa(fcntl.ioctl(
                s.fileno(),
                0x8915,  # SIOCGIFADDR
                struct.pack('256s', ifname[:15].encode('utf-8'))
            )[20:24])

        ip_address = get_ip_address(interface)
        return ip_address
    except Exception as e:
        logging.error(f"Failed to get the IP address of 'wlan0': {e}")
        return None
    
def is_port_open(port, host='localhost'):
    """Check if a port is open on the given host."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)  # 1 second timeout
            result = s.connect_ex((host, port))
            return result == 0  # If result is 0, port is open
    except:
        return False
    
def is_valid_uuid4(s: str) -> bool:
    """
    Check if a string is a valid UUID4.
    """
    try:
        val = uuid.UUID(s, version=4)
    except ValueError:
        return False
    return val.version == 4 and str(val) == s.lower()