import os
import gettext
import configparser
import logging
import sys
# If the systemd journal Python bindings are available, use them.
try:
    from systemd.journal import JournalHandler  # type: ignore
except Exception:
    JournalHandler = None

from enum import Enum
import uuid
import json
from configupdater import ConfigUpdater
from typing import Any, Callable, List, Tuple

from src.mode import is_remote_mode
from src.paths import kittyhack_root

###### ENUM DEFINITIONS ######
class AllowedToEnter(Enum):
    ALL = 'all'
    ALL_RFIDS = 'all_rfids'
    KNOWN = 'known'
    NONE = 'none'
    CONFIGURE_PER_CAT = 'configure_per_cat'

class AllowedToExit(Enum):
    ALLOW = 'allow'
    DENY = 'deny'
    CONFIGURE_PER_CAT = 'configure_per_cat'

###### CONSTANT DEFINITIONS ######

# Files
CONFIGFILE = 'config.ini'
REMOTE_CONFIGFILE = 'config.remote.ini'

# Remote-mode settings must be local to the remote device (sync overwrites config.ini).
# We keep them in a separate overlay file that is loaded only in remote-mode.
_REMOTE_ONLY_SETTINGS: dict[str, tuple[str, str]] = {
    # CONFIG_KEY: (ini_option_name, type)
    "REMOTE_TARGET_HOST": ("remote_target_host", "str"),
    "REMOTE_CONTROL_PORT": ("remote_control_port", "int"),
    "REMOTE_CONTROL_TIMEOUT": ("remote_control_timeout", "float"),
    "REMOTE_SYNC_ON_FIRST_CONNECT": ("remote_sync_on_first_connect", "bool"),
    "REMOTE_INFERENCE_MAX_FPS": ("remote_inference_max_fps", "float"),
}


def _remote_configfile_path() -> str:
    # Allow override primarily for testing.
    override = os.environ.get("KITTYHACK_REMOTE_CONFIGFILE")
    if override:
        return override
    return os.path.join(kittyhack_root(), REMOTE_CONFIGFILE)


def _write_remote_overrides_from_config(path: str) -> None:
    parser = configparser.ConfigParser()
    parser["Settings"] = {}
    for cfg_key, (opt, _t) in _REMOTE_ONLY_SETTINGS.items():
        # Values in configparser need to be strings
        parser["Settings"][opt] = str(CONFIG.get(cfg_key, ""))
    with open(path, "w", encoding="utf-8") as f:
        parser.write(f)


def _apply_remote_overrides() -> None:
    """Apply remote-only settings from config.remote.ini.

    In remote-mode we want REMOTE_* parameters to survive sync operations,
    therefore they are loaded from a local-only overlay file.
    """
    path = _remote_configfile_path()
    if not os.path.exists(path):
        try:
            _write_remote_overrides_from_config(path)
            logging.info(f"[CONFIG] Created remote override file: {path}")
        except Exception as e:
            logging.warning(f"[CONFIG] Failed to create remote override file '{path}': {e}")
        return

    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except Exception as e:
        logging.warning(f"[CONFIG] Failed to read remote override file '{path}': {e}")
        return

    if not parser.has_section("Settings"):
        return

    for cfg_key, (opt, t) in _REMOTE_ONLY_SETTINGS.items():
        if not parser.has_option("Settings", opt):
            continue
        try:
            if t == "int":
                CONFIG[cfg_key] = parser.getint("Settings", opt)
            elif t == "float":
                CONFIG[cfg_key] = parser.getfloat("Settings", opt)
            elif t == "bool":
                CONFIG[cfg_key] = parser.getboolean("Settings", opt)
            else:
                CONFIG[cfg_key] = parser.get("Settings", opt)
        except Exception:
            # Keep the value from config.ini/defaults if override parsing fails.
            logging.warning(f"[CONFIG] Invalid remote override '{opt}' in {path}; keeping current value")


def read_remote_config_values() -> dict:
    defaults = {
        "remote_target_host": (CONFIG.get("REMOTE_TARGET_HOST") or "").strip(),
        "remote_control_port": int(CONFIG.get("REMOTE_CONTROL_PORT", 8888) or 8888),
        "remote_control_timeout": float(CONFIG.get("REMOTE_CONTROL_TIMEOUT", 30.0) or 30.0),
        "remote_sync_on_first_connect": bool(CONFIG.get("REMOTE_SYNC_ON_FIRST_CONNECT", True)),
    }
    remote_cfg_path = _remote_configfile_path()
    if not os.path.exists(remote_cfg_path):
        return defaults

    parser = configparser.ConfigParser()
    try:
        parser.read(remote_cfg_path)
        if "Settings" not in parser:
            return defaults
        section = parser["Settings"]
        return {
            "remote_target_host": (section.get("remote_target_host", defaults["remote_target_host"]) or "").strip(),
            "remote_control_port": section.getint("remote_control_port", fallback=defaults["remote_control_port"]),
            "remote_control_timeout": section.getfloat("remote_control_timeout", fallback=defaults["remote_control_timeout"]),
            "remote_sync_on_first_connect": section.getboolean(
                "remote_sync_on_first_connect", fallback=defaults["remote_sync_on_first_connect"]
            ),
        }
    except Exception as e:
        logging.warning(f"Failed to read config.remote.ini: {e}")
        return defaults


def remote_setup_required() -> bool:
    if not is_remote_mode():
        return False
    remote_cfg_path = _remote_configfile_path()
    if not os.path.exists(remote_cfg_path):
        return True
    parser = configparser.ConfigParser()
    try:
        parser.read(remote_cfg_path)
    except Exception:
        return True
    if "Settings" not in parser:
        return True
    section = parser["Settings"]
    required_keys = [
        "remote_target_host",
        "remote_control_port",
        "remote_control_timeout",
        "remote_sync_on_first_connect",
    ]
    for key in required_keys:
        if key not in section:
            return True
    if not (section.get("remote_target_host", "") or "").strip():
        return True
    return False

# Gettext constants
LOCALE_DIR = "locales"
DOMAIN = "messages"

# Global dictionary to store configuration settings
CONFIG = {}

# True if config.ini had to be created or recreated during this process startup.
CONFIG_CREATED_AT_STARTUP = False

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
        "min_seconds_to_analyze": 1.5,
        "show_images_with_overlay": True,
        "live_view_refresh_interval": 5.0,
        "kittyflap_config_migrated": False,
        "allowed_to_exit": "allow",
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
        "not_graceful_shutdowns": 0,
        "use_camera_for_cat_detection": False,
        "cat_threshold": 70.0,
        "use_camera_for_motion_detection": False,
        "camera_source": "internal", # can be "internal" or "ip_camera"
        "ip_camera_url": "",
        "enable_ip_camera_decode_scale_pipeline": False,
        "ip_camera_target_resolution": "640x360",
        "ip_camera_pipeline_fps_limit": 10,
        "mqtt_device_id": "",
        "mqtt_broker_address": "",
        "mqtt_broker_port": 1883,
        "mqtt_username": None,
        "mqtt_password": None,
        "mqtt_enabled": False,
        "mqtt_image_publish_interval": 5.0,
        "show_cats_only": False,
        "show_mice_only": False,
        "restart_ip_camera_stream_on_failure": True,
        "wlan_watchdog_enabled": True,
        "disable_rfid_reader": False,
        "event_images_fs_migrated": False,

        # Remote control / remote-mode
        "remote_target_host": "",
        "remote_control_port": 8888,
        "remote_control_timeout": 30.0,
        # Target-mode boot behavior: if remote control was used once, the target may wait for a remote reconnect.
        "remote_wait_after_reboot_timeout": 30.0,
        "remote_sync_on_first_connect": True,
        "remote_inference_max_fps": 10.0,
    }
}

# Keys that contain sensitive information (passwords, credentials, etc.) which should not be logged
SENSITIVE_CONFIG_KEYS = {
    'MQTT_PASSWORD',
    'MQTT_USERNAME',
    'EMAIL',
    'IP_CAMERA_URL'  # May contain embedded credentials
}

# Legacy config defaults (kept for migration only; not written to new configs)
_LEGACY_MIN_PICTURES_TO_ANALYZE_DEFAULT = 5
_LEGACY_INFERENCE_FPS_FOR_MIGRATION = 3.33

def load_config():
    """
    Loads the configuration file and populates the CONFIG dictionary.
    Invalid / corrupt values are replaced by their defaults and a user notification is added.
    Missing keys are silently defaulted (not treated as invalid).
    Invalid keys are removed from config.ini.
    """
    global CONFIG, CONFIG_CREATED_AT_STARTUP
    if not os.path.exists(CONFIGFILE):
        print(f"Configuration file '{CONFIGFILE}' not found. Creating with default values...")
        create_default_config()
        CONFIG_CREATED_AT_STARTUP = True
    
    parser = configparser.ConfigParser()
    parser.read(CONFIGFILE)

    # Detect empty or corrupt config and recreate it
    try:
        is_empty = os.path.exists(CONFIGFILE) and os.path.getsize(CONFIGFILE) == 0
    except Exception:
        is_empty = False
    if is_empty or not parser.has_section('Settings'):
        logging.warning("[CONFIG] Config file exists but is empty/corrupt (no [Settings]). Recreating fresh config.ini")
        try:
            os.remove(CONFIGFILE)
        except Exception as e:
            logging.warning(f"[CONFIG] Failed to remove corrupt config.ini: {e}")
        create_default_config()
        CONFIG_CREATED_AT_STARTUP = True
        parser = configparser.ConfigParser()
        parser.read(CONFIGFILE)

    invalid_values: List[Tuple[str, Any]] = []

    def get_raw(section: str, option: str, default: Any) -> Any:
        if not parser.has_option(section, option):
            return default
        return parser.get(section, option)

    def record_invalid(key: str, raw: Any):
        invalid_values.append((key, raw))

    def safe_get(getter: Callable[[], Any], key: str, default: Any) -> Any:
        try:
            return getter()
        except Exception:
            # Only mark invalid if the key exists (conversion/parsing failed). Missing key => use default silently.
            if parser.has_option('Settings', key.lower()):
                try:
                    raw = parser.get('Settings', key.lower())
                except Exception:
                    raw = ""
                record_invalid(key, raw)
            return default

    def safe_int(key: str, default: int) -> int:
        return safe_get(lambda: parser.getint('Settings', key.lower()), key, default)

    def safe_float(key: str, default: float) -> float:
        return safe_get(lambda: parser.getfloat('Settings', key.lower()), key, default)

    def safe_bool(key: str, default: bool) -> bool:
        return safe_get(lambda: parser.getboolean('Settings', key.lower()), key, default)

    def safe_str(key: str, default: str) -> str:
        return safe_get(lambda: parser.get('Settings', key.lower()), key, default)

    def safe_enum(key: str, enum_cls, default_member):
        raw = get_raw('Settings', key.lower(), default_member.value)
        try:
            return enum_cls(raw)
        except Exception:
            if parser.has_option('Settings', key.lower()):
                record_invalid(key, raw)
            return default_member

    def safe_allowed_to_exit(default_member):
        raw = get_raw('Settings', 'allowed_to_exit', default_member.value)
        low = str(raw).strip().lower()

        # Accept legacy Python-like enum strings: "AllowedToExit.ALLOW"
        if low.startswith("allowedtoexit."):
            # keep only the part after the dot
            low = low.split(".", 1)[1]

        try:
            # Accept boolean-like values
            if low in {"true", "on", "1", "yes"}:
                return AllowedToExit.ALLOW
            if low in {"false", "off", "0", "no"}:
                return AllowedToExit.DENY
            # Accept direct enum value tokens
            return AllowedToExit(low)
        except Exception:
            if parser.has_option('Settings', 'allowed_to_exit'):
                record_invalid('ALLOWED_TO_EXIT', raw)
            return default_member

    d = DEFAULT_CONFIG['Settings']

    # --- Migration: MIN_PICTURES_TO_ANALYZE -> MIN_SECONDS_TO_ANALYZE ---
    # Old configs may contain `min_pictures_to_analyze`. New configs use `min_seconds_to_analyze`.
    # If the new key is missing, derive it via: seconds = pictures / 3.33 (avg legacy inference FPS).
    # Also remove the legacy key to avoid user confusion.
    try:
        has_legacy = parser.has_option('Settings', 'min_pictures_to_analyze')
        has_new = parser.has_option('Settings', 'min_seconds_to_analyze')

        if has_legacy and not has_new:
            try:
                legacy_pics = float(parser.get('Settings', 'min_pictures_to_analyze'))
            except Exception:
                legacy_pics = float(_LEGACY_MIN_PICTURES_TO_ANALYZE_DEFAULT)
            migrated_seconds = round(float(legacy_pics) / float(_LEGACY_INFERENCE_FPS_FOR_MIGRATION), 1)
            # Keep it sane; avoid 0.0 which would effectively skip analysis.
            migrated_seconds = max(0.1, migrated_seconds)

            try:
                updater = ConfigUpdater()
                updater.read(CONFIGFILE)
                if 'Settings' not in updater:
                    updater.add_section('Settings')
                settings_section = updater['Settings']
                settings_section['min_seconds_to_analyze'] = f"{migrated_seconds:.1f}"
                if 'min_pictures_to_analyze' in settings_section:
                    del settings_section['min_pictures_to_analyze']
                with open(CONFIGFILE, 'w', encoding='utf-8') as f:
                    updater.write(f)
                logging.info(
                    f"[CONFIG] Migrated min_pictures_to_analyze={legacy_pics} -> min_seconds_to_analyze={migrated_seconds:.1f}"
                )
            except Exception as e:
                logging.warning(f"[CONFIG] Failed to migrate min_seconds_to_analyze: {e}")

            # Reload parser so safe_* picks up the migrated value.
            parser = configparser.ConfigParser()
            parser.read(CONFIGFILE)

        elif has_legacy and has_new:
            # Best-effort cleanup: remove legacy key once new key exists.
            try:
                updater = ConfigUpdater()
                updater.read(CONFIGFILE)
                if 'Settings' in updater and 'min_pictures_to_analyze' in updater['Settings']:
                    del updater['Settings']['min_pictures_to_analyze']
                    with open(CONFIGFILE, 'w', encoding='utf-8') as f:
                        updater.write(f)
                    logging.info("[CONFIG] Removed legacy key min_pictures_to_analyze from config.ini")
            except Exception:
                pass
    except Exception:
        # Migration must never break startup.
        pass

    new_config = {
        "TIMEZONE": safe_str("TIMEZONE", d['timezone']),
        "LANGUAGE": safe_str("LANGUAGE", d['language']),
        "DATE_FORMAT": safe_str("DATE_FORMAT", d['date_format']),
        "DATABASE_PATH": safe_str("DATABASE_PATH", d['database_path']),
        "KITTYHACK_DATABASE_PATH": safe_str("KITTYHACK_DATABASE_PATH", d['kittyhack_database_path']),
        "MAX_PHOTOS_COUNT": safe_int("MAX_PHOTOS_COUNT", int(d['max_photos_count'])),
        "SIMULATE_KITTYFLAP": safe_bool("SIMULATE_KITTYFLAP", d['simulate_kittyflap']),
        "MOUSE_THRESHOLD": safe_float("MOUSE_THRESHOLD", float(d['mouse_threshold'])),
        "NO_MOUSE_THRESHOLD": safe_float("NO_MOUSE_THRESHOLD", float(d['no_mouse_threshold'])),
        "MIN_THRESHOLD": safe_float("MIN_THRESHOLD", float(d['min_threshold'])),
        "ELEMENTS_PER_PAGE": safe_int("ELEMENTS_PER_PAGE", int(d['elements_per_page'])),
        "LOGLEVEL": safe_str("LOGLEVEL", d['loglevel']),
        "PERIODIC_JOBS_INTERVAL": safe_int("PERIODIC_JOBS_INTERVAL", int(d['periodic_jobs_interval'])),
        "ALLOWED_TO_ENTER": safe_enum("ALLOWED_TO_ENTER", AllowedToEnter, AllowedToEnter(d['allowed_to_enter'])),
        "MOUSE_CHECK_ENABLED": safe_bool("MOUSE_CHECK_ENABLED", d['mouse_check_enabled']),
        "MIN_SECONDS_TO_ANALYZE": safe_float("MIN_SECONDS_TO_ANALYZE", float(d['min_seconds_to_analyze'])),
        "SHOW_IMAGES_WITH_OVERLAY": safe_bool("SHOW_IMAGES_WITH_OVERLAY", d['show_images_with_overlay']),
        "LIVE_VIEW_REFRESH_INTERVAL": safe_float("LIVE_VIEW_REFRESH_INTERVAL", float(d['live_view_refresh_interval'])),
        "KITTYFLAP_CONFIG_MIGRATED": safe_bool("KITTYFLAP_CONFIG_MIGRATED", d['kittyflap_config_migrated']),
        "ALLOWED_TO_EXIT": safe_allowed_to_exit(AllowedToExit(d['allowed_to_exit'])),
        "LAST_VACUUM_DATE": safe_str("LAST_VACUUM_DATE", d['last_vacuum_date']),
        "PERIODIC_VERSION_CHECK": safe_bool("PERIODIC_VERSION_CHECK", d['periodic_version_check']),
        "KITTYFLAP_DB_NAGSCREEN": safe_bool("KITTYFLAP_DB_NAGSCREEN", d['kittyflap_db_nagscreen']),
        "LATEST_VERSION": "unknown",
        "LAST_DB_BACKUP_DATE": safe_str("LAST_DB_BACKUP_DATE", d['last_db_backup_date']),
        "KITTYHACK_DATABASE_BACKUP_PATH": safe_str("KITTYHACK_DATABASE_BACKUP_PATH", d['kittyhack_database_backup_path']),
        "PIR_OUTSIDE_THRESHOLD": safe_float("PIR_OUTSIDE_THRESHOLD", float(d['pir_outside_threshold'])),
        "PIR_INSIDE_THRESHOLD": safe_float("PIR_INSIDE_THRESHOLD", float(d['pir_inside_threshold'])),
        "WLAN_TX_POWER": safe_int("WLAN_TX_POWER", int(d['wlan_tx_power'])),
        "GROUP_PICTURES_TO_EVENTS": safe_bool("GROUP_PICTURES_TO_EVENTS", d['group_pictures_to_events']),
        "TFLITE_MODEL_VERSION": safe_str("TFLITE_MODEL_VERSION", d['tflite_model_version']),
        "LOCK_DURATION_AFTER_PREY_DETECTION": safe_int("LOCK_DURATION_AFTER_PREY_DETECTION", int(d['lock_duration_after_prey_detection'])),
        "MAX_PICTURES_PER_EVENT_WITH_RFID": safe_int("MAX_PICTURES_PER_EVENT_WITH_RFID", int(d['max_pictures_per_event_with_rfid'])),
        "MAX_PICTURES_PER_EVENT_WITHOUT_RFID": safe_int("MAX_PICTURES_PER_EVENT_WITHOUT_RFID", int(d['max_pictures_per_event_without_rfid'])),
        "USE_ALL_CORES_FOR_IMAGE_PROCESSING": safe_bool("USE_ALL_CORES_FOR_IMAGE_PROCESSING", d['use_all_cores_for_image_processing']),
        "LAST_BOOTED_VERSION": safe_str("LAST_BOOTED_VERSION", d['last_booted_version']),
        "ALLOWED_TO_EXIT_RANGE1": safe_bool("ALLOWED_TO_EXIT_RANGE1", d['allowed_to_exit_range1']),
        "ALLOWED_TO_EXIT_RANGE1_FROM": safe_str("ALLOWED_TO_EXIT_RANGE1_FROM", d['allowed_to_exit_range1_from']),
        "ALLOWED_TO_EXIT_RANGE1_TO": safe_str("ALLOWED_TO_EXIT_RANGE1_TO", d['allowed_to_exit_range1_to']),
        "ALLOWED_TO_EXIT_RANGE2": safe_bool("ALLOWED_TO_EXIT_RANGE2", d['allowed_to_exit_range2']),
        "ALLOWED_TO_EXIT_RANGE2_FROM": safe_str("ALLOWED_TO_EXIT_RANGE2_FROM", d['allowed_to_exit_range2_from']),
        "ALLOWED_TO_EXIT_RANGE2_TO": safe_str("ALLOWED_TO_EXIT_RANGE2_TO", d['allowed_to_exit_range2_to']),
        "ALLOWED_TO_EXIT_RANGE3": safe_bool("ALLOWED_TO_EXIT_RANGE3", d['allowed_to_exit_range3']),
        "ALLOWED_TO_EXIT_RANGE3_FROM": safe_str("ALLOWED_TO_EXIT_RANGE3_FROM", d['allowed_to_exit_range3_from']),
        "ALLOWED_TO_EXIT_RANGE3_TO": safe_str("ALLOWED_TO_EXIT_RANGE3_TO", d['allowed_to_exit_range3_to']),
        "LABELSTUDIO_VERSION": safe_str("LABELSTUDIO_VERSION", d['labelstudio_version']),
        "EMAIL": safe_str("EMAIL", d['email']),
        "USER_NAME": safe_str("USER_NAME", d['user_name']),
        "MODEL_TRAINING": safe_str("MODEL_TRAINING", d['model_training']),
        "YOLO_MODEL": safe_str("YOLO_MODEL", d['yolo_model']),
        "STARTUP_SHUTDOWN_FLAG": safe_bool("STARTUP_SHUTDOWN_FLAG", d['startup_shutdown_flag']),
        "NOT_GRACEFUL_SHUTDOWNS": safe_int("NOT_GRACEFUL_SHUTDOWNS", int(d['not_graceful_shutdowns'])),
        "USE_CAMERA_FOR_CAT_DETECTION": safe_bool("USE_CAMERA_FOR_CAT_DETECTION", d['use_camera_for_cat_detection']),
        "CAT_THRESHOLD": safe_float("CAT_THRESHOLD", float(d['cat_threshold'])),
        "USE_CAMERA_FOR_MOTION_DETECTION": safe_bool("USE_CAMERA_FOR_MOTION_DETECTION", d.get('use_camera_for_motion_detection', False)),
        "CAMERA_SOURCE": safe_str("CAMERA_SOURCE", d['camera_source']),
        "IP_CAMERA_URL": safe_str("IP_CAMERA_URL", d.get('ip_camera_url', "")),
        "ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE": safe_bool(
            "ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE",
            d.get('enable_ip_camera_decode_scale_pipeline', False),
        ),
        "IP_CAMERA_TARGET_RESOLUTION": safe_str(
            "IP_CAMERA_TARGET_RESOLUTION",
            d.get('ip_camera_target_resolution', "640x360"),
        ),
        "IP_CAMERA_PIPELINE_FPS_LIMIT": safe_int(
            "IP_CAMERA_PIPELINE_FPS_LIMIT",
            int(d.get('ip_camera_pipeline_fps_limit', 10)),
        ),
        "MQTT_DEVICE_ID": safe_str("MQTT_DEVICE_ID", d['mqtt_device_id']),
        "MQTT_BROKER_ADDRESS": safe_str("MQTT_BROKER_ADDRESS", d['mqtt_broker_address']),
        "MQTT_BROKER_PORT": safe_int("MQTT_BROKER_PORT", int(d['mqtt_broker_port'])),
        "MQTT_USERNAME": safe_str("MQTT_USERNAME", d['mqtt_username'] if d['mqtt_username'] is not None else ""),
        "MQTT_PASSWORD": safe_str("MQTT_PASSWORD", d['mqtt_password'] if d['mqtt_password'] is not None else ""),
        "MQTT_ENABLED": safe_bool("MQTT_ENABLED", d.get('mqtt_enabled', False)),
        "MQTT_IMAGE_PUBLISH_INTERVAL": safe_float("MQTT_IMAGE_PUBLISH_INTERVAL", float(d['mqtt_image_publish_interval'])),
        "SHOW_CATS_ONLY": safe_bool("SHOW_CATS_ONLY", d['show_cats_only']),
        "SHOW_MICE_ONLY": safe_bool("SHOW_MICE_ONLY", d['show_mice_only']),
        "RESTART_IP_CAMERA_STREAM_ON_FAILURE": safe_bool("RESTART_IP_CAMERA_STREAM_ON_FAILURE", d.get('restart_ip_camera_stream_on_failure', True)),
        "WLAN_WATCHDOG_ENABLED": safe_bool("WLAN_WATCHDOG_ENABLED", d.get('wlan_watchdog_enabled', True)),
        "DISABLE_RFID_READER": safe_bool("DISABLE_RFID_READER", d.get('disable_rfid_reader', False)),
        "EVENT_IMAGES_FS_MIGRATED": safe_bool("EVENT_IMAGES_FS_MIGRATED", d.get('event_images_fs_migrated', False)),

        # Remote control / remote-mode
        "REMOTE_TARGET_HOST": safe_str("REMOTE_TARGET_HOST", d.get('remote_target_host', "")),
        "REMOTE_CONTROL_PORT": safe_int("REMOTE_CONTROL_PORT", int(d.get('remote_control_port', 8888))),
        "REMOTE_CONTROL_TIMEOUT": safe_float("REMOTE_CONTROL_TIMEOUT", float(d.get('remote_control_timeout', 30.0))),
        "REMOTE_WAIT_AFTER_REBOOT_TIMEOUT": safe_float(
            "REMOTE_WAIT_AFTER_REBOOT_TIMEOUT",
            float(d.get('remote_wait_after_reboot_timeout', 30.0)),
        ),
        "REMOTE_SYNC_ON_FIRST_CONNECT": safe_bool("REMOTE_SYNC_ON_FIRST_CONNECT", d.get('remote_sync_on_first_connect', True)),
        "REMOTE_INFERENCE_MAX_FPS": safe_float("REMOTE_INFERENCE_MAX_FPS", float(d.get('remote_inference_max_fps', 10.0))),
    }

    # Update in-place so imported CONFIG references in other modules stay valid.
    CONFIG.clear()
    CONFIG.update(new_config)

    if invalid_values:
        # Remove invalid keys from config.ini before notifying user
        try:
            updater = ConfigUpdater()
            updater.read(CONFIGFILE)
            settings_section = updater['Settings']
            for k, _ in invalid_values:
                opt = k.lower()
                if opt in settings_section:
                    del settings_section[opt]
            with open(CONFIGFILE, 'w') as f:
                updater.write(f)
            logging.info("[CONFIG] Removed invalid keys from config.ini")
        except Exception as e:
            logging.warning(f"[CONFIG] Failed to remove invalid keys from config.ini: {e}")

        lines = []
        for k, raw in invalid_values:
            masked = "********" if k in SENSITIVE_CONFIG_KEYS else raw
            lines.append(f"- {k}: '{masked}'")
            logging.warning(f"[CONFIG] Invalid value for {k} -> '{raw}'. Using default.")
        if CONFIG.get('LANGUAGE') == 'de':
            msg = (
                "Die folgenden Konfigurationsschlüssel hatten ungültige Werte und wurden auf ihre Standardwerte zurückgesetzt:\n"
                + "\n".join(lines)
                + "\n\nBitte prüfe deine Einstellungen im Abschnitt KONFIGURATION."
            )
            header = "Ungültige Konfiguration erkannt"
        else:
            msg = (
                "The following configuration keys contained invalid values and have been reset to their default values:\n"
                + "\n".join(lines)
                + "\n\nPlease check your settings in the CONFIGURATION section."
            )
            header = "Invalid configuration detected"
        try:
            UserNotifications.add(
                header=header,
                message=msg,
                type="warning",
                id="config_invalid_values",
                skip_if_id_exists=True
            )
        except Exception:
            logging.warning("[CONFIG] Could not add user notification for invalid values.")

    # Remote-mode: load remote-only overrides from a local overlay file.
    # This ensures sync operations (which overwrite config.ini) cannot wipe these settings.
    if is_remote_mode():
        _apply_remote_overrides()

def save_config():
    """
    Saves the configuration file.
    Requires the CONFIG
    """
    # prepare the updated values for the configfile
    updater = ConfigUpdater()
    updater.read(CONFIGFILE)

    # Ensure [Settings] section exists
    if 'Settings' not in updater:
        updater.add_section('Settings')

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
    # Persist with 1 decimal precision
    try:
        settings['min_seconds_to_analyze'] = f"{float(CONFIG['MIN_SECONDS_TO_ANALYZE']):.1f}"
    except Exception:
        settings['min_seconds_to_analyze'] = str(CONFIG['MIN_SECONDS_TO_ANALYZE'])
    settings['show_images_with_overlay'] = CONFIG['SHOW_IMAGES_WITH_OVERLAY']
    settings['live_view_refresh_interval'] = CONFIG['LIVE_VIEW_REFRESH_INTERVAL']
    settings['kittyflap_config_migrated'] = CONFIG['KITTYFLAP_CONFIG_MIGRATED']
    settings['allowed_to_exit'] = CONFIG['ALLOWED_TO_EXIT'].value
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
    settings['remote_wait_after_reboot_timeout'] = CONFIG.get('REMOTE_WAIT_AFTER_REBOOT_TIMEOUT', 30.0)
    #settings['labelstudio_version'] = CONFIG['LABELSTUDIO_VERSION'] # This value may not be written to the config file
    settings['email'] = CONFIG['EMAIL']
    settings['user_name'] = CONFIG['USER_NAME']
    settings['model_training'] = CONFIG['MODEL_TRAINING']
    settings['yolo_model'] = CONFIG['YOLO_MODEL']
    settings['startup_shutdown_flag'] = CONFIG['STARTUP_SHUTDOWN_FLAG']
    settings['not_graceful_shutdowns'] = CONFIG['NOT_GRACEFUL_SHUTDOWNS']
    settings['use_camera_for_cat_detection'] = CONFIG['USE_CAMERA_FOR_CAT_DETECTION']
    settings['cat_threshold'] = CONFIG['CAT_THRESHOLD']
    settings['use_camera_for_motion_detection'] = CONFIG['USE_CAMERA_FOR_MOTION_DETECTION']
    settings['camera_source'] = CONFIG['CAMERA_SOURCE']
    settings['ip_camera_url'] = CONFIG['IP_CAMERA_URL']
    settings['enable_ip_camera_decode_scale_pipeline'] = CONFIG.get('ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE', False)
    settings['ip_camera_target_resolution'] = CONFIG.get('IP_CAMERA_TARGET_RESOLUTION', '640x360')
    settings['ip_camera_pipeline_fps_limit'] = int(CONFIG.get('IP_CAMERA_PIPELINE_FPS_LIMIT', 10) or 10)
    settings['mqtt_device_id'] = CONFIG['MQTT_DEVICE_ID']
    settings['mqtt_broker_address'] = CONFIG['MQTT_BROKER_ADDRESS']
    settings['mqtt_broker_port'] = CONFIG['MQTT_BROKER_PORT']
    settings['mqtt_username'] = CONFIG['MQTT_USERNAME']
    settings['mqtt_password'] = CONFIG['MQTT_PASSWORD']
    settings['mqtt_enabled'] = CONFIG['MQTT_ENABLED']
    settings['mqtt_image_publish_interval'] = CONFIG['MQTT_IMAGE_PUBLISH_INTERVAL']
    settings['show_cats_only'] = CONFIG['SHOW_CATS_ONLY']
    settings['show_mice_only'] = CONFIG['SHOW_MICE_ONLY']
    settings['restart_ip_camera_stream_on_failure'] = CONFIG['RESTART_IP_CAMERA_STREAM_ON_FAILURE']
    settings['wlan_watchdog_enabled'] = CONFIG['WLAN_WATCHDOG_ENABLED']
    settings['disable_rfid_reader'] = CONFIG['DISABLE_RFID_READER']

    # Never persist remote-only settings in config.ini.
    # They are stored in config.remote.ini so they survive sync operations.
    try:
        for _cfg_key, (opt, _t) in _REMOTE_ONLY_SETTINGS.items():
            if opt in settings:
                del settings[opt]
    except Exception:
        pass

    # Write updated configuration back to the file
    try:
        with open(CONFIGFILE, 'w') as configfile:
            updater.write(configfile)
    except:
        logging.error("Failed to update the values in the configfile.")
        return False
    
    logging.info("Updated the values in the configfile")

    # Remote-mode: persist remote-only settings to the local overlay file too.
    # This ensures sync operations (which overwrite config.ini) cannot wipe these settings.
    if is_remote_mode():
        try:
            _write_remote_overrides_from_config(_remote_configfile_path())
        except Exception as e:
            logging.warning(f"[CONFIG] Failed to write remote override file '{_remote_configfile_path()}': {e}")

    return True

def update_config_images_overlay():
    """
    Updates only the SHOW_IMAGES_WITH_OVERLAY setting in the configuration file.
    """
    updater = ConfigUpdater()
    updater.read(CONFIGFILE)

    # Ensure [Settings] section exists
    if 'Settings' not in updater:
        updater.add_section('Settings')

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
    
    # Ensure [Settings] section exists
    if 'Settings' not in updater:
        updater.add_section('Settings')
    
    # Remote-mode: never persist remote-only settings in config.ini.
    if is_remote_mode() and parameter.upper() in _REMOTE_ONLY_SETTINGS:
        try:
            _write_remote_overrides_from_config(_remote_configfile_path())
        except Exception:
            pass
        try:
            # Best-effort cleanup: remove the option from config.ini if present.
            if 'Settings' in updater and parameter.lower() in updater['Settings']:
                del updater['Settings'][parameter.lower()]
                with open(CONFIGFILE, 'w') as configfile:
                    updater.write(configfile)
        except Exception:
            pass
        return

    # Get the value to write
    value = CONFIG[parameter.upper()]
    
    # Special handling for enum values
    if parameter.upper() == 'ALLOWED_TO_ENTER' and isinstance(value, AllowedToEnter):
        value = value.value
    
    updater['Settings'][parameter.lower()] = value

    # Write updated configuration back to the file
    try:
        with open(CONFIGFILE, 'w') as configfile:
            updater.write(configfile)
        logging.info(f"Updated {parameter.upper()} in the configfile to: {value}")
    except Exception as e:
        logging.error(f"Failed to update {parameter.upper()} in the configfile: {e}")

    # Keep remote override file in sync in remote-mode.
    if is_remote_mode() and parameter.upper() in _REMOTE_ONLY_SETTINGS:
        try:
            _write_remote_overrides_from_config(_remote_configfile_path())
        except Exception:
            pass

def create_default_config():
    """
    Creates the configuration file with default values.
    """
    def stringify_dict(d):
        return {k: str(v) if v is not None else "" for k, v in d.items()}

    parser = configparser.ConfigParser()
    # Convert all values in DEFAULT_CONFIG to strings
    config_str = {section: stringify_dict(values) for section, values in DEFAULT_CONFIG.items()}
    parser.read_dict(config_str)
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

    # Prefer systemd journal handler when available, otherwise stream to stdout
    if JournalHandler is not None:
        handler = JournalHandler()
    else:
        handler = logging.StreamHandler(sys.stdout)

    formatter = logging.Formatter('[%(levelname)s] %(message)s')
    handler.setFormatter(formatter)

    # Attach to root logger
    logger = logging.getLogger()
    logger.setLevel(level)
    logger.addHandler(handler)
    logging.info(f"Logger loglevel set to {level_name.upper()} (journal/stdout)")

def get_loggable_config_value(key, value):
    """
    Returns a loggable version of a configuration value.
    Masks sensitive values with asterisks.
    
    Args:
        key (str): The configuration key
        value: The configuration value
        
    Returns:
        A string representation of the value, masked if sensitive
    """
    if key in SENSITIVE_CONFIG_KEYS and value:
        return "********"  # Mask sensitive values
    return value
    
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
configure_logging(CONFIG['LOGLEVEL'])

# Initialize user notifications
UserNotifications()