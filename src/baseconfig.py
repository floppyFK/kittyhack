import os
import gettext
import configparser
import logging
import sys
import tempfile
import threading
from contextlib import contextmanager
# If the systemd journal Python bindings are available, use them.
try:
    from systemd.journal import JournalHandler  # type: ignore
except Exception:
    JournalHandler = None

try:
    import fcntl
except Exception:
    fcntl = None

from enum import Enum
import uuid
import json
from configupdater import ConfigUpdater
from typing import Any, Callable, Iterator, List, Tuple
from dataclasses import dataclass

from src.mode import is_remote_mode
from src.paths import kittyhack_root
from src.locales_runtime import ensure_runtime_locales_ready

###### ENUM DEFINITIONS ######
class AllowedToEnter(Enum):
    """Who may enter through the flap (config / MQTT / API)."""

    ALL = 'all'
    ALL_RFIDS = 'all_rfids'
    KNOWN = 'known'
    NONE = 'none'
    CONFIGURE_PER_CAT = 'configure_per_cat'

class AllowedToExit(Enum):
    """Whether exit is allowed (optionally per-cat or time-ranged in UI)."""

    ALLOW = 'allow'
    DENY = 'deny'
    CONFIGURE_PER_CAT = 'configure_per_cat'

###### CONSTANT DEFINITIONS ######

# Files
CONFIGFILE = 'config.ini'
REMOTE_CONFIGFILE = 'config.remote.ini'

###### CONFIG FILE I/O ######
# kittyhack.service and kittyhack_control.service both write config.ini
# (notably STARTUP_SHUTDOWN_FLAG on reboot). In-place open(..., "w") truncates
# first, so a concurrent reader can persist a near-empty file that silently
# loads as all defaults. Always lock + write via temp file + os.replace.


@contextmanager
def _config_file_lock() -> Iterator[None]:
    """Cross-process exclusive lock for config.ini read-modify-write (fcntl/Linux)."""
    lock_path = f"{CONFIGFILE}.lock"
    lock_dir = os.path.dirname(os.path.abspath(lock_path))
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)
    lock_file = open(lock_path, "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except Exception as e:
                logging.warning(f"[CONFIG] Failed to acquire config.ini lock: {e}")
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            lock_file.close()
        except Exception:
            pass


def _atomic_write_text(path: str, write_fn: Callable[[Any], None]) -> None:
    """Write ``path`` atomically (sibling temp file + ``os.replace``)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".config_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            write_fn(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def _atomic_write_config_updater(updater: ConfigUpdater) -> None:
    """Atomically persist a ConfigUpdater to CONFIGFILE (caller must hold lock)."""
    _atomic_write_text(CONFIGFILE, updater.write)


###### SETTINGS SCHEMA ######
# Single source of truth for defaults, load typing, and save persistence.
# Add a new setting here only — load_config / save_config / update_single pick it up.


@dataclass(frozen=True)
class Setting:
    """One persisted (or runtime) config key: defaults, load kind, and save behavior."""

    key: str  # CONFIG key, e.g. "MOUSE_THRESHOLD" (ini option = key.lower())
    default: Any
    kind: str  # str | int | float | bool | enum | allowed_to_exit
    enum_cls: Any = None
    save_fmt: str | None = None  # e.g. "{:.1f}"
    persist: bool = True  # False: load into CONFIG but never write on save_config
    remote_only: bool = False  # persist only in config.remote.ini (stripped from config.ini)
    save_as_str: bool = False  # force str(...) on save (MOUSE_CHECK_ENABLED)


def _S(key, default, kind="str", **kwargs) -> Setting:
    """Shorthand constructor for a ``Setting`` in ``SETTINGS_SCHEMA``."""
    return Setting(key=key, default=default, kind=kind, **kwargs)


SETTINGS_SCHEMA: list[Setting] = [
    _S("TIMEZONE", "Europe/Berlin"),
    _S("LANGUAGE", "en"),
    _S("DATE_FORMAT", "yyyy-mm-dd"),
    _S("DATABASE_PATH", "../kittyflap.db"),
    _S("KITTYHACK_DATABASE_PATH", "./kittyhack.db"),
    _S("MAX_PHOTOS_COUNT", 6000, "int"),
    _S("MOUSE_THRESHOLD", 70.0, "float"),
    _S("NO_MOUSE_THRESHOLD", 70.0, "float"),
    _S("MIN_THRESHOLD", 30.0, "float"),
    _S("ELEMENTS_PER_PAGE", 20, "int"),
    _S("LOGLEVEL", "INFO"),
    _S("PERIODIC_JOBS_INTERVAL", 900, "int"),
    _S("ALLOWED_TO_ENTER", "all", "enum", enum_cls=AllowedToEnter),
    _S("MOUSE_CHECK_ENABLED", True, "bool", save_as_str=True),
    _S("MIN_SECONDS_TO_ANALYZE", 1.5, "float", save_fmt="{:.1f}"),
    _S("SHOW_IMAGES_WITH_OVERLAY", True, "bool"),
    _S("LIVE_VIEW_REFRESH_INTERVAL", 5.0, "float"),
    _S("KITTYFLAP_CONFIG_MIGRATED", False, "bool"),
    _S("ALLOWED_TO_EXIT", "allow", "allowed_to_exit", enum_cls=AllowedToExit),
    _S("LAST_VACUUM_DATE", ""),
    _S("PERIODIC_VERSION_CHECK", True, "bool"),
    _S("KITTYFLAP_DB_NAGSCREEN", False, "bool"),
    _S("LAST_DB_BACKUP_DATE", ""),
    _S("KITTYHACK_DATABASE_BACKUP_PATH", "../kittyhack_backup.db"),
    _S("PIR_OUTSIDE_THRESHOLD", 0.5, "float"),
    _S("PIR_INSIDE_THRESHOLD", 3.0, "float"),
    _S("IMMEDIATE_LOCK_AFTER_PASSAGE", False, "bool"),
    _S("WLAN_TX_POWER", 7, "int"),
    _S("GROUP_PICTURES_TO_EVENTS", True, "bool"),
    _S("TFLITE_MODEL_VERSION", "original_kittyflap_model_v2"),
    _S("LOCK_DURATION_AFTER_PREY_DETECTION", 300, "int"),
    _S("MAX_PICTURES_PER_EVENT_WITH_RFID", 100, "int"),
    _S("MAX_PICTURES_PER_EVENT_WITHOUT_RFID", 30, "int"),
    _S("USE_ALL_CORES_FOR_IMAGE_PROCESSING", False, "bool"),  # forced True in remote-mode; always False on Kittyflap hardware
    _S("LAST_BOOTED_VERSION", "v1.5.1"),  # Parameter introduced in v1.5.1
    _S("ALLOWED_TO_EXIT_RANGE1", False, "bool"),
    _S("ALLOWED_TO_EXIT_RANGE1_FROM", "00:00"),
    _S("ALLOWED_TO_EXIT_RANGE1_TO", "23:59"),
    _S("ALLOWED_TO_EXIT_RANGE2", False, "bool"),
    _S("ALLOWED_TO_EXIT_RANGE2_FROM", "00:00"),
    _S("ALLOWED_TO_EXIT_RANGE2_TO", "23:59"),
    _S("ALLOWED_TO_EXIT_RANGE3", False, "bool"),
    _S("ALLOWED_TO_EXIT_RANGE3_FROM", "00:00"),
    _S("ALLOWED_TO_EXIT_RANGE3_TO", "23:59"),
    # Not written by save_config (managed separately / migration flags)
    _S("LABELSTUDIO_VERSION", None, persist=False),
    _S("LABELSTUDIO_API_TOKEN", "", persist=False),
    _S("LABELSTUDIO_PROJECT", ""),
    _S("LABELSTUDIO_PROJECT_TITLE", ""),
    _S("EMAIL", ""),
    _S("USER_NAME", ""),
    _S("MODEL_TRAINING", ""),
    _S("YOLO_MODEL", ""),
    _S("INFERENCE_DEVICE", "cpu"),
    _S("STARTUP_SHUTDOWN_FLAG", False, "bool"),
    _S("NOT_GRACEFUL_SHUTDOWNS", 0, "int"),
    _S("USE_CAMERA_FOR_CAT_DETECTION", False, "bool"),
    _S("CAT_THRESHOLD", 70.0, "float"),
    _S("USE_CAMERA_FOR_MOTION_DETECTION", False, "bool"),
    _S("REQUIRE_OUTSIDE_PIR_FOR_CAMERA_ENTRY", False, "bool"),
    _S("CAMERA_SOURCE", "internal"),  # can be "internal" or "ip_camera"
    _S("IP_CAMERA_URL", ""),
    _S("ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE", False, "bool"),
    _S("IP_CAMERA_TARGET_RESOLUTION", "640x360"),
    _S("IP_CAMERA_PIPELINE_FPS_LIMIT", 10, "int"),
    _S("IP_CAMERA_HW_DECODE", "auto"),
    _S("MQTT_DEVICE_ID", ""),
    _S("MQTT_BROKER_ADDRESS", ""),
    _S("MQTT_BROKER_PORT", 1883, "int"),
    _S("MQTT_USERNAME", None),
    _S("MQTT_PASSWORD", None),
    _S("MQTT_ENABLED", False, "bool"),
    _S("MQTT_IMAGE_PUBLISH_INTERVAL", 5.0, "float"),
    _S("SHOW_CATS_ONLY", False, "bool"),
    _S("SHOW_MICE_ONLY", False, "bool"),
    _S("RESTART_IP_CAMERA_STREAM_ON_FAILURE", True, "bool"),
    _S("WLAN_WATCHDOG_ENABLED", True, "bool"),
    _S("DISABLE_RFID_READER", False, "bool"),
    _S("EVENT_IMAGES_FS_MIGRATED", False, "bool", persist=False),
    # Remote control / remote-mode (subset lives only in config.remote.ini)
    _S("REMOTE_TARGET_HOST", "", remote_only=True),
    _S("REMOTE_CONTROL_PORT", 8888, "int", remote_only=True),
    _S("REMOTE_CONTROL_TIMEOUT", 30.0, "float", remote_only=True),
    # Target-mode boot: wait for remote reconnect after remote-triggered reboot
    _S("REMOTE_WAIT_AFTER_REBOOT_TIMEOUT", 30.0, "float"),
    _S("REMOTE_SYNC_ON_FIRST_CONNECT", True, "bool", remote_only=True),
    _S("REMOTE_SYNC_LABELSTUDIO", True, "bool", remote_only=True),
    _S("REMOTE_INFERENCE_MAX_FPS", 10.0, "float", remote_only=True),
    # Update repository: standard | beta | custom (custom uses UPDATE_REPOSITORY)
    _S("UPDATE_REPOSITORY_MODE", "standard"),
    _S("UPDATE_REPOSITORY", ""),
]

_SETTINGS_BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS_SCHEMA}

# Remote-mode overlay map: CONFIG_KEY -> (ini_option, type_name)
_REMOTE_ONLY_SETTINGS = {
    s.key: (s.key.lower(), s.kind if s.kind in {"str", "int", "float", "bool"} else "str")
    for s in SETTINGS_SCHEMA
    if s.remote_only
}

# Default configuration values (generated from schema; UI reads DEFAULT_CONFIG["Settings"][...])
DEFAULT_CONFIG = {
    "Settings": {s.key.lower(): s.default for s in SETTINGS_SCHEMA}
}


def _load_default_for_setting(setting: Setting) -> Any:
    """Typed default used when reading from ini."""
    if setting.kind == "enum" and isinstance(setting.default, str) and setting.enum_cls is not None:
        return setting.enum_cls(setting.default)
    if setting.kind == "allowed_to_exit" and isinstance(setting.default, str) and setting.enum_cls is not None:
        return setting.enum_cls(setting.default)
    if setting.kind == "int":
        return int(setting.default)
    if setting.kind == "float":
        return float(setting.default)
    # Match historical load: MQTT None defaults coerce to ""; LABELSTUDIO_VERSION stays None.
    if setting.key in {"MQTT_USERNAME", "MQTT_PASSWORD"} and setting.default is None:
        return ""
    return setting.default


def _value_for_ini(setting: Setting, value: Any) -> Any:
    """Convert a CONFIG value into something ConfigUpdater can write."""
    if setting.kind in {"enum", "allowed_to_exit"} and hasattr(value, "value"):
        value = value.value
    if setting.save_fmt:
        try:
            return setting.save_fmt.format(float(value))
        except Exception:
            return str(value)
    if setting.save_as_str:
        return str(value)
    if setting.kind == "int":
        try:
            return int(value or 0)
        except Exception:
            return int(setting.default or 0)
    return value


def _remote_configfile_path() -> str:
    """Absolute path to ``config.remote.ini`` (or ``KITTYHACK_REMOTE_CONFIGFILE`` override)."""
    # Allow override primarily for testing.
    override = os.environ.get("KITTYHACK_REMOTE_CONFIGFILE")
    if override:
        return override
    return os.path.join(kittyhack_root(), REMOTE_CONFIGFILE)


def _write_remote_overrides_from_config(path: str) -> None:
    """Write remote-only CONFIG keys into the overlay ini at ``path``."""
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
    """Read remote-setup fields from the overlay file (with CONFIG-based fallbacks)."""
    defaults = {
        "remote_target_host": (CONFIG.get("REMOTE_TARGET_HOST") or "").strip(),
        "remote_control_port": int(CONFIG.get("REMOTE_CONTROL_PORT", 8888) or 8888),
        "remote_control_timeout": float(CONFIG.get("REMOTE_CONTROL_TIMEOUT", 30.0) or 30.0),
        "remote_sync_on_first_connect": bool(CONFIG.get("REMOTE_SYNC_ON_FIRST_CONNECT", True)),
        "remote_sync_labelstudio": bool(CONFIG.get("REMOTE_SYNC_LABELSTUDIO", True)),
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
            "remote_sync_labelstudio": section.getboolean(
                "remote_sync_labelstudio", fallback=defaults["remote_sync_labelstudio"]
            ),
        }
    except Exception as e:
        logging.warning(f"Failed to read config.remote.ini: {e}")
        return defaults


def remote_setup_required() -> bool:
    """True in remote mode when host/port/timeout/sync keys are missing or host is empty."""
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

# Keys that contain sensitive information (passwords, credentials, etc.) which should not be logged
SENSITIVE_CONFIG_KEYS = {
    'MQTT_PASSWORD',
    'MQTT_USERNAME',
    'EMAIL',
    'IP_CAMERA_URL',  # May contain embedded credentials
    'LABELSTUDIO_API_TOKEN'
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

    # Legacy: simulate_kittyflap was a persisted config key; simulation is now a process flag.
    if parser.has_option('Settings', 'simulate_kittyflap'):
        logging.warning(
            "[CONFIG] simulate_kittyflap in config.ini is ignored; "
            "use env KITTYHACK_SIMULATE=1 or kittyhack_control --simulate"
        )
        try:
            with _config_file_lock():
                updater = ConfigUpdater()
                updater.read(CONFIGFILE)
                if 'Settings' in updater and 'simulate_kittyflap' in updater['Settings']:
                    del updater['Settings']['simulate_kittyflap']
                    _atomic_write_config_updater(updater)
                    logging.info("[CONFIG] Removed obsolete simulate_kittyflap from config.ini")
        except Exception as e:
            logging.warning(f"[CONFIG] Failed to remove simulate_kittyflap from config.ini: {e}")

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
                with _config_file_lock():
                    updater = ConfigUpdater()
                    updater.read(CONFIGFILE)
                    if 'Settings' not in updater:
                        updater.add_section('Settings')
                    settings_section = updater['Settings']
                    settings_section['min_seconds_to_analyze'] = f"{migrated_seconds:.1f}"
                    if 'min_pictures_to_analyze' in settings_section:
                        del settings_section['min_pictures_to_analyze']
                    _atomic_write_config_updater(updater)
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
                with _config_file_lock():
                    updater = ConfigUpdater()
                    updater.read(CONFIGFILE)
                    if 'Settings' in updater and 'min_pictures_to_analyze' in updater['Settings']:
                        del updater['Settings']['min_pictures_to_analyze']
                        _atomic_write_config_updater(updater)
                        logging.info("[CONFIG] Removed legacy key min_pictures_to_analyze from config.ini")
            except Exception:
                pass
    except Exception:
        # Migration must never break startup.
        pass

    new_config: dict[str, Any] = {}
    for setting in SETTINGS_SCHEMA:
        default = _load_default_for_setting(setting)
        if setting.kind == "int":
            new_config[setting.key] = safe_int(setting.key, default)
        elif setting.kind == "float":
            new_config[setting.key] = safe_float(setting.key, default)
        elif setting.kind == "bool":
            new_config[setting.key] = safe_bool(setting.key, default)
        elif setting.kind == "enum":
            new_config[setting.key] = safe_enum(setting.key, setting.enum_cls, default)
        elif setting.kind == "allowed_to_exit":
            new_config[setting.key] = safe_allowed_to_exit(default)
        else:
            new_config[setting.key] = safe_str(setting.key, default)

    # Runtime-only (not persisted)
    new_config["LATEST_VERSION"] = "unknown"

    # Update in-place so imported CONFIG references in other modules stay valid.
    CONFIG.clear()
    CONFIG.update(new_config)

    if invalid_values:
        # Remove invalid keys from config.ini before notifying user
        try:
            with _config_file_lock():
                updater = ConfigUpdater()
                updater.read(CONFIGFILE)
                settings_section = updater['Settings']
                for k, _ in invalid_values:
                    opt = k.lower()
                    if opt in settings_section:
                        del settings_section[opt]
                _atomic_write_config_updater(updater)
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
    with _config_file_lock():
        # prepare the updated values for the configfile
        updater = ConfigUpdater()
        updater.read(CONFIGFILE)

        # Ensure [Settings] section exists
        if 'Settings' not in updater:
            updater.add_section('Settings')

        settings = updater['Settings']
        for setting in SETTINGS_SCHEMA:
            if not setting.persist or setting.remote_only:
                continue
            value = CONFIG.get(setting.key, setting.default)
            settings[setting.key.lower()] = _value_for_ini(setting, value)

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
            _atomic_write_config_updater(updater)
        except Exception:
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
    try:
        with _config_file_lock():
            updater = ConfigUpdater()
            updater.read(CONFIGFILE)

            # Ensure [Settings] section exists
            if 'Settings' not in updater:
                updater.add_section('Settings')

            setting = _SETTINGS_BY_KEY["SHOW_IMAGES_WITH_OVERLAY"]
            updater['Settings']['show_images_with_overlay'] = _value_for_ini(
                setting, CONFIG['SHOW_IMAGES_WITH_OVERLAY']
            )

            _atomic_write_config_updater(updater)
        logging.info("Updated SHOW_IMAGES_WITH_OVERLAY in the configfile")
    except Exception as e:
        logging.error(f"Failed to update SHOW_IMAGES_WITH_OVERLAY in the configfile: {e}")

def update_single_config_parameter(parameter: str):
    """
    Updates only a single config parameter in the configuration file.

    Args:
        parameter (str): The parameter name, which shall be updated.
    """
    key = parameter.upper()
    setting = _SETTINGS_BY_KEY.get(key)

    # Remote-mode: never persist remote-only settings in config.ini.
    if is_remote_mode() and (key in _REMOTE_ONLY_SETTINGS or (setting and setting.remote_only)):
        try:
            _write_remote_overrides_from_config(_remote_configfile_path())
        except Exception:
            pass
        try:
            # Best-effort cleanup: remove the option from config.ini if present.
            with _config_file_lock():
                updater = ConfigUpdater()
                updater.read(CONFIGFILE)
                if 'Settings' in updater and parameter.lower() in updater['Settings']:
                    del updater['Settings'][parameter.lower()]
                    _atomic_write_config_updater(updater)
        except Exception:
            pass
        return

    if setting is None:
        logging.error(f"[CONFIG] Unknown config parameter '{key}' — not written")
        return

    # Note: persist=False only affects save_config() (bulk write). update_single may still
    # persist keys like LABELSTUDIO_API_TOKEN that are omitted from full saves.

    value = _value_for_ini(setting, CONFIG[key])
    try:
        with _config_file_lock():
            updater = ConfigUpdater()
            updater.read(CONFIGFILE)

            # Ensure [Settings] section exists
            if 'Settings' not in updater:
                updater.add_section('Settings')

            updater['Settings'][setting.key.lower()] = value
            _atomic_write_config_updater(updater)
        loggable_value = get_loggable_config_value(key, value)
        logging.info(f"Updated {key} in the configfile to: {loggable_value}")
    except Exception as e:
        logging.error(f"Failed to update {key} in the configfile: {e}")

    # Keep remote override file in sync in remote-mode.
    if is_remote_mode() and key in _REMOTE_ONLY_SETTINGS:
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
    with _config_file_lock():
        _atomic_write_text(CONFIGFILE, parser.write)
    logging.info(f"Default configuration written to {CONFIGFILE}")

_locale_bootstrap_lock = threading.Lock()
_locale_bootstrap_done = False

def set_language(language_code = "de"):
    """Load translations for the specified language."""
    global _locale_bootstrap_done

    if not _locale_bootstrap_done:
        with _locale_bootstrap_lock:
            if not _locale_bootstrap_done:
                try:
                    ensure_runtime_locales_ready()
                except Exception as e:
                    logging.warning(f"[I18N] Failed to prepare runtime locales: {e}")
                _locale_bootstrap_done = True

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