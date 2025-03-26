from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
import gettext
import configparser
from enum import Enum
from configupdater import ConfigUpdater
import subprocess
import logging
import signal
import os
import threading
import requests
import shlex
import cv2
import numpy as np
import base64
import socket
import sys
import re
import htmltools
from faicons import icon_svg
from src.system import *


###### CONSTANT DEFINITIONS ######

# Files
CONFIGFILE = 'config.ini'
LOGFILE = "kittyhack.log"
JOURNAL_LOG = "/tmp/kittyhack-journal.log"

# Gettext constants
LOCALE_DIR = "locales"
DOMAIN = "messages"

# Global dictionary to store configuration settings
CONFIG = {}

# Default configuration values
DEFAULT_CONFIG = {
    "Settings": {
        "timezone": "Europe/Berlin",
        "language": "en",
        "date_format": "yyyy-mm-dd",
        "database_path": "../kittyflap.db",
        "kittyhack_database_path": "./kittyhack.db",
        "max_photos_count": 6000,
        "simulate_kittyflap": False,
        "mouse_threshold": 70.0,
        "no_mouse_threshold": 70.0,
        "min_threshold": 30.0,
        "elements_per_page": 20,
        "loglevel": "INFO",
        "periodic_jobs_interval": 900,
        "allowed_to_enter": "all",
        "mouse_check_enabled": True,
        "min_pictures_to_analyze": 5,
        "show_images_with_overlay": True,
        "live_view_refresh_interval": 5.0,
        "kittyflap_config_migrated": False,
        "allowed_to_exit": True,
        "last_vacuum_date": "",
        "periodic_version_check": True,
        "kittyflap_db_nagscreen": False,
        "last_db_backup_date": "",
        "kittyhack_database_backup_path": "../kittyhack_backup.db",
        "pir_outside_threshold": 0.5,
        "pir_inside_threshold": 3.0,
        "wlan_tx_power": 7,
        "group_pictures_to_events": True,
        "tflite_model_version": "original_kittyflap_model_v2",
        "lock_duration_after_prey_detection": 300,
        "last_read_changelogs": "v1.0.0",
        "max_pictures_per_event_with_rfid": 100,
        "max_pictures_per_event_without_rfid": 30,
        "use_all_cores_for_image_processing": False,
        "last_booted_version": "v1.5.1" # Parameter introduced in v1.5.1
    }
}


@dataclass
class Result:
    success: bool
    message: str

class AllowedToEnter(Enum):
    ALL = 'all'
    ALL_RFIDS = 'all_rfids'
    KNOWN = 'known'
    NONE = 'none'

class EventType:
    MOTION_OUTSIDE_ONLY = "motion_outside_only"
    MOTION_OUTSIDE_WITH_MOUSE = "motion_outside_with_mouse"
    CAT_WENT_INSIDE = "cat_went_inside"
    CAT_WENT_PROBABLY_INSIDE = "cat_went_probably_inside"
    CAT_WENT_INSIDE_WITH_MOUSE = "cat_went_inside_with_mouse"
    CAT_WENT_OUTSIDE = "cat_went_outside"

    @staticmethod
    def to_pretty_string(event_type):
        return {
            EventType.MOTION_OUTSIDE_ONLY: _("Motion outside only"),
            EventType.MOTION_OUTSIDE_WITH_MOUSE: _("Motion outside with mouse"),
            EventType.CAT_WENT_INSIDE: _("Cat went inside"),
            EventType.CAT_WENT_PROBABLY_INSIDE: _("Cat went probably inside (no motion inside detected, but the flap was unlocked)"),
            EventType.CAT_WENT_INSIDE_WITH_MOUSE: _("Cat went inside with mouse"),
            EventType.CAT_WENT_OUTSIDE: _("Cat went outside")
        }.get(event_type, _("Unknown event"))

    @staticmethod
    def to_icons(event_type):
        return {
            EventType.MOTION_OUTSIDE_ONLY: [str(icon_svg("eye"))],
            EventType.MOTION_OUTSIDE_WITH_MOUSE: [str(icon_svg("hand")), icon_svg_local("mouse")],
            EventType.CAT_WENT_INSIDE: [str(icon_svg("circle-down"))],
            EventType.CAT_WENT_PROBABLY_INSIDE: [str(icon_svg("circle-down")), str(icon_svg("circle-question"))],
            EventType.CAT_WENT_INSIDE_WITH_MOUSE: [str(icon_svg("circle-down"))],
            EventType.CAT_WENT_OUTSIDE: [str(icon_svg("circle-up"))]
        }.get(event_type, [str(icon_svg("circle-question"))])

def icon_svg_local(svg: str, margin_left: str | None = "auto", margin_right: str | None = "0.2em",) -> htmltools.TagChild:
    """
    Creates an HTML img tag with the path to a local SVG file.
    
    Args:
        svg (str): Name of the SVG file without extension
        
    Returns:
        htmltools.TagChild: HTML img tag with the SVG file as source
    """
    return htmltools.img(
        src=f"icons/{svg}.svg",
        alt=svg,
        style=f"""
        fill:currentColor;
        height:1em;
        width:1.0em;
        margin-left: {margin_left};
        margin-right: {margin_right};
        position:relative;
        vertical-align:-0.125em;
        overflow:visible;
        outline-width: 0px;
        margin-top: 0;
        margin-bottom: 0;
        """
    )

class GracefulKiller:
    stop_now = False

    def __init__(self):
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)
        self.tasks_done = threading.Event()
        self.tasks_count = 0
        self.lock = threading.Lock()

    def exit_gracefully(self, signum, frame):
        """
        Handles graceful shutdown (except for the shiny process itself) of the process upon 
        receiving a termination signal.

        This method sets a flag to indicate that the process should terminate, logs the
        intention to wait for all tasks to finish, and waits for the tasks to complete
        before forcefully killing the process.
        """
        self.stop_now = True
        logging.info("Waiting for all tasks to finish...")
        with self.lock:
            if self.tasks_count == 0:
                self.tasks_done.set()
            else:
                self.tasks_done.clear()
        while self.tasks_count > 0:
            self.tasks_done.wait(timeout=1)  # Wait until all tasks signal they are done
        logging.info("All tasks finished. Exiting now.")
        subprocess.run(["/usr/bin/pkill", "-9", "-f", "shiny"])  # Send SIGKILL to "shiny" process

    def halt_backend(self):
        """
        Halts the backend by setting the stop_now flag to True.
        """
        self.stop_now = True
        with self.lock:
            if self.tasks_count == 0:
                self.tasks_done.set()
            else:
                self.tasks_done.clear()
        while self.tasks_count > 0:
            self.tasks_done.wait(timeout=1)  # Wait until all tasks signal they are done
        

    def signal_task_done(self):
        """
        Signals that a task has been completed.

        This method decrements the tasks_count by 1. If the tasks_count reaches 0,
        it sets the tasks_done event to indicate that all tasks have been completed.
        """
        with self.lock:
            self.tasks_count -= 1
            if self.tasks_count == 0:
                self.tasks_done.set()

    def register_task(self):
        """
        Registers a new task by incrementing the tasks_count attribute.
        """
        with self.lock:
            self.tasks_count += 1

sigterm_monitor = GracefulKiller()

def create_default_config():
    """
    Creates the configuration file with default values.
    """
    parser = configparser.ConfigParser()
    parser.read_dict(DEFAULT_CONFIG)
    with open(CONFIGFILE, 'w') as configfile:
        parser.write(configfile)
    logging.info(f"Default configuration written to {CONFIGFILE}")

def load_config():
    """
    Loads the configuration file and populates the CONFIG dictionary.
    """
    global CONFIG
    if not os.path.exists(CONFIGFILE):
        print(f"Configuration file '{CONFIGFILE}' not found. Creating with default values...")
        create_default_config()
    
    parser = configparser.ConfigParser()
    parser.read(CONFIGFILE)
    
    CONFIG = {
        "TIMEZONE": parser.get('Settings', 'timezone', fallback=DEFAULT_CONFIG['Settings']['timezone']),
        "LANGUAGE": parser.get('Settings', 'language', fallback=DEFAULT_CONFIG['Settings']['language']),
        "DATE_FORMAT": parser.get('Settings', 'date_format', fallback=DEFAULT_CONFIG['Settings']['date_format']),
        "DATABASE_PATH": parser.get('Settings', 'database_path', fallback=DEFAULT_CONFIG['Settings']['database_path']),
        "KITTYHACK_DATABASE_PATH": parser.get('Settings', 'kittyhack_database_path', fallback=DEFAULT_CONFIG['Settings']['kittyhack_database_path']),
        "MAX_PHOTOS_COUNT": parser.getint('Settings', 'max_photos_count', fallback=DEFAULT_CONFIG['Settings']['max_photos_count']),
        "SIMULATE_KITTYFLAP": parser.getboolean('Settings', 'simulate_kittyflap', fallback=DEFAULT_CONFIG['Settings']['simulate_kittyflap']),
        "MOUSE_THRESHOLD": parser.getfloat('Settings', 'mouse_threshold', fallback=DEFAULT_CONFIG['Settings']['mouse_threshold']), # Currently not used
        "NO_MOUSE_THRESHOLD": parser.getfloat('Settings', 'no_mouse_threshold', fallback=DEFAULT_CONFIG['Settings']['no_mouse_threshold']),
        "MIN_THRESHOLD": parser.getfloat('Settings', 'min_threshold', fallback=DEFAULT_CONFIG['Settings']['min_threshold']),
        "ELEMENTS_PER_PAGE": parser.getint('Settings', 'elements_per_page', fallback=DEFAULT_CONFIG['Settings']['elements_per_page']),
        "LOGLEVEL": parser.get('Settings', 'loglevel', fallback=DEFAULT_CONFIG['Settings']['loglevel']),
        "PERIODIC_JOBS_INTERVAL": parser.getint('Settings', 'periodic_jobs_interval', fallback=DEFAULT_CONFIG['Settings']['periodic_jobs_interval']),
        "ALLOWED_TO_ENTER": AllowedToEnter(parser.get('Settings', 'allowed_to_enter', fallback=DEFAULT_CONFIG['Settings']['allowed_to_enter'])),
        "MOUSE_CHECK_ENABLED": parser.getboolean('Settings', 'mouse_check_enabled', fallback=DEFAULT_CONFIG['Settings']['mouse_check_enabled']),
        "MIN_PICTURES_TO_ANALYZE": parser.getint('Settings', 'min_pictures_to_analyze', fallback=DEFAULT_CONFIG['Settings']['min_pictures_to_analyze']),
        "SHOW_IMAGES_WITH_OVERLAY": parser.getboolean('Settings', 'show_images_with_overlay', fallback=DEFAULT_CONFIG['Settings']['show_images_with_overlay']),
        "LIVE_VIEW_REFRESH_INTERVAL": parser.getfloat('Settings', 'live_view_refresh_interval', fallback=DEFAULT_CONFIG['Settings']['live_view_refresh_interval']),
        "KITTYFLAP_CONFIG_MIGRATED": parser.getboolean('Settings', 'kittyflap_config_migrated', fallback=DEFAULT_CONFIG['Settings']['kittyflap_config_migrated']),
        "ALLOWED_TO_EXIT": parser.getboolean('Settings', 'allowed_to_exit', fallback=DEFAULT_CONFIG['Settings']['allowed_to_exit']),
        "LAST_VACUUM_DATE": parser.get('Settings', 'last_vacuum_date', fallback=DEFAULT_CONFIG['Settings']['last_vacuum_date']),
        "PERIODIC_VERSION_CHECK": parser.getboolean('Settings', 'periodic_version_check', fallback=DEFAULT_CONFIG['Settings']['periodic_version_check']),
        "KITTYFLAP_DB_NAGSCREEN": parser.getboolean('Settings', 'kittyflap_db_nagscreen', fallback=DEFAULT_CONFIG['Settings']['kittyflap_db_nagscreen']),
        "LATEST_VERSION": "unknown", # This value will not be written to the config file
        "LAST_DB_BACKUP_DATE": parser.get('Settings', 'last_db_backup_date', fallback=DEFAULT_CONFIG['Settings']['last_db_backup_date']),
        "KITTYHACK_DATABASE_BACKUP_PATH": parser.get('Settings', 'kittyhack_database_backup_path', fallback=DEFAULT_CONFIG['Settings']['kittyhack_database_backup_path']),
        "PIR_OUTSIDE_THRESHOLD": parser.getfloat('Settings', 'pir_outside_threshold', fallback=DEFAULT_CONFIG['Settings']['pir_outside_threshold']),
        "PIR_INSIDE_THRESHOLD": parser.getfloat('Settings', 'pir_inside_threshold', fallback=DEFAULT_CONFIG['Settings']['pir_inside_threshold']),
        "WLAN_TX_POWER": parser.getint('Settings', 'wlan_tx_power', fallback=DEFAULT_CONFIG['Settings']['wlan_tx_power']),
        "GROUP_PICTURES_TO_EVENTS": parser.getboolean('Settings', 'group_pictures_to_events', fallback=DEFAULT_CONFIG['Settings']['group_pictures_to_events']),
        "TFLITE_MODEL_VERSION": parser.get('Settings', 'tflite_model_version', fallback=DEFAULT_CONFIG['Settings']['tflite_model_version']),
        "LOCK_DURATION_AFTER_PREY_DETECTION": parser.getint('Settings', 'lock_duration_after_prey_detection', fallback=DEFAULT_CONFIG['Settings']['lock_duration_after_prey_detection']),
        "LAST_READ_CHANGELOGS": parser.get('Settings', 'last_read_changelogs', fallback=DEFAULT_CONFIG['Settings']['last_read_changelogs']),
        "MAX_PICTURES_PER_EVENT_WITH_RFID": parser.getint('Settings', 'max_pictures_per_event_with_rfid', fallback=DEFAULT_CONFIG['Settings']['max_pictures_per_event_with_rfid']),
        "MAX_PICTURES_PER_EVENT_WITHOUT_RFID": parser.getint('Settings', 'max_pictures_per_event_without_rfid', fallback=DEFAULT_CONFIG['Settings']['max_pictures_per_event_without_rfid']),
        "USE_ALL_CORES_FOR_IMAGE_PROCESSING": parser.getboolean('Settings', 'use_all_cores_for_image_processing', fallback=DEFAULT_CONFIG['Settings']['use_all_cores_for_image_processing']),
        "LAST_BOOTED_VERSION": parser.get('Settings', 'last_booted_version', fallback=DEFAULT_CONFIG['Settings']['last_booted_version'])
    }

def save_config():
    """
    Saves the configuration file.
    Requires the CONFIG
    """
    # prepare the updated values for the configfile
    updater = ConfigUpdater()
    updater.read(CONFIGFILE)

    settings = updater['Settings']
    settings['timezone'] = CONFIG['TIMEZONE']
    settings['language'] = CONFIG['LANGUAGE']
    settings['date_format'] = CONFIG['DATE_FORMAT']
    settings['database_path'] = CONFIG['DATABASE_PATH']
    settings['kittyhack_database_path'] = CONFIG['KITTYHACK_DATABASE_PATH']
    settings['max_photos_count'] = CONFIG['MAX_PHOTOS_COUNT']
    settings['simulate_kittyflap'] = CONFIG['SIMULATE_KITTYFLAP']
    settings['mouse_threshold'] = CONFIG['MOUSE_THRESHOLD']
    settings['no_mouse_threshold'] = CONFIG['NO_MOUSE_THRESHOLD']
    settings['min_threshold'] = CONFIG['MIN_THRESHOLD']
    settings['elements_per_page'] = CONFIG['ELEMENTS_PER_PAGE']
    settings['loglevel'] = CONFIG['LOGLEVEL']
    settings['periodic_jobs_interval'] = CONFIG['PERIODIC_JOBS_INTERVAL']
    settings['allowed_to_enter'] = CONFIG['ALLOWED_TO_ENTER'].value
    settings['mouse_check_enabled'] = str(CONFIG['MOUSE_CHECK_ENABLED'])
    settings['min_pictures_to_analyze'] = CONFIG['MIN_PICTURES_TO_ANALYZE']
    settings['show_images_with_overlay'] = CONFIG['SHOW_IMAGES_WITH_OVERLAY']
    settings['live_view_refresh_interval'] = CONFIG['LIVE_VIEW_REFRESH_INTERVAL']
    settings['kittyflap_config_migrated'] = CONFIG['KITTYFLAP_CONFIG_MIGRATED']
    settings['allowed_to_exit'] = CONFIG['ALLOWED_TO_EXIT']
    settings['last_vacuum_date'] = CONFIG['LAST_VACUUM_DATE']
    settings['periodic_version_check'] = CONFIG['PERIODIC_VERSION_CHECK']
    settings['kittyflap_db_nagscreen'] = CONFIG['KITTYFLAP_DB_NAGSCREEN']
    settings['last_db_backup_date'] = CONFIG['LAST_DB_BACKUP_DATE']
    settings['kittyhack_database_backup_path'] = CONFIG['KITTYHACK_DATABASE_BACKUP_PATH']
    settings['pir_outside_threshold'] = CONFIG['PIR_OUTSIDE_THRESHOLD']
    settings['pir_inside_threshold'] = CONFIG['PIR_INSIDE_THRESHOLD']
    settings['wlan_tx_power'] = CONFIG['WLAN_TX_POWER']
    settings['group_pictures_to_events'] = CONFIG['GROUP_PICTURES_TO_EVENTS']
    settings['tflite_model_version'] = CONFIG['TFLITE_MODEL_VERSION']
    settings['lock_duration_after_prey_detection'] = CONFIG['LOCK_DURATION_AFTER_PREY_DETECTION']
    settings['last_read_changelogs'] = CONFIG['LAST_READ_CHANGELOGS']
    settings['max_pictures_per_event_with_rfid'] = CONFIG['MAX_PICTURES_PER_EVENT_WITH_RFID']
    settings['max_pictures_per_event_without_rfid'] = CONFIG['MAX_PICTURES_PER_EVENT_WITHOUT_RFID']
    settings['use_all_cores_for_image_processing'] = CONFIG['USE_ALL_CORES_FOR_IMAGE_PROCESSING']
    settings['last_booted_version'] = CONFIG['LAST_BOOTED_VERSION']

    # Write updated configuration back to the file
    try:
        with open(CONFIGFILE, 'w') as configfile:
            updater.write(configfile)
    except:
        logging.error("Failed to update the values in the configfile.")
        return False
    
    logging.info("Updated the values in the configfile")
    return True

def update_config_images_overlay():
    """
    Updates only the SHOW_IMAGES_WITH_OVERLAY setting in the configuration file.
    """
    updater = ConfigUpdater()
    updater.read(CONFIGFILE)
    updater['Settings']['show_images_with_overlay'] = CONFIG['SHOW_IMAGES_WITH_OVERLAY']

    # Write updated configuration back to the file
    try:
        with open(CONFIGFILE, 'w') as configfile:
            updater.write(configfile)
        logging.info("Updated SHOW_IMAGES_WITH_OVERLAY in the configfile")
    except Exception as e:
        logging.error(f"Failed to update SHOW_IMAGES_WITH_OVERLAY in the configfile: {e}")

def update_single_config_parameter(parameter: str):
    """
    Updates only a single config parameter in the configuration file.

    Args:
        parameter (str): The parameter name, which shall be updated.
    """
    updater = ConfigUpdater()
    updater.read(CONFIGFILE)
    updater['Settings'][parameter.lower()] = CONFIG[parameter.upper()]

    # Write updated configuration back to the file
    try:
        with open(CONFIGFILE, 'w') as configfile:
            updater.write(configfile)
        logging.info(f"Updated {parameter.upper()} in the configfile to: {CONFIG[parameter.upper()]}")
    except Exception as e:
        logging.error(f"Failed to update {parameter.upper()} in the configfile: {e}")

# Initial load of the configuration
load_config()

# Function to set the language
def set_language(language_code):
    """Load translations for the specified language."""
    gettext.bindtextdomain(DOMAIN, LOCALE_DIR)
    gettext.textdomain(DOMAIN)
    lang = gettext.translation(DOMAIN, localedir=LOCALE_DIR, languages=[language_code], fallback=True)
    lang.install()
    global _
    _ = lang.gettext

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

def read_latest_kittyhack_version():
    """
    Reads the latest version of Kittyhack from the GitHub repository.
    If the version cannot be fetched, it returns 'unknown'.
    """    
    try:
        response = requests.get("https://api.github.com/repos/floppyFK/kittyhack/releases/latest", timeout=10)
        latest_version = str(response.json().get("tag_name", "unknown"))
        return latest_version
    except Exception as e:
        logging.error(f"Failed to fetch the latest version from GitHub: {e}")
        return "unknown"
    
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
