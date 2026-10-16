import os
import gettext
import configparser
import logging
import shutil
import sqlite3
import sys
import threading
from datetime import datetime, timezone
# If the systemd journal Python bindings are available, use them.
try:
    from systemd.journal import JournalHandler  # type: ignore
except Exception:
    JournalHandler = None

from enum import Enum
import uuid
import json
from typing import Any, List, Tuple
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

# Legacy files. Read once at startup, then deleted after a successful import.
CONFIGFILE = 'config.ini'
REMOTE_CONFIGFILE = 'config.remote.ini'

###### SETTINGS SCHEMA ######
# Single source of truth for defaults, load typing, and save persistence.
# Add a new setting here only — load_config / save_config / update_single pick it up.
# Values live in the ``settings`` table of kittyhack.db (JSON), not in an ini file.


@dataclass(frozen=True)
class Setting:
    """One persisted (or runtime) config key: defaults, load kind, and save behavior."""

    key: str  # CONFIG key, e.g. "MOUSE_THRESHOLD" (legacy ini option = key.lower())
    default: Any
    kind: str  # str | int | float | bool | enum | allowed_to_exit
    enum_cls: Any = None
    save_fmt: str | None = None  # legacy ini formatting; unused by the database store
    persist: bool = True  # False: load into CONFIG but never write on save_config
    remote_only: bool = False  # kept across a remote sync that replaces kittyhack.db
    save_as_str: bool = False  # legacy ini flag; unused by the database store


def _S(key, default, kind="str", **kwargs) -> Setting:
    """Shorthand constructor for a ``Setting`` in ``SETTINGS_SCHEMA``."""
    return Setting(key=key, default=default, kind=kind, **kwargs)


SETTINGS_SCHEMA: list[Setting] = [
    _S("TIMEZONE", "Europe/Berlin"),
    _S("LANGUAGE", "en"),
    _S("DATE_FORMAT", "yyyy-mm-dd"),
    _S("DATABASE_PATH", "../kittyflap.db"),
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
    _S("MODEL_TRAINING_JOB_TOKEN", ""),
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
    # Remote control / remote-mode. Restored locally after a database sync.
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

# Default configuration values (generated from schema; UI reads DEFAULT_CONFIG["Settings"][...])
DEFAULT_CONFIG = {
    "Settings": {s.key.lower(): s.default for s in SETTINGS_SCHEMA}
}


def _load_default_for_setting(setting: Setting) -> Any:
    """Typed default used when a stored value is missing or invalid."""
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


_SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def kittyhack_db_path() -> str:
    """Absolute path of the kittyhack SQLite database.

    ``KITTYHACK_DATABASE_PATH`` in the environment overrides the canonical
    ``<repo>/kittyhack.db`` location (used by tests).
    """
    override = os.environ.get("KITTYHACK_DATABASE_PATH", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(kittyhack_root(), "kittyhack.db")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _connect_settings(db_path: str | None = None) -> sqlite3.Connection:
    """Open the settings database in autocommit mode (caller starts transactions)."""
    path = db_path or kittyhack_db_path()
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _ensure_settings_table(conn: sqlite3.Connection) -> None:
    conn.execute(_SETTINGS_DDL)


def _read_setting_rows(db_path: str | None = None) -> dict[str, str]:
    """Return raw JSON values keyed by settings-table key. Empty if unreadable."""
    path = db_path or kittyhack_db_path()
    try:
        conn = _connect_settings(path)
    except Exception as e:
        logging.error(f"[CONFIG] Failed to open settings database '{path}': {e}")
        return {}
    try:
        _ensure_settings_table(conn)
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {str(key): str(value) for key, value in rows}
    except Exception as e:
        logging.error(f"[CONFIG] Failed to read settings from '{path}': {e}")
        return {}
    finally:
        conn.close()


def _settings_count(db_path: str | None = None) -> int:
    path = db_path or kittyhack_db_path()
    if not os.path.isfile(path):
        return 0
    try:
        conn = _connect_settings(path)
    except Exception:
        return 0
    try:
        _ensure_settings_table(conn)
        row = conn.execute("SELECT COUNT(*) FROM settings").fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


def settings_count(db_path: str | None = None) -> int:
    """Return the number of persisted settings in a Kittyhack database."""
    return _settings_count(db_path)


def settings_revision(db_path: str | None = None) -> str:
    """Latest ``updated_at`` in the settings table (empty when there are no rows)."""
    path = db_path or kittyhack_db_path()
    if not os.path.isfile(path):
        return ""
    try:
        conn = _connect_settings(path)
    except Exception:
        return ""
    try:
        _ensure_settings_table(conn)
        row = conn.execute("SELECT MAX(updated_at) FROM settings").fetchone()
        return str(row[0] or "") if row else ""
    except Exception:
        return ""
    finally:
        conn.close()


def _json_for_setting(setting: Setting, value: Any) -> str:
    """Encode a CONFIG value as JSON for the settings table."""
    if setting.kind in {"enum", "allowed_to_exit"} and hasattr(value, "value"):
        value = value.value
    if setting.kind == "int":
        try:
            value = int(value or 0)
        except Exception:
            value = int(setting.default or 0)
    elif setting.kind == "float":
        try:
            value = float(value)
        except Exception:
            value = float(setting.default or 0.0)
    elif setting.kind == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in {"1", "true", "yes", "on"}
        else:
            value = bool(value)
    elif value is None and setting.key in {"MQTT_USERNAME", "MQTT_PASSWORD"}:
        value = ""
    return json.dumps(value, ensure_ascii=False)


def _parse_allowed_to_exit(raw: Any, default_member: AllowedToExit) -> AllowedToExit:
    low = str(raw).strip().lower()
    if low.startswith("allowedtoexit."):
        low = low.split(".", 1)[1]
    if low in {"true", "on", "1", "yes"}:
        return AllowedToExit.ALLOW
    if low in {"false", "off", "0", "no"}:
        return AllowedToExit.DENY
    return AllowedToExit(low)


def _coerce_stored_value(setting: Setting, raw_json: str) -> Any:
    """Decode one JSON settings value into the typed CONFIG form. Raises on failure."""
    value = json.loads(raw_json)
    if setting.kind == "int":
        return int(value)
    if setting.kind == "float":
        return float(value)
    if setting.kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if setting.kind == "enum" and setting.enum_cls is not None:
        return setting.enum_cls(value)
    if setting.kind == "allowed_to_exit":
        return _parse_allowed_to_exit(value, _load_default_for_setting(setting))
    if setting.key in {"MQTT_USERNAME", "MQTT_PASSWORD"} and value is None:
        return ""
    return value


def _default_config_map() -> dict[str, Any]:
    return {setting.key: _load_default_for_setting(setting) for setting in SETTINGS_SCHEMA}


def _config_from_stored_rows(
    rows: dict[str, str],
) -> tuple[dict[str, Any], List[Tuple[str, Any]]]:
    """Build a full CONFIG map from stored JSON. Invalid rows become defaults."""
    loaded: dict[str, Any] = {}
    invalid: List[Tuple[str, Any]] = []
    for setting in SETTINGS_SCHEMA:
        default = _load_default_for_setting(setting)
        raw = rows.get(setting.key)
        if raw is None:
            loaded[setting.key] = default
            continue
        try:
            loaded[setting.key] = _coerce_stored_value(setting, raw)
        except Exception:
            invalid.append((setting.key, raw))
            loaded[setting.key] = default
    return loaded, invalid


def _upsert_settings(values: dict[str, Any], *, only_persist: bool = False) -> bool:
    """Insert or update schema keys. ``only_persist`` skips ``persist=False`` keys."""
    rows: list[tuple[str, str]] = []
    for setting in SETTINGS_SCHEMA:
        if setting.key not in values:
            continue
        if only_persist and not setting.persist:
            continue
        try:
            encoded = _json_for_setting(setting, values[setting.key])
        except Exception as e:
            logging.error(f"[CONFIG] Failed to encode {setting.key}: {e}")
            return False
        rows.append((setting.key, encoded))
    if not rows:
        return True
    now = _utc_now()
    conn = _connect_settings()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_settings_table(conn)
        conn.executemany(
            "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            [(key, encoded, now) for key, encoded in rows],
        )
        conn.execute("COMMIT")
        return True
    except Exception as e:
        logging.error(f"[CONFIG] Failed to write settings: {e}")
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return False
    finally:
        conn.close()


def _unlink_quiet(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception as e:
        logging.warning(f"[CONFIG] Failed to remove '{path}': {e}")


def _read_ini_parser(path: str) -> configparser.ConfigParser | None:
    if not os.path.isfile(path):
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except Exception as e:
        logging.warning(f"[CONFIG] Failed to parse '{path}': {e}")
        return None
    return parser


def _ini_has_settings(path: str) -> bool:
    parser = _read_ini_parser(path)
    return bool(parser and parser.has_section("Settings"))


def _remote_configfile_path() -> str:
    """Absolute path to ``config.remote.ini`` (or ``KITTYHACK_REMOTE_CONFIGFILE`` override)."""
    override = os.environ.get("KITTYHACK_REMOTE_CONFIGFILE")
    if override:
        return override
    return os.path.join(kittyhack_root(), REMOTE_CONFIGFILE)


def _maybe_relocate_database(ini_path: str) -> None:
    """Move a legacy database onto the canonical path when that file does not exist yet."""
    parser = _read_ini_parser(ini_path)
    if parser is None or not parser.has_option("Settings", "kittyhack_database_path"):
        return
    raw = (parser.get("Settings", "kittyhack_database_path") or "").strip()
    if not raw:
        return
    source = raw if os.path.isabs(raw) else os.path.abspath(raw)
    canonical = kittyhack_db_path()
    try:
        if os.path.realpath(source) == os.path.realpath(canonical):
            return
    except Exception:
        if os.path.abspath(source) == os.path.abspath(canonical):
            return
    if not os.path.isfile(source) or os.path.exists(canonical):
        if os.path.isfile(source) and os.path.exists(canonical):
            logging.info(
                f"[CONFIG] Keeping database '{canonical}'; "
                f"ignoring legacy path '{source}'."
            )
        return
    os.makedirs(os.path.dirname(os.path.abspath(canonical)) or ".", exist_ok=True)
    try:
        os.replace(source, canonical)
    except OSError:
        shutil.copy2(source, canonical)
        try:
            os.remove(source)
        except Exception:
            pass
    logging.info(f"[CONFIG] Moved database from '{source}' to '{canonical}'.")


def _parse_ini_settings(path: str) -> tuple[dict[str, Any], List[Tuple[str, Any]]]:
    """Load a legacy config.ini into a typed map. Invalid values become defaults."""
    parser = _read_ini_parser(path)
    invalid: List[Tuple[str, Any]] = []
    loaded = _default_config_map()
    if parser is None or not parser.has_section("Settings"):
        return loaded, invalid

    if parser.has_option("Settings", "simulate_kittyflap"):
        logging.warning(
            "[CONFIG] simulate_kittyflap in config.ini is ignored; "
            "use env KITTYHACK_SIMULATE=1 or kittyhack_control --simulate"
        )

    has_legacy_pictures = parser.has_option("Settings", "min_pictures_to_analyze")
    has_seconds = parser.has_option("Settings", "min_seconds_to_analyze")
    migrated_seconds: float | None = None
    if has_legacy_pictures and not has_seconds:
        try:
            legacy_pics = float(parser.get("Settings", "min_pictures_to_analyze"))
        except Exception:
            legacy_pics = float(_LEGACY_MIN_PICTURES_TO_ANALYZE_DEFAULT)
        migrated_seconds = round(float(legacy_pics) / float(_LEGACY_INFERENCE_FPS_FOR_MIGRATION), 1)
        migrated_seconds = max(0.1, migrated_seconds)
        logging.info(
            f"[CONFIG] Migrated min_pictures_to_analyze={legacy_pics} "
            f"-> min_seconds_to_analyze={migrated_seconds:.1f}"
        )
    elif has_legacy_pictures and has_seconds:
        logging.info("[CONFIG] Ignoring legacy key min_pictures_to_analyze")

    def has_option(key: str) -> bool:
        return parser.has_option("Settings", key.lower())

    def record_invalid(key: str, raw: Any) -> None:
        invalid.append((key, raw))

    for setting in SETTINGS_SCHEMA:
        option = setting.key.lower()
        default = loaded[setting.key]
        if setting.key == "MIN_SECONDS_TO_ANALYZE" and migrated_seconds is not None:
            loaded[setting.key] = migrated_seconds
            continue
        if not has_option(setting.key):
            continue
        try:
            raw = parser.get("Settings", option)
        except Exception:
            raw = ""
        try:
            if setting.kind == "int":
                loaded[setting.key] = parser.getint("Settings", option)
            elif setting.kind == "float":
                loaded[setting.key] = parser.getfloat("Settings", option)
            elif setting.kind == "bool":
                loaded[setting.key] = parser.getboolean("Settings", option)
            elif setting.kind == "enum" and setting.enum_cls is not None:
                loaded[setting.key] = setting.enum_cls(raw)
            elif setting.kind == "allowed_to_exit":
                loaded[setting.key] = _parse_allowed_to_exit(raw, default)
            else:
                loaded[setting.key] = raw
        except Exception:
            record_invalid(setting.key, raw)
            loaded[setting.key] = default
    return loaded, invalid


def import_legacy_config_ini(path: str) -> tuple[bool, str]:
    """Merge settings explicitly present in a legacy ``config.ini``.

    Existing values for keys absent from the file are left unchanged. In
    remote mode, local connection settings are never imported from the file.
    Returns ``(success, message_code)``.
    """
    if not path or not os.path.isfile(path):
        return False, "not_found"
    parser = _read_ini_parser(path)
    if parser is None:
        return False, "invalid_ini"
    if not parser.has_section("Settings"):
        return False, "missing_settings_section"

    parsed, invalid = _parse_ini_settings(path)
    imported: dict[str, Any] = {}
    has_legacy_pictures = parser.has_option(
        "Settings", "min_pictures_to_analyze"
    )
    has_seconds = parser.has_option("Settings", "min_seconds_to_analyze")

    for setting in SETTINGS_SCHEMA:
        if is_remote_mode() and setting.remote_only:
            continue
        if parser.has_option("Settings", setting.key.lower()):
            imported[setting.key] = parsed[setting.key]
        elif (
            setting.key == "MIN_SECONDS_TO_ANALYZE"
            and has_legacy_pictures
            and not has_seconds
        ):
            imported[setting.key] = parsed[setting.key]

    if not imported:
        return False, "no_supported_settings"
    if not _upsert_settings(imported):
        return False, "write_failed"

    CONFIG.update(imported)
    imported_keys = set(imported)
    _notify_invalid_settings(
        [(key, raw) for key, raw in invalid if key in imported_keys]
    )
    logging.info(
        f"[CONFIG] Imported {len(imported)} setting(s) from legacy file '{path}'."
    )
    return True, ""


def _read_remote_ini_overrides() -> dict[str, Any]:
    """Typed remote-only values present in config.remote.ini."""
    path = _remote_configfile_path()
    parser = _read_ini_parser(path)
    if parser is None or not parser.has_section("Settings"):
        return {}
    found: dict[str, Any] = {}
    for setting in SETTINGS_SCHEMA:
        if not setting.remote_only:
            continue
        option = setting.key.lower()
        if not parser.has_option("Settings", option):
            continue
        default = _load_default_for_setting(setting)
        try:
            if setting.kind == "int":
                found[setting.key] = parser.getint("Settings", option)
            elif setting.kind == "float":
                found[setting.key] = parser.getfloat("Settings", option)
            elif setting.kind == "bool":
                found[setting.key] = parser.getboolean("Settings", option)
            else:
                found[setting.key] = parser.get("Settings", option)
        except Exception:
            logging.warning(f"[CONFIG] Invalid remote override '{option}' in {path}; keeping current value")
            found[setting.key] = default
    return found


def _keep_existing_remote_only(
    pending: dict[str, Any],
    existing: dict[str, str],
    overlay_keys: set[str],
) -> None:
    """In remote mode, do not let a synced ini replace local connection settings."""
    if not is_remote_mode():
        return
    for setting in SETTINGS_SCHEMA:
        if not setting.remote_only or setting.key in overlay_keys:
            continue
        raw = existing.get(setting.key)
        if raw is None:
            continue
        try:
            pending[setting.key] = _coerce_stored_value(setting, raw)
        except Exception:
            continue


def _notify_invalid_settings(invalid_values: List[Tuple[str, Any]]) -> None:
    if not invalid_values:
        return
    lines = []
    for key, raw in invalid_values:
        masked = "********" if key in SENSITIVE_CONFIG_KEYS else raw
        lines.append(f"- {key}: '{masked}'")
        logging.warning(f"[CONFIG] Invalid value for {key} -> '{raw}'. Using default.")
    if CONFIG.get("LANGUAGE") == "de":
        msg = (
            "Die folgenden Konfigurationsschlüssel hatten ungültige Werte und wurden auf ihre Standardwerte zurückgesetzt:\n"
            + "\n".join(lines)
            + "\n\nPrüfe deine Einstellungen im Abschnitt Einstellungen."
        )
        header = "Ungültige Konfiguration erkannt"
    else:
        msg = (
            "The following configuration keys contained invalid values and have been reset to their default values:\n"
            + "\n".join(lines)
            + "\n\nCheck your settings in the Settings section."
        )
        header = "Invalid configuration detected"
    try:
        UserNotifications.add(
            header=header,
            message=msg,
            type="warning",
            id="config_invalid_values",
            skip_if_id_exists=True,
        )
    except Exception:
        logging.warning("[CONFIG] Could not add user notification for invalid values.")


# Gettext constants
LOCALE_DIR = "locales"
DOMAIN = "messages"

# Global dictionary to store configuration settings
CONFIG = {}

# True when this process had to seed a brand-new settings table.
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
    Load settings from kittyhack.db into CONFIG.

    A leftover config.ini (and config.remote.ini) is imported once and then
    deleted. Invalid values are replaced by their defaults. Missing keys are
    silently defaulted.
    """
    global CONFIG, CONFIG_CREATED_AT_STARTUP
    ini_path = CONFIGFILE
    remote_path = _remote_configfile_path()
    created = False
    invalid_values: List[Tuple[str, Any]] = []

    usable_ini = _ini_has_settings(ini_path)
    if usable_ini:
        _maybe_relocate_database(ini_path)

    existing = _read_setting_rows()
    remote_overlay = _read_remote_ini_overrides() if os.path.isfile(remote_path) else {}
    corrupt_ini = os.path.isfile(ini_path) and not usable_ini

    pending: dict[str, Any] | None = None
    if usable_ini:
        pending, invalid_values = _parse_ini_settings(ini_path)
        for key, value in remote_overlay.items():
            pending[key] = value
        _keep_existing_remote_only(pending, existing, set(remote_overlay))
        logging.info(f"[CONFIG] Importing settings from '{ini_path}'.")
    elif remote_overlay:
        if existing:
            pending, _ignored = _config_from_stored_rows(existing)
        else:
            pending = _default_config_map()
            created = True
        for key, value in remote_overlay.items():
            pending[key] = value
        logging.info(f"[CONFIG] Importing remote settings from '{remote_path}'.")
    elif not existing:
        if corrupt_ini:
            logging.warning("[CONFIG] config.ini is empty or has no [Settings] section. Ignoring it.")
        else:
            logging.info("[CONFIG] No settings found. Creating defaults.")
        pending = _default_config_map()
        created = True
    elif corrupt_ini:
        logging.warning("[CONFIG] config.ini is empty or has no [Settings] section. Ignoring it.")

    wrote = True
    if pending is not None:
        wrote = _upsert_settings(pending)
        if not wrote:
            logging.error("[CONFIG] Failed to persist settings; legacy config files were kept.")

    if wrote:
        if usable_ini or corrupt_ini:
            _unlink_quiet(ini_path)
            _unlink_quiet(f"{ini_path}.lock")
            if usable_ini:
                logging.info("[CONFIG] Migrated config.ini into the database and removed the file.")
            else:
                logging.info("[CONFIG] Removed unusable config.ini.")
        if remote_overlay:
            _unlink_quiet(remote_path)
            logging.info("[CONFIG] Removed config.remote.ini after migrating remote settings.")

    if created and wrote:
        CONFIG_CREATED_AT_STARTUP = True

    stored = _read_setting_rows()
    loaded, db_invalid = _config_from_stored_rows(stored)
    if db_invalid:
        _upsert_settings({key: loaded[key] for key, _raw in db_invalid})
        invalid_values.extend(db_invalid)

    previous_latest = CONFIG.get("LATEST_VERSION", "unknown") if CONFIG else "unknown"
    loaded["LATEST_VERSION"] = previous_latest or "unknown"
    loaded["KITTYHACK_DATABASE_PATH"] = kittyhack_db_path()

    # Update in-place so imported CONFIG references in other modules stay valid.
    CONFIG.clear()
    CONFIG.update(loaded)
    _notify_invalid_settings(invalid_values)


def save_config() -> bool:
    """Persist every ``persist=True`` CONFIG value into the settings table."""
    if not _upsert_settings(CONFIG, only_persist=True):
        logging.error("[CONFIG] Failed to update settings in the database.")
        return False
    logging.info("[CONFIG] Updated settings in the database.")
    return True


def update_config_images_overlay():
    """Persist SHOW_IMAGES_WITH_OVERLAY."""
    update_single_config_parameter("SHOW_IMAGES_WITH_OVERLAY")


def update_single_config_parameter(parameter: str) -> bool:
    """
    Persist one CONFIG key into the settings table.

    ``persist=False`` still applies only to ``save_config()``. A single-key
    update may store keys such as LABELSTUDIO_API_TOKEN.
    Returns False when the key is unknown or the write fails.
    """
    key = parameter.upper()
    setting = _SETTINGS_BY_KEY.get(key)
    if setting is None:
        logging.error(f"[CONFIG] Unknown config parameter '{key}' — not written")
        return False
    if key not in CONFIG:
        logging.error(f"[CONFIG] '{key}' is not loaded — not written")
        return False
    if not _upsert_settings({key: CONFIG[key]}):
        logging.error(f"[CONFIG] Failed to update {key} in the database.")
        return False
    loggable_value = get_loggable_config_value(key, CONFIG[key])
    logging.info(f"[CONFIG] Updated {key} in the database to: {loggable_value}")
    return True


def create_default_config():
    """Seed the settings table with schema defaults."""
    if not _upsert_settings(_default_config_map()):
        logging.error("[CONFIG] Failed to write default settings.")
        return
    logging.info(f"[CONFIG] Default settings written to {kittyhack_db_path()}")


def read_remote_config_values() -> dict:
    """Remote-setup fields from the in-memory configuration."""
    try:
        port = int(CONFIG.get("REMOTE_CONTROL_PORT", 8888) or 8888)
    except Exception:
        port = 8888
    try:
        timeout = float(CONFIG.get("REMOTE_CONTROL_TIMEOUT", 30.0) or 30.0)
    except Exception:
        timeout = 30.0
    return {
        "remote_target_host": str(CONFIG.get("REMOTE_TARGET_HOST") or "").strip(),
        "remote_control_port": port,
        "remote_control_timeout": timeout,
        "remote_sync_on_first_connect": bool(CONFIG.get("REMOTE_SYNC_ON_FIRST_CONNECT", True)),
        "remote_sync_labelstudio": bool(CONFIG.get("REMOTE_SYNC_LABELSTUDIO", True)),
    }


def remote_setup_required() -> bool:
    """True in remote mode when the target host has not been set."""
    if not is_remote_mode():
        return False
    return not str(CONFIG.get("REMOTE_TARGET_HOST") or "").strip()


def snapshot_remote_only_settings() -> dict[str, Any]:
    """Local connection settings to re-apply after a remote database sync.

    Prefers the settings table. Falls back to a not-yet-migrated config.remote.ini.
    """
    existing = _read_setting_rows()
    snapshot: dict[str, Any] = {}
    for setting in SETTINGS_SCHEMA:
        if not setting.remote_only or setting.key not in existing:
            continue
        try:
            snapshot[setting.key] = _coerce_stored_value(setting, existing[setting.key])
        except Exception:
            continue
    if snapshot:
        return snapshot
    return _read_remote_ini_overrides()


def restore_remote_only_settings(snapshot: dict[str, Any] | None) -> bool:
    """Write previously snapshotted remote-only keys back into the database and CONFIG."""
    if not snapshot:
        return True
    to_write: dict[str, Any] = {}
    for setting in SETTINGS_SCHEMA:
        if setting.remote_only and setting.key in snapshot:
            to_write[setting.key] = snapshot[setting.key]
            CONFIG[setting.key] = snapshot[setting.key]
    if not to_write:
        return True
    ok = _upsert_settings(to_write)
    if ok:
        logging.info("[CONFIG] Restored remote-only settings after database sync.")
    return ok


def ensure_settings_persisted() -> bool:
    """Write in-memory CONFIG when the live database has no settings rows.

    A backup restore or a freshly created database can replace the file that
    already held migrated settings. The values are still in CONFIG.
    """
    path = kittyhack_db_path()
    if not os.path.isfile(path):
        return False
    if _settings_count(path) > 0:
        return True
    logging.info("[CONFIG] Settings table is empty; persisting in-memory configuration.")
    return save_config()


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
    ID_INSIDE_UNLOCK_HELD_AFTER_MAX_TIME = "inside_unlock_held_after_max_time"
    ID_SUGGEST_PIR_SECOND_FACTOR = "suggest_pir_second_factor_for_camera_entry"
    ID_ORIGINAL_MODEL_ACCURACY = "original_kittyflap_model_accuracy"

    notifications = []
    muted_ids = []

    def __init__(cls):
        cls.load()

    @classmethod
    def _normalized_muted_ids(cls, muted) -> list:
        if not isinstance(muted, list):
            return []
        return [str(item) for item in muted if item]

    @classmethod
    def load(cls):
        """
        Load notifications from the json file.
        """
        cls.notifications = []
        cls.muted_ids = []
        try:
            with open("notifications.json", "r") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except json.JSONDecodeError:
            logging.error("[USR_NOTIFICATIONS] Failed to decode notifications.json. Starting with an empty list.")
            return

        # Legacy files were a bare list of notification dicts.
        if isinstance(data, list):
            cls.notifications = data
            return
        if isinstance(data, dict):
            notes = data.get("notifications", [])
            cls.notifications = notes if isinstance(notes, list) else []
            cls.muted_ids = cls._normalized_muted_ids(data.get("muted_ids", []))
            return

    @classmethod
    def save(cls):
        """
        Save notifications to the json file.
        """
        with open("notifications.json", "w") as f:
            json.dump(
                {
                    "notifications": cls.notifications,
                    "muted_ids": cls.muted_ids,
                },
                f,
                indent=4,
            )

    @classmethod
    def is_muted(cls, id: str) -> bool:
        """Return True if the user asked not to see this notification again."""
        return bool(id) and id in cls.muted_ids

    @classmethod
    def mute(cls, id: str):
        """Permanently suppress a notification id and drop any pending copy."""
        if not id:
            return False
        if id not in cls.muted_ids:
            cls.muted_ids.append(id)
        cls.notifications = [n for n in cls.notifications if n.get("id") != id]
        cls.save()
        logging.info(f"[USR_NOTIFICATIONS] Muted notification id: {id}")
        return True

    @classmethod
    def get_muted_ids(cls) -> list:
        """Return a copy of the muted notification ids."""
        return list(cls.muted_ids)

    @classmethod
    def clear_muted(cls) -> int:
        """Clear all muted notification ids. Returns how many were unmuted."""
        count = len(cls.muted_ids)
        if count:
            cls.muted_ids = []
            cls.save()
            logging.info("[USR_NOTIFICATIONS] Cleared all muted notification ids")
        return count

    @classmethod
    def add(cls, header, message, type="default", id=None, skip_if_id_exists=False, muteable=False):
        """
        Add a notification to the list.
        Args:
            header (str): The header of the notification.
            message (str): The message of the notification.
            type (str): The type of the notification. Can be "default", "message", "warning", "error"
            id (str): The id of the notification. If None, a random id will be generated.
            skip_if_id_exists (bool): If True, skip adding the notification if the id already exists.
            muteable (bool): If True, the UI may offer a "do not notify again" action.
        """
        if id is None:
            id = str(uuid.uuid4())
        if cls.is_muted(id):
            logging.debug(f"[USR_NOTIFICATIONS] Skipped muted notification id: {id}")
            return
        if skip_if_id_exists and any(n['id'] == id for n in cls.notifications):
            return
        entry = {
            "id": id,
            "header": header,
            "message": message,
            "type": type
        }
        if muteable:
            entry["muteable"] = True
        cls.notifications.append(entry)
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

# Attach a handler before the import-time load so a one-time config.ini migration
# is visible in the journal. The level is applied again once LOGLEVEL is known.
configure_logging("INFO")

# Initial load of the configuration
load_config()

# Configure logging
configure_logging(CONFIG['LOGLEVEL'])

# Initialize user notifications
UserNotifications()