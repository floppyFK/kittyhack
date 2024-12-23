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


###### CONSTANT DEFINITIONS ######

# Files
CONFIGFILE = 'config.ini'
LOGFILE = "kittyhack.log"

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
        "max_photos_count": "2000",
        "simulate_kittyflap": False,
        "mouse_threshold": "70.0",
        "elements_per_page": "20",
        "loglevel": "INFO",
        "periodic_jobs_interval": "900",
        "allowed_to_enter": "all",
        "mouse_check_enabled": True
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
        self.tasks_done.wait()  # Wait until all tasks signal they are done
        logging.info("All tasks finished. Exiting now.")
        # FIXME: do a explicit SIGKILL on processes of "shiny" do not use os.gepid() here. Use "pkill -f shiny" instead
        subprocess.run(["/usr/bin/pkill", "-9", "-f", "shiny"])  # Send SIGKILL to "shiny" process

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
        "MAX_PHOTOS_COUNT": int(parser.get('Settings', 'max_photos_count', fallback=DEFAULT_CONFIG['Settings']['max_photos_count'])),
        "SIMULATE_KITTYFLAP": parser.getboolean('Settings', 'simulate_kittyflap', fallback=DEFAULT_CONFIG['Settings']['simulate_kittyflap']),
        "MOUSE_THRESHOLD": float(parser.get('Settings', 'mouse_threshold', fallback=DEFAULT_CONFIG['Settings']['mouse_threshold'])),
        "ELEMENTS_PER_PAGE": int(parser.get('Settings', 'elements_per_page', fallback=DEFAULT_CONFIG['Settings']['elements_per_page'])),
        "LOGLEVEL": parser.get('Settings', 'loglevel', fallback=DEFAULT_CONFIG['Settings']['loglevel']),
        "PERIODIC_JOBS_INTERVAL": int(parser.get('Settings', 'periodic_jobs_interval', fallback=DEFAULT_CONFIG['Settings']['periodic_jobs_interval'])),
        "ALLOWED_TO_ENTER": AllowedToEnter(parser.get('Settings', 'allowed_to_enter', fallback=DEFAULT_CONFIG['Settings']['allowed_to_enter'])),
        "MOUSE_CHECK_ENABLED": parser.getboolean('Settings', 'mouse_check_enabled', fallback=DEFAULT_CONFIG['Settings']['mouse_check_enabled'])
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
    settings['max_photos_count'] = str(CONFIG['MAX_PHOTOS_COUNT'])
    settings['simulate_kittyflap'] = str(CONFIG['SIMULATE_KITTYFLAP']).lower()
    settings['mouse_threshold'] = str(CONFIG['MOUSE_THRESHOLD'])
    settings['elements_per_page'] = str(CONFIG['ELEMENTS_PER_PAGE'])
    settings['loglevel'] = CONFIG['LOGLEVEL']
    settings['periodic_jobs_interval'] = str(CONFIG['PERIODIC_JOBS_INTERVAL'])
    settings['allowed_to_enter'] = str(CONFIG['ALLOWED_TO_ENTER']),
    settings['mouse_check_enabled'] = str(CONFIG['MOUSE_CHECK_ENABLED']).lower()

    # Write updated configuration back to the file
    try:
        with open(CONFIGFILE, 'w') as configfile:
            updater.write(configfile)
    except:
        logging.error("Failed to update the values in the configfile.")
        return False
    
    logging.info("Updated the values in the configfile")
    return True

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