from datetime import datetime, time
from zoneinfo import ZoneInfo
import gettext
import configparser
from configupdater import ConfigUpdater
import logging
import os

# Path to configuration and locales
CONFIGFILE = 'config.ini'
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
        "simulate_kittyflap": "false",
        "mouse_threshold": "70.0",
        "elements_per_page": "20",
        "loglevel": "INFO",
    }
}

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
        "SIMULATE_KITTYFLAP": parser.get('Settings', 'simulate_kittyflap', fallback=DEFAULT_CONFIG['Settings']['simulate_kittyflap']),
        "MOUSE_THRESHOLD": float(parser.get('Settings', 'mouse_threshold', fallback=DEFAULT_CONFIG['Settings']['mouse_threshold'])),
        "ELEMENTS_PER_PAGE": int(parser.get('Settings', 'elements_per_page', fallback=DEFAULT_CONFIG['Settings']['elements_per_page'])),
        "LOGLEVEL": parser.get('Settings', 'loglevel', fallback=DEFAULT_CONFIG['Settings']['loglevel']),
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
    settings['language'] = CONFIG['LANGUAGE']
    settings['timezone'] = CONFIG['TIMEZONE']
    settings['date_format'] = CONFIG['DATE_FORMAT']
    settings['mouse_threshold'] = str(CONFIG['MOUSE_THRESHOLD'])
    settings['elements_per_page'] = str(CONFIG['ELEMENTS_PER_PAGE'])
    settings['loglevel'] = CONFIG['LOGLEVEL']

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