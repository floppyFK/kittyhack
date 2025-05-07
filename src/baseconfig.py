import os
import gettext
import configparser
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from zoneinfo import ZoneInfo
from enum import Enum
import uuid
import json
from configupdater import ConfigUpdater

###### ENUM DEFINITIONS ######
class AllowedToEnter(Enum):
    ALL = 'all'
    ALL_RFIDS = 'all_rfids'
    KNOWN = 'known'
    NONE = 'none'

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
        "last_booted_version": "v1.5.1", # Parameter introduced in v1.5.1
        "allowed_to_exit_range1": False,
        "allowed_to_exit_range1_from": "00:00",
        "allowed_to_exit_range1_to": "23:59",
        "allowed_to_exit_range2": False,
        "allowed_to_exit_range2_from": "00:00",
        "allowed_to_exit_range2_to": "23:59",
        "allowed_to_exit_range3": False,
        "allowed_to_exit_range3_from": "00:00",
        "allowed_to_exit_range3_to": "23:59",
        "labelstudio_version": None,
        "email": "",
        "user_name": "",
        "model_training": "",
        "yolo_model": "",
        "startup_shutdown_flag": False,
        "not_graceful_shutdowns": 0
    }
}

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
        "LAST_BOOTED_VERSION": parser.get('Settings', 'last_booted_version', fallback=DEFAULT_CONFIG['Settings']['last_booted_version']),
        "ALLOWED_TO_EXIT_RANGE1": parser.getboolean('Settings', 'allowed_to_exit_range1', fallback=DEFAULT_CONFIG['Settings']['allowed_to_exit_range1']),
        "ALLOWED_TO_EXIT_RANGE1_FROM": parser.get('Settings', 'allowed_to_exit_range1_from', fallback=DEFAULT_CONFIG['Settings']['allowed_to_exit_range1_from']),
        "ALLOWED_TO_EXIT_RANGE1_TO": parser.get('Settings', 'allowed_to_exit_range1_to', fallback=DEFAULT_CONFIG['Settings']['allowed_to_exit_range1_to']),
        "ALLOWED_TO_EXIT_RANGE2": parser.getboolean('Settings', 'allowed_to_exit_range2', fallback=DEFAULT_CONFIG['Settings']['allowed_to_exit_range2']),
        "ALLOWED_TO_EXIT_RANGE2_FROM": parser.get('Settings', 'allowed_to_exit_range2_from', fallback=DEFAULT_CONFIG['Settings']['allowed_to_exit_range2_from']),
        "ALLOWED_TO_EXIT_RANGE2_TO": parser.get('Settings', 'allowed_to_exit_range2_to', fallback=DEFAULT_CONFIG['Settings']['allowed_to_exit_range2_to']),
        "ALLOWED_TO_EXIT_RANGE3": parser.getboolean('Settings', 'allowed_to_exit_range3', fallback=DEFAULT_CONFIG['Settings']['allowed_to_exit_range3']),
        "ALLOWED_TO_EXIT_RANGE3_FROM": parser.get('Settings', 'allowed_to_exit_range3_from', fallback=DEFAULT_CONFIG['Settings']['allowed_to_exit_range3_from']),
        "ALLOWED_TO_EXIT_RANGE3_TO": parser.get('Settings', 'allowed_to_exit_range3_to', fallback=DEFAULT_CONFIG['Settings']['allowed_to_exit_range3_to']),
        "LABELSTUDIO_VERSION": parser.get('Settings', 'labelstudio_version', fallback=DEFAULT_CONFIG['Settings']['labelstudio_version']),
        "EMAIL": parser.get('Settings', 'email', fallback=DEFAULT_CONFIG['Settings']['email']),
        "USER_NAME": parser.get('Settings', 'user_name', fallback=DEFAULT_CONFIG['Settings']['user_name']),
        "MODEL_TRAINING": parser.get('Settings', 'model_training', fallback=DEFAULT_CONFIG['Settings']['model_training']),
        "YOLO_MODEL": parser.get('Settings', 'yolo_model', fallback=DEFAULT_CONFIG['Settings']['yolo_model']),
        "STARTUP_SHUTDOWN_FLAG": parser.getboolean('Settings', 'startup_shutdown_flag', fallback=DEFAULT_CONFIG['Settings']['startup_shutdown_flag']),
        "NOT_GRACEFUL_SHUTDOWNS": parser.getint('Settings', 'not_graceful_shutdowns', fallback=DEFAULT_CONFIG['Settings']['not_graceful_shutdowns'])
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
    settings['allowed_to_exit_range1'] = CONFIG['ALLOWED_TO_EXIT_RANGE1']
    settings['allowed_to_exit_range1_from'] = CONFIG['ALLOWED_TO_EXIT_RANGE1_FROM']
    settings['allowed_to_exit_range1_to'] = CONFIG['ALLOWED_TO_EXIT_RANGE1_TO']
    settings['allowed_to_exit_range2'] = CONFIG['ALLOWED_TO_EXIT_RANGE2']
    settings['allowed_to_exit_range2_from'] = CONFIG['ALLOWED_TO_EXIT_RANGE2_FROM']
    settings['allowed_to_exit_range2_to'] = CONFIG['ALLOWED_TO_EXIT_RANGE2_TO']
    settings['allowed_to_exit_range3'] = CONFIG['ALLOWED_TO_EXIT_RANGE3']
    settings['allowed_to_exit_range3_from'] = CONFIG['ALLOWED_TO_EXIT_RANGE3_FROM']
    settings['allowed_to_exit_range3_to'] = CONFIG['ALLOWED_TO_EXIT_RANGE3_TO']
    #settings['labelstudio_version'] = CONFIG['LABELSTUDIO_VERSION'] # This value may not be written to the config file
    settings['email'] = CONFIG['EMAIL']
    settings['user_name'] = CONFIG['USER_NAME']
    settings['model_training'] = CONFIG['MODEL_TRAINING']
    settings['yolo_model'] = CONFIG['YOLO_MODEL']
    settings['startup_shutdown_flag'] = CONFIG['STARTUP_SHUTDOWN_FLAG']
    settings['not_graceful_shutdowns'] = CONFIG['NOT_GRACEFUL_SHUTDOWNS']

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

def create_default_config():
    """
    Creates the configuration file with default values.
    """
    parser = configparser.ConfigParser()
    parser.read_dict(DEFAULT_CONFIG)
    with open(CONFIGFILE, 'w') as configfile:
        parser.write(configfile)
    logging.info(f"Default configuration written to {CONFIGFILE}")

def set_language(language_code = "de"):
    """Load translations for the specified language."""
    gettext.bindtextdomain(DOMAIN, LOCALE_DIR)
    gettext.textdomain(DOMAIN)
    lang = gettext.translation(DOMAIN, localedir=LOCALE_DIR, languages=[language_code], fallback=True)
    lang.install()
    return lang.gettext

def configure_logging(level_name: str = "INFO"):
    """
    Configures the logging settings.
    """
    level = logging._nameToLevel.get(level_name.upper(), logging.INFO)

    # Remove all existing handlers from the root logger
    for h in logging.root.handlers[:]:
        logging.root.removeHandler(h)

    # Create a rotating file handler for logging
    # This handler will create log files with a maximum size of 10 MB each and keep up to 5 backup files
    handler = RotatingFileHandler(LOGFILE, maxBytes=10*1024*1024, backupCount=5)

    # Define the format for log messages
    formatter = TimeZoneFormatter('%(asctime)s [%(levelname)s] %(message)s')
    handler.setFormatter(formatter)

    # Get the root logger and set its level and handler
    logger = logging.getLogger()
    logger.setLevel(level)
    logger.addHandler(handler)
    logging.info(f"Logger loglevel set to {level_name.upper()}")

# Custom formatter with timezone-aware local time
class TimeZoneFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        # Get current time in local timezone
        local_time = datetime.fromtimestamp(record.created, tz=ZoneInfo(CONFIG['TIMEZONE']))
        
        # Build the timestamp with milliseconds and timezone offset
        timestamp = local_time.strftime('%Y-%m-%d %H:%M:%S')
        milliseconds = f"{local_time.microsecond // 1000:03d}"
        timezone = local_time.strftime('%z (%Z)')

        return f"{timestamp}.{milliseconds} {timezone}"
    
class UserNotifications:
    """
    Class to handle user notifications.
    The notifications are stored in a json file and will be displayed to the user when he opens the web interface.
    """
    notifications = []

    def __init__(cls):
        cls.load()

    @classmethod
    def load(cls):
        """
        Load notifications from the json file.
        """
        try:
            with open("notifications.json", "r") as f:
                cls.notifications = json.load(f)
        except FileNotFoundError:
            cls.notifications = []
        except json.JSONDecodeError:
            logging.error("[USR_NOTIFICATIONS] Failed to decode notifications.json. Starting with an empty list.")
            cls.notifications = []

    @classmethod
    def save(cls):
        """
        Save notifications to the json file.
        """
        with open("notifications.json", "w") as f:
            json.dump(cls.notifications, f, indent=4)

    @classmethod
    def add(cls, header, message, type="default", id=None, skip_if_id_exists=False):
        """
        Add a notification to the list.
        Args:
            header (str): The header of the notification.
            message (str): The message of the notification.
            type (str): The type of the notification. Can be "default", "message", "warning", "error"
            id (str): The id of the notification. If None, a random id will be generated.
            skip_if_id_exists (bool): If True, skip adding the notification if the id already exists.
        """
        if id is None:
            id = str(uuid.uuid4())
        if skip_if_id_exists and any(n['id'] == id for n in cls.notifications):
            return
        cls.notifications.append({
            "id": id,
            "header": header,
            "message": message,
            "type": type
        })
        cls.save()
        logging.info(f"[USR_NOTIFICATIONS] Added notification: {header} - {message} (type: {type})")
        return id

    @classmethod
    def remove(cls, id: str):
        """
        Remove a notification from the list.
        Args:
            id (str): The id of the notification to remove.
        """
        cls.notifications = [n for n in cls.notifications if n['id'] != id]
        cls.save()
        logging.info(f"[USR_NOTIFICATIONS] Removed notification with id: {id}")
        return True

    @classmethod
    def clear(cls):
        """
        Clear all notifications.
        """
        cls.notifications = []
        cls.save()
        logging.info("[USR_NOTIFICATIONS] Cleared all notifications")
        return True

    @classmethod
    def get_all(cls):
        """
        Get all notifications.
        Returns:
            list: A list of notifications.
        """
        return cls.notifications

    @classmethod
    def get_by_id(cls, id: str):
        """
        Get a notification by its id.
        Args:
            id (str): The id of the notification to get.
        Returns:
            dict: The notification with the given id.
        """
        for n in cls.notifications:
            if n['id'] == id:
                return n
        return None

# -------------------------------------------------------------------------------------------------

# Initial load of the configuration
load_config()

# Configure logging
configure_logging()

# Initialize user notifications
UserNotifications()