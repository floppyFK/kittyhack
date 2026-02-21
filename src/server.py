import os
import configparser
import pandas as pd
from datetime import datetime, timedelta
import time as tm
from src.clock import monotonic_time
from shiny import render, ui, reactive, module
from shiny.types import FileInfo
import logging
import base64
from zoneinfo import ZoneInfo
from faicons import icon_svg
import math
import threading
import subprocess
import re
import hashlib
import asyncio
import tarfile
from io import BytesIO
import zipfile
import uuid
import glob
import shutil
from typing import List
from src.baseconfig import (
    CONFIG,
    CONFIG_CREATED_AT_STARTUP,
    AllowedToEnter,
    load_config,
    set_language,
    save_config,
    configure_logging,
    get_loggable_config_value,
    DEFAULT_CONFIG,
    UserNotifications,
    read_remote_config_values,
    remote_setup_required
)
from src.helper import (
    EventType,
    get_git_version,
    wait_for_network,
    get_free_disk_space,
    check_and_stop_kittyflap_services,
    read_latest_kittyhack_version,
    execute_update_step,
    get_file_size,
    get_local_date_from_utc_date,
    format_date_minmax,
    normalize_version,
    log_relevant_deb_packages,
    log_system_information,
    icon_svg_local,
    get_changelogs,
    get_current_ip,
    is_valid_uuid4,
    is_port_open,
    get_total_disk_space,
    get_used_ram_space,
    get_total_ram_space,
    fetch_github_release_notes,
    filter_release_notes_for_language,
    sigterm_monitor
)
from src.database import *
from src.system import (
    switch_wlan_connection, 
    get_wlan_connections, 
    systemcmd, 
    manage_and_switch_wlan, 
    delete_wlan_connection,
    get_labelstudio_status,
    get_labelstudio_installed_version,
    get_labelstudio_latest_version,
    install_labelstudio,
    update_labelstudio,
    remove_labelstudio,
    systemctl,
    scan_wlan_networks,
    get_hostname,
    set_hostname,
    update_kittyhack,
    upgrade_base_system_packages,
    ensure_target_boot_service_semantics,
    ensure_ffmpeg_installed,
)
from src.paths import pictures_original_dir, kittyhack_root
from src.mode import is_remote_mode

# Prepare gettext for translations based on the configured language
_ = set_language(CONFIG['LANGUAGE'])


def _disable_numeric_input(tag):
    """Disable a ui.input_numeric Tag (shiny currently has no disabled= for input_numeric)."""
    try:
        # Structure: <div> [0]=<label>, [1]=<input>
        if getattr(tag, "children", None) and len(tag.children) >= 2:
            tag.children[1].attrs["disabled"] = "disabled"
    except Exception:
        pass
    return tag


def _disable_input(tag):
    """Disable generic input tags (e.g. select/text) by setting disabled on the input element."""
    try:
        # Structure: <div> [0]=<label>, [1]=<input/select>
        if getattr(tag, "children", None) and len(tag.children) >= 2:
            tag.children[1].attrs["disabled"] = "disabled"
    except Exception:
        pass
    return tag


logging.info("----- Startup -----------------------------------------------------------------------------------------")

# Check and set the startup flag - this must be done before loading the model
if CONFIG['STARTUP_SHUTDOWN_FLAG'] == True:
    logging.warning("!!!!!!!!!! STARTUP FLAG WAS ACTIVE - NOT GRACEFUL SHUTDOWN DETECTED !!!!!!!!!!")
    CONFIG['NOT_GRACEFUL_SHUTDOWNS'] = CONFIG['NOT_GRACEFUL_SHUTDOWNS'] + 1
else:
    CONFIG['NOT_GRACEFUL_SHUTDOWNS'] = 0
CONFIG['STARTUP_SHUTDOWN_FLAG'] = True
update_single_config_parameter("NOT_GRACEFUL_SHUTDOWNS")
update_single_config_parameter("STARTUP_SHUTDOWN_FLAG")

if CONFIG['NOT_GRACEFUL_SHUTDOWNS'] >= 3:
    logging.error("Not graceful shutdown detected 3 times in a row!")
    if not is_remote_mode():
        logging.error("We will disable the 'use all cores' setting, if it was enabled.")
        CONFIG['USE_ALL_CORES_FOR_IMAGE_PROCESSING'] = False
        update_single_config_parameter("USE_ALL_CORES_FOR_IMAGE_PROCESSING")
    else:
        logging.error("[REMOTE_MODE] Keeping 'use all cores' enabled by policy.")
    CONFIG['NOT_GRACEFUL_SHUTDOWNS'] = 0
    update_single_config_parameter("NOT_GRACEFUL_SHUTDOWNS")

    # Add a entry to the user notifications, which will be shown at the next login in the frontend
    shutdown_message = (
        _("The kittyflap was not shut down gracefully several times in a row. Please do not power off the device without shutting it down first, otherwise the database may be corrupted!") + "\n\n" +
        _("If you have shut it down gracefully and see this message, please report it in the") + " " +
        "[GitHub issue tracker](https://github.com/floppyFK/kittyhack/issues), " +
        _("thanks!")
    )
    if not is_remote_mode():
        shutdown_message += (
            "\n\n" +
            _("> **NOTE:** The option `Use all CPU cores for image processing` has been disabled now automatically, since this could cause the issue on some devices.") + "\n" +
            _("Please check the settings and enable it again, if you want to use it.")
        )

    UserNotifications.add(
        header=_("Several crashes detected!"),
        message=shutdown_message,
                type="warning",
                id="not_graceful_shutdown",
                skip_if_id_exists=True
        )

if not CONFIG['MQTT_DEVICE_ID']:
    # Generate a new MQTT device ID if it does not exist
    CONFIG['MQTT_DEVICE_ID'] = f"kittyhack_{str(uuid.uuid4()).split('-')[0]}"
    update_single_config_parameter('MQTT_DEVICE_ID')
    logging.info(f"[BACKEND] Generated MQTT device ID: {CONFIG['MQTT_DEVICE_ID']}")
    
# Now proceed with the startup
# Remote-mode policy: image processing must always use all CPU cores.
if is_remote_mode() and not CONFIG.get('USE_ALL_CORES_FOR_IMAGE_PROCESSING', False):
    logging.info("[REMOTE_MODE] Enforcing USE_ALL_CORES_FOR_IMAGE_PROCESSING=True.")
    CONFIG['USE_ALL_CORES_FOR_IMAGE_PROCESSING'] = True
    update_single_config_parameter("USE_ALL_CORES_FOR_IMAGE_PROCESSING")

from src.model import YoloModel


def _ensure_valid_startup_model_selection() -> None:
    """Ensure configured model exists, otherwise fallback and notify user."""
    configured_yolo = (CONFIG.get('YOLO_MODEL') or '').strip()
    if not configured_yolo:
        return

    try:
        configured_path = YoloModel.get_model_path(configured_yolo)
    except Exception as e:
        logging.warning(f"[MODEL] Failed to validate configured YOLO model '{configured_yolo}': {e}")
        configured_path = None

    if configured_path:
        return

    logging.warning(
        f"[MODEL] Configured YOLO model '{configured_yolo}' does not exist. Selecting fallback model."
    )

    fallback_yolo_id = None
    fallback_yolo_name = ""
    try:
        available_models = [m for m in YoloModel.get_model_list() if (m.get('unique_id') or '').strip()]
        available_models.sort(key=lambda m: (m.get('creation_date') or ''), reverse=True)
        for model in available_models:
            candidate_id = (model.get('unique_id') or '').strip()
            if not candidate_id:
                continue
            if candidate_id == configured_yolo:
                continue
            if YoloModel.get_model_path(candidate_id):
                fallback_yolo_id = candidate_id
                fallback_yolo_name = str(model.get('full_display_name') or model.get('display_name') or candidate_id)
                break
    except Exception as e:
        logging.warning(f"[MODEL] Failed while searching fallback YOLO model: {e}")

    if fallback_yolo_id:
        CONFIG['YOLO_MODEL'] = fallback_yolo_id
        CONFIG['TFLITE_MODEL_VERSION'] = ""
        update_single_config_parameter("YOLO_MODEL")
        update_single_config_parameter("TFLITE_MODEL_VERSION")
        try:
            UserNotifications.add(
                header=_("Model fallback applied"),
                message=(
                    _("The configured YOLO model was not found at startup:") + f" {configured_yolo}\n\n" +
                    _("Kittyhack switched automatically to another available YOLO model:") + f" {fallback_yolo_name}"
                ),
                type="warning",
                id="model_fallback_missing_yolo",
                skip_if_id_exists=True,
            )
        except Exception as e:
            logging.warning(f"[MODEL] Failed to create user notification for YOLO fallback: {e}")
        logging.info(f"[MODEL] Fallback applied: YOLO_MODEL={fallback_yolo_id}")
        return

    CONFIG['YOLO_MODEL'] = ""
    CONFIG['TFLITE_MODEL_VERSION'] = "original_kittyflap_model_v2"
    update_single_config_parameter("YOLO_MODEL")
    update_single_config_parameter("TFLITE_MODEL_VERSION")
    try:
        UserNotifications.add(
            header=_("Model fallback applied"),
            message=(
                _("The configured YOLO model was not found at startup:") + f" {configured_yolo}\n\n" +
                _("No other YOLO model was available. Kittyhack switched to the shipped TFLite model:") +
                " original_kittyflap_model_v2"
            ),
            type="warning",
            id="model_fallback_missing_yolo",
            skip_if_id_exists=True,
        )
    except Exception as e:
        logging.warning(f"[MODEL] Failed to create user notification for TFLite fallback: {e}")
    logging.info("[MODEL] Fallback applied: TFLITE_MODEL_VERSION=original_kittyflap_model_v2")


_ensure_valid_startup_model_selection()

from src.backend import backend_main, restart_mqtt, update_mqtt_config, update_mqtt_language, manual_door_override, model_handler, reload_model_handler_runtime
if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir
from src.model import RemoteModelTrainer
from src.shiny_wrappers import uix

# Read the GIT version
def _parse_version_tuple(version: str) -> tuple[int, ...]:
    parts = []
    for part in (version or "").split("."):
        if part.isdigit():
            parts.append(int(part))
        else:
            break
    return tuple(parts)


def _get_highest_changelog_version() -> str | None:
    changelog_dir = os.path.join(kittyhack_root(), "doc", "changelogs")
    pattern = os.path.join(changelog_dir, "changelog_v*_*.md")
    best_version = None
    best_tuple = None
    for path in glob.glob(pattern):
        name = os.path.basename(path)
        match = re.search(r"changelog_v(\d+(?:\.\d+)+)_", name)
        if not match:
            continue
        version = match.group(1)
        version_tuple = _parse_version_tuple(version)
        if not version_tuple:
            continue
        if best_tuple is None or version_tuple > best_tuple:
            best_tuple = version_tuple
            best_version = version
    return best_version


def _get_fallback_version() -> str:
    version = _get_highest_changelog_version()
    if version:
        return f"v{version}"
    return "unknown"


git_repo_available = os.path.isdir(os.path.join(kittyhack_root(), ".git"))
version_from_changelog = False
try:
    git_version = get_git_version()
except Exception as e:
    logging.warning(f"Failed to read git version: {e}")
    git_version = "unknown"

if not git_version or git_version == "unknown":
    fallback_version = _get_fallback_version()
    if fallback_version != "unknown":
        git_version = fallback_version
        version_from_changelog = True
        logging.warning(f"Using version from changelog files: {git_version}")

remote_setup_required = remote_setup_required()
backend_thread = None
background_task_started = False


def start_backend_if_needed():
    global backend_thread
    if backend_thread is not None and backend_thread.is_alive():
        return
    logging.info("Starting backend...")
    backend_thread = threading.Thread(target=backend_main, args=(CONFIG['SIMULATE_KITTYFLAP'],), daemon=True)
    backend_thread.start()


def start_background_task_if_needed():
    global background_task_started
    if background_task_started:
        return
    start_background_task()
    background_task_started = True

# MIGRATION RULES ##################################################################################################

last_booted_version = CONFIG['LAST_BOOTED_VERSION']
# Check if we need to update the USE_ALL_CORES_FOR_IMAGE_PROCESSING setting
# This is only needed once after updating to version 1.5.2 or higher
if (not is_remote_mode()) and normalize_version(last_booted_version) < '1.5.2' and normalize_version(git_version) >= '1.5.2':
    logging.info("First run after update to 1.5.2 or higher. Setting USE_ALL_CORES_FOR_IMAGE_PROCESSING to the new default value.")
    CONFIG['USE_ALL_CORES_FOR_IMAGE_PROCESSING'] = False
    update_single_config_parameter("USE_ALL_CORES_FOR_IMAGE_PROCESSING")

# Now update the last booted version in the configuration
CONFIG['LAST_BOOTED_VERSION'] = git_version
update_single_config_parameter("LAST_BOOTED_VERSION")

# Check if the CAT_THRESHOLD setting is lower than the MIN_THRESHOLD. If so, set it to the MIN_THRESHOLD
if CONFIG['CAT_THRESHOLD'] < CONFIG['MIN_THRESHOLD']:
    logging.warning(f"CAT_THRESHOLD is lower than MIN_THRESHOLD ({CONFIG['MIN_THRESHOLD']}). Setting CAT_THRESHOLD to MIN_THRESHOLD.")
    CONFIG['CAT_THRESHOLD'] = CONFIG['MIN_THRESHOLD']
    update_single_config_parameter("CAT_THRESHOLD")

# END OF MIGRATION RULES ############################################################################################

logging.info(f"Current version: {git_version}")

# Log all configuration values from CONFIG dictionary
logging.info("Configuration values:")
for key, value in CONFIG.items():
    loggable_value = get_loggable_config_value(key, value)
    logging.info(f"{key}={loggable_value}")

# IMPORTANT: First of all check that the kwork and manager services are NOT running
# (only relevant on the target device)
if not is_remote_mode():
    check_and_stop_kittyflap_services(CONFIG['SIMULATE_KITTYFLAP'])

    # Migration: devices updating from pre-remote versions may still have kittyhack.service enabled
    # and kittyhack_control.service disabled/missing. Ensure the new boot semantics take effect
    # on the next reboot (best-effort; should never block startup).
    try:
        ensure_target_boot_service_semantics()
    except Exception as e:
        logging.warning(f"[SYSTEM] Failed to ensure target boot service semantics: {e}")

# Remote-mode constraints
if is_remote_mode():
    if not (CONFIG.get('REMOTE_TARGET_HOST') or '').strip():
        logging.warning("[REMOTE_MODE] REMOTE_TARGET_HOST is empty; remote sensors/actors will not connect.")

# Cleanup old temp files
if os.path.exists("/tmp/kittyhack.db"):
    try:
        os.remove("/tmp/kittyhack.db")
    except:
        logging.error("Failed to delete the temporary kittyhack.db file.")

# Remove deprecated backup database file (pre v2.4) at startup
if os.path.exists(CONFIG['KITTYHACK_DATABASE_BACKUP_PATH']):
    try:
        os.remove(CONFIG['KITTYHACK_DATABASE_BACKUP_PATH'])
        logging.info(f"Deleted deprecated backup database file: {CONFIG['KITTYHACK_DATABASE_BACKUP_PATH']}")
    except Exception as e:
        logging.warning(f"Failed to delete deprecated backup database file '{CONFIG['KITTYHACK_DATABASE_BACKUP_PATH']}': {e}")

def prune_old_backups(backup_dir: str, keep: int = 3):
    """
    Keep only the newest `keep` kittyhack_backup_*.db files in backup_dir.
    """
    try:
        pattern = os.path.join(backup_dir, "kittyhack_backup_*.db")
        files = [f for f in glob.glob(pattern) if os.path.isfile(f)]
        if len(files) <= keep:
            return
        # Sort by modification time (newest first)
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        to_delete = files[keep:]
        for f in to_delete:
            try:
                os.remove(f)
                logging.info(f"[DATABASE_BACKUP] Pruned old backup: {f}")
            except Exception as e:
                logging.warning(f"[DATABASE_BACKUP] Failed to delete old backup '{f}': {e}")
    except Exception as e:
        logging.warning(f"[DATABASE_BACKUP] Failed pruning backups: {e}")

# Initial prune at startup (in case of manual or accumulated backups)
try:
    prune_old_backups(os.path.dirname(CONFIG['KITTYHACK_DATABASE_PATH']) or ".")
except Exception:
    pass

# Initial database integrity check
if os.path.exists(CONFIG['KITTYHACK_DATABASE_PATH']):
    db_check = check_database_integrity(CONFIG['KITTYHACK_DATABASE_PATH'])
    if db_check.success:
        logging.info("Initial Database integrity check successful.")
    else:
        logging.error(f"Initial Database integrity check failed: {db_check.message}")
        backup_dir = os.path.dirname(CONFIG['KITTYHACK_DATABASE_PATH']) or "."
        pattern = os.path.join(backup_dir, "kittyhack_backup_*.db")
        backup_files = [f for f in glob.glob(pattern) if os.path.isfile(f)]
        if backup_files:
            # Newest first
            backup_files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
            latest_backup = backup_files[0]
            logging.info(f"[DATABASE_BACKUP] Attempting restore from latest backup: {latest_backup}")
            try:
                # Preserve corrupted file
                corrupt_archive = CONFIG['KITTYHACK_DATABASE_PATH'] + f".corrupt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
                try:
                    shutil.move(CONFIG['KITTYHACK_DATABASE_PATH'], corrupt_archive)
                    logging.info(f"Corrupted database archived as: {corrupt_archive}")
                except Exception as e:
                    logging.warning(f"Failed to archive corrupted database: {e}")
                shutil.copy2(latest_backup, CONFIG['KITTYHACK_DATABASE_PATH'])
                # Re-check integrity after restore
                post_restore = check_database_integrity(CONFIG['KITTYHACK_DATABASE_PATH'])
                if post_restore.success:
                    logging.info(f"[DATABASE_BACKUP] Restore successful from {latest_backup}")
                    # --- User notification about successful restore ---
                    try:
                        UserNotifications.add(
                            header=_("Database restored"),
                            message=_("The kittyhack database was corrupted at startup and has been restored from backup: {}. The corrupted file was archived as: {}").format(latest_backup, corrupt_archive),
                            type="warning",
                            id="db_restored",
                            skip_if_id_exists=True
                        )
                    except Exception as e:
                        logging.warning(f"Failed to create user notification for DB restore: {e}")
                else:
                    logging.error(f"[DATABASE_BACKUP] Restore failed: {post_restore.message}")
            except Exception as e:
                logging.error(f"[DATABASE_BACKUP] Unexpected error during restore: {e}")
        else:
            logging.error("[DATABASE_BACKUP] No backup files (kittyhack_backup_*.db) found. Remove corrupted database and start fresh.")
            os.remove(CONFIG['KITTYHACK_DATABASE_PATH'])
else:
    logging.warning(f"Database '{CONFIG['KITTYHACK_DATABASE_PATH']}' not found. This is probably the first start of the application.")

# Check, if the kittyhack database file exists. If not, create it.
if not os.path.exists(CONFIG['KITTYHACK_DATABASE_PATH']):
    logging.info(f"Database '{CONFIG['KITTYHACK_DATABASE_PATH']}' not found. Creating it...")
    create_kittyhack_events_table(CONFIG['KITTYHACK_DATABASE_PATH'])

if not check_if_table_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events"):
    logging.warning(f"Table 'events' not found in the kittyhack database. Creating it...")
    create_kittyhack_events_table(CONFIG['KITTYHACK_DATABASE_PATH'])

# v1.5.1: Check if the "thumbnails" column exists in the "events" table. If not, add it
if not check_if_column_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "thumbnail"):
    logging.warning(f"Column 'thumbnail' not found in the 'events' table. Adding it...")
    add_column_to_table(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "thumbnail", "BLOB")

# v2.0.0: Check if the "own_cat_probability" column exists in the "events" table. If not, add it
if not check_if_column_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "own_cat_probability"):
    logging.warning(f"Column 'own_cat_probability' not found in the 'events' table. Adding it...")
    add_column_to_table(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "own_cat_probability", "REAL")

# v2.5.0: Store recorded image dimensions for better initial aspect-ratio in the event modal
if not check_if_column_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "img_width"):
    logging.warning("Column 'img_width' not found in the 'events' table. Adding it...")
    add_column_to_table(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "img_width", "INTEGER")
if not check_if_column_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "img_height"):
    logging.warning("Column 'img_height' not found in the 'events' table. Adding it...")
    add_column_to_table(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "img_height", "INTEGER")

# v2.5.0: Store effective FPS per event so playback speed matches capture speed
if not check_if_column_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "effective_fps"):
    logging.warning("Column 'effective_fps' not found in the 'events' table. Adding it...")
    add_column_to_table(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "effective_fps", "REAL")

if not check_if_table_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "photo"):
    logging.warning(f"Legacy table 'photo' not found in the kittyhack database. Creating it...")
    create_kittyhack_photo_table(CONFIG['KITTYHACK_DATABASE_PATH'])

# Check if table "cats" exist in the kittyhack database. If not, create it.
if not check_if_table_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "cats"):
    logging.warning(f"Table 'cats' not found in the kittyhack database. Creating it...")
    create_kittyhack_cats_table(CONFIG['KITTYHACK_DATABASE_PATH'])
    # Migrate the cats from the kittyflap database to the kittyhack database
    if check_if_table_exists(CONFIG['DATABASE_PATH'], "cat"):
        migrate_cats_to_kittyhack(kittyflap_db=CONFIG['DATABASE_PATH'], kittyhack_db=CONFIG['KITTYHACK_DATABASE_PATH'])
    else:
        logging.warning("Table 'cat' not found in the kittyflap database. No cats migrated to the kittyhack database.")

# v3.4.0: Ensure per-cat settings columns exist (enable_prey_detection, allow_entry, allow_exit)
for db in [CONFIG['KITTYHACK_DATABASE_PATH']]:
    try:
        if os.path.exists(db):
            if not check_if_column_exists(db, "cats", "enable_prey_detection"):
                logging.warning(f"Column 'enable_prey_detection' not found in the 'cats' table of {db}. Adding it...")
                add_column_to_table(db, "cats", "enable_prey_detection", "INTEGER DEFAULT 1")
                write_stmt_to_database(db, "UPDATE cats SET enable_prey_detection = 1 WHERE enable_prey_detection IS NULL")
            if not check_if_column_exists(db, "cats", "allow_entry"):
                logging.warning(f"Column 'allow_entry' not found in the 'cats' table of {db}. Adding it...")
                add_column_to_table(db, "cats", "allow_entry", "INTEGER DEFAULT 1")
                write_stmt_to_database(db, "UPDATE cats SET allow_entry = 1 WHERE allow_entry IS NULL")
            if not check_if_column_exists(db, "cats", "allow_exit"):
                logging.warning(f"Column 'allow_exit' not found in the 'cats' table of {db}. Adding it...")
                add_column_to_table(db, "cats", "allow_exit", "INTEGER DEFAULT 1")
                write_stmt_to_database(db, "UPDATE cats SET allow_exit = 1 WHERE allow_exit IS NULL")
    except Exception as e:
        logging.error(f"Failed to ensure per-cat settings columns in database {db}: {e}")

if check_if_table_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "photo"):
    logging.info("Table 'photo' found in the kittyhack database. Migrating it to 'events'...")
    migrate_photos_to_events(CONFIG['KITTYHACK_DATABASE_PATH'])

# Migrate the kittyflap config database table into the config.ini:
if check_if_table_exists(CONFIG['DATABASE_PATH'], "config") and CONFIG['KITTYFLAP_CONFIG_MIGRATED'] == False:
    logging.info("Table 'config' found in the kittyflap database. Migrating it to the config.ini...")
    df_config = db_get_config(CONFIG['DATABASE_PATH'], ReturnDataConfigDB.all)
    if not df_config.empty:
        CONFIG['MOUSE_CHECK_ENABLED'] = bool(df_config.iloc[0]["detect_prey"])
        CONFIG['ALLOWED_TO_ENTER'] = AllowedToEnter.ALL if bool(df_config.iloc[0]["accept_all_cats"]) else AllowedToEnter.KNOWN
        CONFIG['KITTYFLAP_CONFIG_MIGRATED'] = True
        if save_config():
            logging.info("Kittyflap configuration migrated successfully.")
        else:
            logging.error("Failed to save the migrated kittyflap configuration.")
    else:
        logging.error("Failed to read the configuration from the kittyflap database.")

# Create indexes for the kittyhack database
create_index_on_events(CONFIG['KITTYHACK_DATABASE_PATH'])

# Wait for internet connectivity and NTP sync
logging.info("Waiting for network connectivity...")
if wait_for_network(timeout=10):
    try:
        CONFIG['LATEST_VERSION'] = read_latest_kittyhack_version(timeout=3)
        logging.info(f"[VERSION] Latest Kittyhack version fetched at startup: {CONFIG['LATEST_VERSION']}")
    except Exception as e:
        logging.warning(f"[VERSION] Failed to fetch latest Kittyhack version at startup: {e}")
else:
    logging.warning("Timeout for network connectivity reached. Proceeding without network connection.")
if is_remote_mode() and remote_setup_required:
    logging.warning("[REMOTE_MODE] Remote configuration missing; backend startup deferred.")
else:
    start_backend_if_needed()

# Log the relevant installed deb packages
log_relevant_deb_packages()

# Set the WLAN TX Power level
if is_remote_mode():
    logging.info("Remote-mode detected: skipping WLAN txpower and power-save configuration.")
else:
    logging.info(f"Setting WLAN TX Power to {CONFIG['WLAN_TX_POWER']} dBm...")
    systemcmd(["iwconfig", "wlan0", "txpower", f"{CONFIG['WLAN_TX_POWER']}"] , CONFIG['SIMULATE_KITTYFLAP'])
    logging.info("Disabling WiFi power saving mode...")
    systemcmd(["iw", "dev", "wlan0", "set", "power_save", "off"], CONFIG['SIMULATE_KITTYFLAP'])

logging.info("Starting frontend...")

# Check if the label-studio version is installed
CONFIG["LABELSTUDIO_VERSION"] = get_labelstudio_installed_version()

# Global for the free disk space:
free_disk_space = get_free_disk_space()

# Global flag to indicate if a user WLAN action is in progress
user_wlan_action_in_progress = False


def _wlan_action_marker_path() -> str:
    return os.path.join(kittyhack_root(), ".wlan-action-in-progress")


def _set_wlan_action_in_progress(active: bool) -> None:
    """Set/clear a cross-service marker for intentional WLAN reconfiguration.

    kittyhack_control reads this marker and temporarily pauses its WLAN watchdog
    so user-triggered WLAN changes are not treated as failures.
    """
    global user_wlan_action_in_progress
    user_wlan_action_in_progress = bool(active)

    marker = _wlan_action_marker_path()
    if active:
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(str(tm.time()))
        except Exception as e:
            logging.warning(f"[WLAN ACTION] Failed to create marker file '{marker}': {e}")
    else:
        try:
            if os.path.exists(marker):
                os.remove(marker)
        except Exception as e:
            logging.warning(f"[WLAN ACTION] Failed to remove marker file '{marker}': {e}")

# Check ids with images in the database
# In v2.4 we moved the images from the kittyhack database to the filesystem.
logging.info("Checking ids with images in the database...")
ids_with_original_blob = get_ids_with_original_blob(CONFIG['KITTYHACK_DATABASE_PATH'])
logging.info(f"Found {len(ids_with_original_blob)} images in the database with original_image blob.")
if len(ids_with_original_blob) == 0:
    CONFIG['EVENT_IMAGES_FS_MIGRATED'] = True

# Frontend background task in a separate thread
def start_background_task():
    # Register task in the sigterm_monitor object
    sigterm_monitor.register_task()

    def run_periodically():
        last_periodic_jobs_run_mono = monotonic_time()  # Start the first periodic jobs in PERIODIC_JOBS_INTERVAL seconds

        # Model training status check cadence (seconds)
        last_model_training_check_mono = monotonic_time() - 120  # allow immediate first check after boot

        # --- Added migration control state ---
        migration_last_mono = monotonic_time() - 5  # allow immediate first run
        migration_in_progress = False
        migration_batch_size = 200
        global ids_with_original_blob  # reuse list defined at startup

        # --- APT updates cadence state (24h) ---
        last_apt_update_mono = monotonic_time() - 86400  # allow immediate first check on boot

        while not sigterm_monitor.stop_now:
            # --- Background legacy image migration (every >=5s) ---
            # Migrate original_image / thumbnail BLOBs batch-wise (max 100 IDs) to filesystem
            try:
                if (not migration_in_progress
                    and ids_with_original_blob
                    and (monotonic_time() - migration_last_mono) >= 5):
                    migration_in_progress = True
                    batch = ids_with_original_blob[:migration_batch_size]
                    logging.info(f"[BG_MIGRATION] Starting migration batch of {len(batch)} IDs (remaining total: {len(ids_with_original_blob)})")
                    result = perform_event_image_migration_ids(
                        CONFIG['KITTYHACK_DATABASE_PATH'],
                        batch,
                        chunk_size=migration_batch_size
                    )
                    if result.success:
                        # Remove migrated IDs from list
                        migrated_set = set(batch)
                        ids_with_original_blob = [i for i in ids_with_original_blob if i not in migrated_set]
                        logging.info(f"[BG_MIGRATION] Batch done. Remaining legacy IDs: {len(ids_with_original_blob)} | {result.message}")
                        if not ids_with_original_blob:
                            # Perform a single VACUUM at the very end to reclaim space
                            logging.info("[BG_MIGRATION] Running final VACUUM on database after full migration...")
                            vacuum_database(CONFIG['KITTYHACK_DATABASE_PATH'])
                            logging.info("[BG_MIGRATION] Final VACUUM completed.")
                            CONFIG['EVENT_IMAGES_FS_MIGRATED'] = True
                            logging.info("[BG_MIGRATION] All legacy image blobs migrated successfully.")
                    else:
                        logging.warning(f"[BG_MIGRATION] Batch migration failed: {result.message}")
                    migration_last_mono = monotonic_time()
                    migration_in_progress = False
            except Exception as e:
                logging.error(f"[BG_MIGRATION] Unexpected migration error: {e}")
                migration_in_progress = False
                migration_last_mono = monotonic_time()

            # --- Model training status polling (every 120s, only if a training is active) ---
            now_mono = monotonic_time()
            if (now_mono - last_model_training_check_mono) >= 120:
                last_model_training_check_mono = now_mono
                try:
                    if is_valid_uuid4(CONFIG.get("MODEL_TRAINING", "")):
                        # Important: do not emit UI notifications from the background thread.
                        RemoteModelTrainer.check_model_training_result(show_notification=False, show_in_progress=False)
                except Exception as e:
                    logging.warning(f"[MODEL_TRAINING] Periodic status check failed: {e}")

            # --- Main periodic jobs (every PERIODIC_JOBS_INTERVAL seconds) ---
            # Periodically check that the kwork and manager services are NOT running anymore
            now_mono = monotonic_time()
            if now_mono - last_periodic_jobs_run_mono >= CONFIG['PERIODIC_JOBS_INTERVAL']:
                last_periodic_jobs_run_mono = now_mono
                check_and_stop_kittyflap_services(CONFIG['SIMULATE_KITTYFLAP'])
                immediate_bg_task("background task")

                # --- Base-system APT updates (once per 24h, only at night between 2:00 and 4:00) ---
                try:
                    hours_since_last_apt = (monotonic_time() - last_apt_update_mono) / 3600.0
                    current_time = datetime.now()
                    in_night_window = 2 <= current_time.hour < 4

                    if hours_since_last_apt >= 24:
                        if in_night_window:
                            logging.info("[APT] Starting base-system package refresh and upgrades...")
                            ok, msg = upgrade_base_system_packages()
                            if ok:
                                logging.info(f"[APT] Upgrade finished successfully: {msg}")
                            else:
                                logging.error(f"[APT] Upgrade failed: {msg}")
                            # Update timestamp regardless to avoid retry storms
                            last_apt_update_mono = monotonic_time()
                except Exception as e:
                    logging.error(f"[APT] Unexpected error in periodic APT update: {e}")
                    last_apt_update_mono = monotonic_time()

                # Cleanup the events table
                cleanup_deleted_events(CONFIG['KITTYHACK_DATABASE_PATH'])
                ids_without_thumbnail = get_ids_without_thumbnail(CONFIG['KITTYHACK_DATABASE_PATH'])
                if ids_without_thumbnail:
                    # Limit the number of thumbnails to generate in one run to 200 to avoid high CPU load
                    ids_without_thumbnail.reverse()
                    thumbnails_to_process = ids_without_thumbnail[:200]
                    logging.info(f"[TRIGGER: background task] Start generating thumbnails for {len(thumbnails_to_process)} events (out of {len(ids_without_thumbnail)} total)...")
                    for id in thumbnails_to_process:
                        get_thubmnail_by_id(database=CONFIG['KITTYHACK_DATABASE_PATH'], photo_id=id)
                    logging.info(f"[DATABASE] Generated {len(thumbnails_to_process)} thumbnails for events without thumbnail.")
                else:
                    logging.info("[TRIGGER: background task] No events found without thumbnail.")

                # Check the free disk space
                free_disk_space = get_free_disk_space()

                # Check the latest version of kittyhack on GitHub, if the periodic version check is enabled
                if CONFIG['PERIODIC_VERSION_CHECK']:
                    CONFIG['LATEST_VERSION'] = read_latest_kittyhack_version()

                # Check if the last backup date is stored in the configuration
                if CONFIG['LAST_DB_BACKUP_DATE']:
                    last_backup_date = datetime.strptime(CONFIG['LAST_DB_BACKUP_DATE'], '%Y-%m-%d %H:%M:%S')
                else:
                    last_backup_date = datetime.min

                # Check if the last scheduled vacuum date is stored in the configuration
                if CONFIG['LAST_VACUUM_DATE']:
                    last_vacuum_date = datetime.strptime(CONFIG['LAST_VACUUM_DATE'], '%Y-%m-%d %H:%M:%S')
                else:
                    last_vacuum_date = datetime.min

                # Perform backup only between 2:00 and 4:00 AM if last backup is >22h old
                current_time = datetime.now()
                backup_window = 2 <= current_time.hour < 4
                backup_needed = (current_time - last_backup_date) > timedelta(hours=22)
                
                if backup_needed and backup_window:
                    if CONFIG.get('EVENT_IMAGES_FS_MIGRATED', False):
                        logging.info(f"[TRIGGER: background task] It is {current_time.hour}:{current_time.minute}:{current_time.second}. Start backup of the kittyhack database...")
                        # Write timestamped backup next to the main DB
                        backup_dir = os.path.dirname(CONFIG['KITTYHACK_DATABASE_PATH']) or "."
                        backup_name = f"kittyhack_backup_{current_time.strftime('%Y%m%d_%H%M%S')}.db"
                        backup_dest = os.path.join(backup_dir, backup_name)
                        result = backup_database_sqlite(CONFIG['KITTYHACK_DATABASE_PATH'], backup_dest)
                        if result.success:
                            CONFIG['LAST_DB_BACKUP_DATE'] = current_time.strftime('%Y-%m-%d %H:%M:%S')
                            update_single_config_parameter("LAST_DB_BACKUP_DATE")
                            logging.info(f"[DATABASE_BACKUP] Backup successful: {backup_dest}")
                            prune_old_backups(backup_dir, keep=3)
                        else:
                            logging.error(f"[DATABASE_BACKUP] Backup failed: {result.message}")
                    else:
                        logging.info("[DATABASE_BACKUP] Skipping backup: legacy image migration not finished (EVENT_IMAGES_FS_MIGRATED = False).")

                # Perform Scheduled VACUUM only if the last scheduled vacuum date is older than 24 hours
                if (datetime.now() - last_vacuum_date) > timedelta(days=1):
                    logging.info("[TRIGGER: background task] Start cleanup of orphan image files...")
                    cleanup_orphan_image_files(CONFIG['KITTYHACK_DATABASE_PATH'])
                    logging.info("[TRIGGER: background task] Start VACUUM of the kittyhack database...")
                    vacuum_database(CONFIG['KITTYHACK_DATABASE_PATH'])
                    CONFIG['LAST_VACUUM_DATE'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    update_single_config_parameter("LAST_VACUUM_DATE")

                # Log system information
                log_system_information()

            # Sleep 5 seconds (split to allow faster shutdown)
            for __ in range(5):
                if sigterm_monitor.stop_now:
                    break
                tm.sleep(1.0)
        
        logging.info("[TRIGGER: background task] Stopped background task scheduler.")
        sigterm_monitor.signal_task_done()

    frontend_bg_thread = threading.Thread(target=run_periodically, daemon=True)
    frontend_bg_thread.start()

# Immediate sync of photos from kittyflap to kittyhack
def immediate_bg_task(trigger = "reload"):
    logging.info(f"[TRIGGER: {trigger}] Start immediate background task")
    # TODO: immediate background task
    logging.info(f"[TRIGGER: {trigger}] Currently nothing to do here - keep for future usage")
    logging.info(f"[TRIGGER: {trigger}] End immediate background task")

# Start the background task
if is_remote_mode() and remote_setup_required:
    logging.warning("[REMOTE_MODE] Remote configuration missing; background tasks deferred.")
else:
    start_background_task_if_needed()

# Global reactive triggers
reload_trigger_wlan = reactive.Value(0)
reload_trigger_photos = reactive.Value(0)
reload_trigger_ai = reactive.Value(0)
reload_trigger_config = reactive.Value(0)

# Live view helpers: force immediate refresh on camera config changes
# and keep the stage aspect-ratio in sync without waiting for the next image tick.
live_view_refresh_nonce = reactive.Value(0)
live_view_aspect = reactive.Value((4, 3))

# Global update progress state
update_progress_state = {
    "in_progress": False,
    "step": 0,
    "max_steps": 8,
    "message": "",
    "detail": "",
    "result": None,  # None, "ok", "reboot_dialog" or "error"
    "error_msg": "",
}
update_progress_lock = threading.Lock()

def set_update_progress(**kwargs):
    with update_progress_lock:
        update_progress_state.update(kwargs)

def get_update_progress():
    with update_progress_lock:
        return update_progress_state.copy()
    
#######################################################################
# Modules
#######################################################################
@module.ui
def btn_wlan_modify():
    return ui.input_action_button(id=f"btn_wlan_modify" , label="", icon=icon_svg("pencil", margin_left="-0.1em", margin_right="auto"), class_="btn-narrow btn-vertical-margin", style_="width: 42px;")

@module.ui
def btn_wlan_connect():
    return ui.input_action_button(id=f"btn_wlan_connect" , label="", icon=icon_svg("wifi", margin_left="-0.2em", margin_right="auto"), class_="btn-narrow btn-vertical-margin", style_="width: 42px;")

@module.ui
def btn_show_event():
    return ui.input_action_button(
        id=f"btn_show_event",
        label="",
        icon=icon_svg("magnifying-glass", margin_left="0", margin_right="0"),
        class_="btn-icon-square btn-outline-secondary",
    )

@module.ui
def btn_yolo_modify():
    return ui.input_action_button(
        id=f"btn_yolo_modify",
        label="",
        icon=icon_svg("pencil", margin_left="0", margin_right="0"),
        class_="btn-icon-square btn-vertical-margin",
    )

@module.ui
def btn_yolo_activate():
    return ui.input_action_button(
        id=f"btn_yolo_activate",
        label="",
        icon=icon_svg("check", margin_left="0", margin_right="0"),
        class_="btn-icon-square btn-outline-secondary btn-vertical-margin",
    )

@module.server
def show_event_server(input, output, session, block_id: int):

    # Use standard Python list to store frame IDs (int) and current frame index
    # Images are served directly from disk via /thumb/<id>.jpg (see app.py mounts).
    pictures: list[int] = []
    timestamps = []
    photo_ids: list[int | None] = []
    # Store lists of DetectedObjects, one list per event
    event_datas: List[List[DetectedObject]] = []
    frame_index = [0]  # 0-based index into pictures (kept non-reactive to avoid feedback loops)
    shown_index = [0]  # 0-based index that matches the actually displayed frame (confirmed by JS)
    frame_tick = reactive.Value(0)  # bump to force re-render when paused
    fallback_mode = [False]
    slideshow_running = reactive.Value(True)
    scrubber_suppress = reactive.Value(0)  # suppress server-side reactions to programmatic scrubber updates
    last_scrubber_seen = [None]  # 1-based last observed scrubber value (for robustness)
    bundle_url = [None]  # optional: /thumb/... tar.gz that contains all thumbnails for this event
    aspect_style = [""]  # CSS var: --kh-event-aspect: w / h
    event_effective_fps = [None]  # float | None

    def _event_bundle_rel_url(block_id: int) -> str:
        # Served via static_assets mapping in app.py: "/thumb" -> THUMBNAIL_DIR.
        # We store bundles in a subfolder to avoid collisions with <id>.jpg.
        # Use a plain .tar
        return f"/thumb/bundles/event_{int(block_id)}.tar"

    def _event_bundle_versioned_url(block_id: int, bundle_path: str) -> str:
        """Return a cache-busting bundle URL based on the file mtime.

        Important: Browsers may reuse cached responses for the same URL even if the
        underlying file was deleted/recreated. Adding a version query parameter ensures
        a rebuilt bundle is fetched again.
        """
        try:
            v = int(float(os.path.getmtime(bundle_path)) * 1000)
        except Exception:
            v = int(tm.time() * 1000)
        return f"{_event_bundle_rel_url(block_id)}?v={v}"

    def _event_bundle_fs_path(block_id: int) -> str:
        bundles_dir = os.path.join(THUMBNAIL_DIR, "bundles")
        os.makedirs(bundles_dir, exist_ok=True)
        return os.path.join(bundles_dir, f"event_{int(block_id)}.tar")

    def _ensure_event_bundle_file(block_id: int, pids: list[int]) -> str | None:
        """Create/update a tar containing all thumbnail JPGs for an event.

        Returns the URL path (under /thumb) or None if creation fails.
        """
        try:
            pids_int = [int(x) for x in (pids or [])]
        except Exception:
            pids_int = []

        if not pids_int or len(pids_int) < 2:
            return None

        bundle_path = _event_bundle_fs_path(block_id)

        # Rebuild bundle only if missing or older than any contained thumbnail.
        newest_thumb_mtime = 0.0
        thumb_paths: list[tuple[int, str]] = []
        for pid in pids_int:
            try:
                thumb_path = os.path.join(THUMBNAIL_DIR, f"{pid}.jpg")
                if not os.path.exists(thumb_path):
                    # Generate if missing (legacy rows).
                    get_thubmnail_by_id(database=CONFIG['KITTYHACK_DATABASE_PATH'], photo_id=pid)
                if os.path.exists(thumb_path):
                    thumb_paths.append((pid, thumb_path))
                    newest_thumb_mtime = max(newest_thumb_mtime, float(os.path.getmtime(thumb_path)))
            except Exception:
                continue

        if not thumb_paths:
            return None

        try:
            if os.path.exists(bundle_path):
                try:
                    if float(os.path.getmtime(bundle_path)) >= newest_thumb_mtime and os.path.getsize(bundle_path) > 0:
                        return _event_bundle_versioned_url(block_id, bundle_path)
                except Exception:
                    pass
        except Exception:
            pass

        tmp_path = bundle_path + ".tmp"
        try:
            # Build tar: entries are <pid>.jpg. Keep it simple for the JS tar parser.
            with tarfile.open(tmp_path, mode="w") as tf:
                for pid, thumb_path in thumb_paths:
                    try:
                        tf.add(thumb_path, arcname=f"{pid}.jpg", recursive=False)
                    except Exception:
                        continue

            # Atomic-ish replace
            try:
                os.replace(tmp_path, bundle_path)
            except Exception:
                shutil.move(tmp_path, bundle_path)

            return _event_bundle_versioned_url(block_id, bundle_path)
        except Exception as e:
            logging.debug(f"Failed creating event bundle for block_id={block_id}: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return None

    def _suppress_next_scrubber_events(count: int = 1) -> None:
        try:
            current = int(scrubber_suppress.get() or 0)
        except Exception:
            current = 0
        try:
            scrubber_suppress.set(max(0, current + int(count)))
        except Exception:
            scrubber_suppress.set(max(0, int(count)))

    def _handle_scrub_to(target_idx: int, source: str = "event") -> None:
        """Seek to target_idx (0-based) and pause if needed.

        This is used by both the explicit event handler and a fallback watcher.
        """
        if not pictures:
            return

        target_idx = max(0, min(len(pictures) - 1, int(target_idx)))

        # If a scrub happens while playing (race / fast click), pause playback and seek.
        if bool(slideshow_running.get()):
            try:
                slideshow_running.set(False)
                setattr(_advance_slideshow_when_loaded, "_last_pid", -1)
            except Exception:
                pass

        try:
            current_idx = int(frame_index[0] or 0)
        except Exception:
            current_idx = 0
        if target_idx == current_idx:
            return

        frame_index[0] = target_idx
        _bump_frame_tick()

    def _bump_frame_tick():
        try:
            frame_tick.set(int(frame_tick.get()) + 1)
        except Exception:
            # last-resort fallback
            frame_tick.set(monotonic_time())

    @render.ui
    def event_nav_controls():
        is_playing = bool(slideshow_running.get())
        play_pause_icon = (
            icon_svg("pause", margin_left="0", margin_right="0")
            if is_playing
            else icon_svg("play", margin_left="0", margin_right="0")
        )

        return ui.div(
            ui.input_action_button(
                id="btn_prev",
                label="",
                icon=icon_svg("backward-step", margin_left="0", margin_right="0"),
                class_="btn-icon-square btn-outline-secondary",
                disabled=is_playing,
                style_="opacity: 0.5;" if is_playing else "",
            ),
            ui.input_action_button(
                id="btn_play_pause",
                label="",
                icon=play_pause_icon,
                class_="btn-icon-square btn-outline-secondary",
            ),
            ui.input_action_button(
                id="btn_next",
                label="",
                icon=icon_svg("forward-step", margin_left="0", margin_right="0"),
                class_="btn-icon-square btn-outline-secondary",
                disabled=is_playing,
                style_="opacity: 0.5;" if is_playing else "",
            ),
            class_="event-modal-toolbar-nav",
        )

    @output
    @render.ui
    def event_scrubber_ui():
        is_playing = bool(slideshow_running.get())
        if not pictures or len(pictures) <= 1:
            return ui.HTML("")

        try:
            vis_idx = int(frame_index[0] or 0)
        except Exception:
            vis_idx = 0

        # Use 1-based values in the UI
        value = max(1, min(len(pictures), vis_idx + 1))

        # Always show the scrubber (video-player style). Keep data-playing for client logic.
        cls = "event-modal-scrubber"

        def _mouse_threshold() -> float:
            try:
                return float(CONFIG.get("MOUSE_THRESHOLD"))
            except Exception:
                return 0.0

        def _frame_marker_state(frame_i: int) -> tuple[bool, bool, bool]:
            """Return (has_prey, prey_is_strong, has_other) for a given frame index."""
            try:
                objs = event_datas[frame_i] if frame_i < len(event_datas) else []
            except Exception:
                objs = []

            has_other = False
            prey_max_prob = None
            for obj in (objs or []):
                try:
                    name = (obj.object_name or "").strip()
                except Exception:
                    name = ""
                if not name:
                    continue

                name_l = name.lower()
                if name_l == "false-accept":
                    continue

                if name_l in ("prey", "beute"):
                    try:
                        p = float(getattr(obj, "probability", 0.0))
                    except Exception:
                        p = 0.0
                    prey_max_prob = p if prey_max_prob is None else max(prey_max_prob, p)
                    continue

                has_other = True

            has_prey = prey_max_prob is not None
            prey_is_strong = bool(has_prey and float(prey_max_prob or 0.0) >= _mouse_threshold())
            return has_prey, prey_is_strong, has_other

        def _build_lane_segments(n_frames: int):
            """Build grouped segments per lane: prey (soft/hard) and other."""
            if n_frames <= 1:
                return ([], [])

            prey_lane: list[tuple[int, int, str]] = []
            other_lane: list[tuple[int, int, str]] = []

            prey_kind = ""
            prey_start = -1
            prey_end = -1

            other_on = False
            other_start = -1
            other_end = -1

            for i in range(n_frames):
                has_prey, prey_strong, has_other = _frame_marker_state(i)

                # Prey lane: kind is prey-strong or prey-soft
                this_prey_kind = "prey-strong" if (has_prey and prey_strong) else ("prey-soft" if has_prey else "")
                if not this_prey_kind:
                    if prey_kind:
                        prey_lane.append((prey_start, prey_end, prey_kind))
                        prey_kind, prey_start, prey_end = "", -1, -1
                else:
                    if this_prey_kind == prey_kind and i == prey_end + 1:
                        prey_end = i
                    else:
                        if prey_kind:
                            prey_lane.append((prey_start, prey_end, prey_kind))
                        prey_kind, prey_start, prey_end = this_prey_kind, i, i

                # Other lane: boolean segment
                if not has_other:
                    if other_on:
                        other_lane.append((other_start, other_end, "other"))
                        other_on, other_start, other_end = False, -1, -1
                else:
                    if other_on and i == other_end + 1:
                        other_end = i
                    else:
                        if other_on:
                            other_lane.append((other_start, other_end, "other"))
                        other_on, other_start, other_end = True, i, i

            if prey_kind:
                prey_lane.append((prey_start, prey_end, prey_kind))
            if other_on:
                other_lane.append((other_start, other_end, "other"))

            return (prey_lane, other_lane)

        def _pct(i: int, n_frames: int) -> float:
            denom = max(1, n_frames - 1)
            return (float(i) / float(denom)) * 100.0

        n_frames = len(pictures)
        prey_segments, other_segments = _build_lane_segments(n_frames)

        marker_children = []
        for start_i, end_i, kind in prey_segments:
            left = _pct(start_i, n_frames)
            right = _pct(end_i, n_frames)
            width = max(0.0, right - left)
            single_cls = " is-single" if start_i == end_i else ""
            marker_children.append(
                ui.tags.div(
                    {
                        "class": f"event-scrubber-seg lane-prey is-{kind}{single_cls}",
                        # Add a couple pixels so single-frame segments are still visible.
                        "style": f"left: calc({left:.4f}% - 1px); width: calc({width:.4f}% + 2px);",
                    }
                )
            )

        for start_i, end_i, kind in other_segments:
            left = _pct(start_i, n_frames)
            right = _pct(end_i, n_frames)
            width = max(0.0, right - left)
            single_cls = " is-single" if start_i == end_i else ""
            marker_children.append(
                ui.tags.div(
                    {
                        "class": f"event-scrubber-seg lane-other is-{kind}{single_cls}",
                        "style": f"left: calc({left:.4f}% - 1px); width: calc({width:.4f}% + 2px);",
                    }
                )
            )

        return ui.div(
            ui.input_slider(
                id="event_scrubber",
                label="",
                min=1,
                max=len(pictures),
                value=value,
                step=1,
                width="100%",
            ),
            ui.tags.div({"class": "event-scrubber-markers", "aria-hidden": "true"}, *marker_children),
            id="event_scrubber_wrap",
            class_=cls,
            **{"data-playing": "1" if is_playing else "0"},
        )

    @render.ui
    def download_single_ui():
        disabled = bool(slideshow_running.get())
        help_text = (
            _("Pause playback to download the current picture")
            if disabled
            else _("Download current picture")
        )

        if disabled:
            button = ui.tags.button(
                icon_svg("image", margin_left="0", margin_right="0"),
                type="button",
                class_="btn btn-icon-square btn-outline-secondary",
                disabled=True,
                tabindex="-1",
                **{"aria-disabled": "true"},
                style_="opacity: 0.5;",
            )
        else:
            button = ui.download_button(
                id="btn_download_single",
                label="",
                icon=icon_svg("image", margin_left="0", margin_right="0"),
                class_="btn-icon-square btn-outline-secondary",
            )

        return ui.tooltip(
            button,
            help_text,
            id="tooltip_download_single",
            options={"trigger": "hover"},
        )

    @render.ui
    @reactive.effect
    @reactive.event(input.btn_show_event)
    async def show_event():
        logging.info(f"Show event with block_id {block_id}")
        picture_type = ReturnDataPhotosDB.all_original_image
        blob_picture = "original_image"

        # FALLBACK: The event_text column was added in version 1.4.0. If it is not present, show the "modified_image" with baked-in event data
        event = db_get_photos_by_block_id(CONFIG['KITTYHACK_DATABASE_PATH'], block_id, ReturnDataPhotosDB.all_except_photos)
        if event.empty:
            # All frames may have been deleted; keep state empty and avoid index errors.
            pictures.clear()
            timestamps.clear()
            event_datas.clear()
            photo_ids.clear()
            bundle_url[0] = None
            aspect_style[0] = ""
            return

        # Read effective FPS (capture/playback speed) from DB if available.
        event_effective_fps[0] = None
        try:
            if 'effective_fps' in event.columns:
                for __, r in event.iterrows():
                    v = r.get('effective_fps')
                    if v is None or pd.isna(v):
                        continue
                    try:
                        fv = float(v)
                    except Exception:
                        continue
                    if fv > 0:
                        event_effective_fps[0] = fv
                        break
        except Exception:
            event_effective_fps[0] = None

        # Compute aspect ratio for stable initial modal size (before first image loads).
        try:
            w = None
            h = None
            if 'img_width' in event.columns and 'img_height' in event.columns:
                try:
                    for __, r in event.iterrows():
                        rw = r.get('img_width')
                        rh = r.get('img_height')
                        if rw is not None and rh is not None:
                            try:
                                rw_i = int(rw)
                                rh_i = int(rh)
                            except Exception:
                                continue
                            if rw_i > 0 and rh_i > 0:
                                w, h = rw_i, rh_i
                                break
                except Exception:
                    pass

            # Fallback: infer from the first thumbnail file
            if (w is None or h is None) and 'id' in event.columns:
                try:
                    pid0 = int(event.iloc[0]['id'])
                    thumb_path0 = os.path.join(THUMBNAIL_DIR, f"{pid0}.jpg")
                    if not os.path.exists(thumb_path0):
                        try:
                            get_thubmnail_by_id(database=CONFIG['KITTYHACK_DATABASE_PATH'], photo_id=pid0)
                        except Exception:
                            pass
                    if os.path.exists(thumb_path0):
                        with open(thumb_path0, 'rb') as f:
                            s = get_jpeg_size(f.read(256 * 1024))
                        if s:
                            w, h = int(s[0]), int(s[1])
                    # As a last resort, try the original image file
                    if (w is None or h is None):
                        orig_path0 = os.path.join(ORIGINAL_IMAGE_DIR, f"{pid0}.jpg")
                        if os.path.exists(orig_path0):
                            with open(orig_path0, 'rb') as f:
                                s = get_jpeg_size(f.read(256 * 1024))
                            if s:
                                w, h = int(s[0]), int(s[1])
                except Exception:
                    pass

            if w and h:
                aspect_style[0] = f"--kh-event-aspect: {w} / {h};"
                try:
                    update_image_dimensions_for_block(CONFIG['KITTYHACK_DATABASE_PATH'], block_id, w, h)
                except Exception:
                    pass
            else:
                aspect_style[0] = ""
        except Exception:
            aspect_style[0] = ""

        if not event.iloc[0]["event_text"]:
            fallback_mode[0] = True
            if CONFIG['SHOW_IMAGES_WITH_OVERLAY']:
                blob_picture = "modified_image"
                picture_type = ReturnDataPhotosDB.all_modified_image


        event = db_get_photos_by_block_id(CONFIG['KITTYHACK_DATABASE_PATH'], block_id, picture_type)
        
        # Clear modal state
        pictures.clear()
        timestamps.clear()
        event_datas.clear()
        photo_ids.clear()

        # Iterate over the rows and encode the pictures
        async def process_event_row(row):
            try:
                event_text = row['event_text']

                try:
                    pid = int(row['id'])
                except Exception:
                    return

                # Ensure a thumbnail file exists on disk (generate if missing).
                # Avoid reading/encoding image bytes here.
                try:
                    thumb_path = os.path.join(THUMBNAIL_DIR, f"{pid}.jpg")
                    if not os.path.exists(thumb_path):
                        # Will generate and persist to THUMBNAIL_DIR if needed.
                        get_thubmnail_by_id(database=CONFIG['KITTYHACK_DATABASE_PATH'], photo_id=pid)
                except Exception:
                    pass

                pictures.append(pid)
                photo_ids.append(pid)

                # Convert the timestamp to the local timezone and format it
                try:
                    timestamp = pd.to_datetime(row["created_at"])
                    timestamps.append(timestamp.tz_convert(CONFIG['TIMEZONE']).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3])
                except Exception as e:
                    logging.error(f"Failed to process timestamp: {e}")
                    timestamps.append("")

                try:
                    if event_text:
                        event_datas.append(read_event_from_json(event_text))
                    else:
                        event_datas.append([])
                except Exception as e:
                    logging.error(f"Failed to parse event data: {e}")
                    event_datas.append([])
            except Exception as e:
                logging.error(f"Failed to encode picture: {e}")

        # Process the event rows asynchronously
        await asyncio.gather(*(process_event_row(row) for x, row in event.iterrows()))

        # Sort the timestamps, picture IDs, and event_datas lists by timestamps
        if len(timestamps) > 0:
            sorted_data = sorted(zip(timestamps, pictures, event_datas, photo_ids), key=lambda x: x[0])
            timestamps[:], pictures[:], event_datas[:], photo_ids[:] = zip(*sorted_data)
            # zip() returns tuples
            pictures[:] = list(pictures)
            photo_ids[:] = list(photo_ids)
            event_datas[:] = list(event_datas)
            timestamps[:] = list(timestamps)

        # Legacy fallback: if no effective_fps stored, approximate it from the event's frame timestamps.
        if event_effective_fps[0] is None:
            try:
                ts = pd.to_datetime(event["created_at"], errors="coerce")
                ts = ts.dropna()
                if len(ts) >= 2 and len(pictures) >= 2:
                    span = (ts.max() - ts.min()).total_seconds()
                    if span and span > 0:
                        fps = float(len(pictures) - 1) / float(span)
                        if fps > 0:
                            # Keep within the same bounds as the frontend.
                            fps = max(0.1, min(30.0, fps))
                            event_effective_fps[0] = fps
            except Exception:
                pass

        # Initialize double-buffer state for the slideshow: visible + preload
        frame_index[0] = 0
        shown_index[0] = 0
        _bump_frame_tick()

        # Optional optimization: build a single tar.gz containing all thumbnails.
        # The browser can fetch & unpack once and then swap <img> sources to blob: URLs.
        try:
            bundle_url[0] = await asyncio.to_thread(_ensure_event_bundle_file, block_id, list(pictures))
        except Exception:
            bundle_url[0] = None

        if int(CONFIG['SHOW_IMAGES_WITH_OVERLAY']):
            overlay_icon = icon_svg('border-all', margin_left="0", margin_right="0")
        else:
            overlay_icon = icon_svg('border-none', margin_left="0", margin_right="0")

        ui.modal_show(
            ui.modal(
                ui.card(
                    ui.div(
                        ui.div(
                            ui.tags.div(
                                id="event_modal_js_layer",
                                class_="event-modal-js-layer",
                            ),
                            ui.tags.div(
                                ui.output_ui("show_event_picture"),
                                id="event_modal_overlay_container",
                                class_="event-modal-overlay-container",
                            ),
                            ui.tags.div(
                                {
                                    "class": "event-modal-picture-spinner",
                                    "aria-hidden": "true",
                                },
                                ui.tags.div(
                                    {
                                        "class": "spinner-border text-primary",
                                        "role": "status",
                                    },
                                    ui.tags.span({"class": "visually-hidden"}, _("Loading...")),
                                ),
                            ),
                            id="event_modal_picture_wrap",
                            class_="event-modal-picture-wrap",
                            style_=aspect_style[0],
                        ),
                        id="event_modal_root",
                        **{
                            "data-block-id": str(block_id),
                            "data-bundle-url": str(bundle_url[0] or ""),
                            "data-event-fps": "" if event_effective_fps[0] is None else str(event_effective_fps[0]),
                        },
                    ),
                    ui.card_footer(
                        ui.div(
                            ui.div(
                                ui.div(
                                    ui.tooltip(
                                        ui.input_action_button(
                                            id="btn_delete_event",
                                            label="",
                                            icon=icon_svg("trash-can", margin_left="0", margin_right="0"),
                                            class_="btn-icon-square btn-outline-danger",
                                        ),
                                        _("Delete all pictures of this event"),
                                        id="tooltip_delete_event",
                                        options={"trigger": "hover"},
                                    ),
                                    class_="event-modal-toolbar-left",
                                ),
                                ui.div(
                                    ui.output_ui("event_nav_controls"),
                                ),
                                ui.div(
                                    ui.tooltip(
                                        ui.input_action_button(
                                            id="btn_toggle_overlay",
                                            label="",
                                            icon=overlay_icon,
                                            class_="btn-icon-square btn-outline-secondary",
                                            style_=f"opacity: 0.5;" if fallback_mode[0] else "",
                                            disabled=fallback_mode[0],
                                        ),
                                        _("Toggle overlay for detected objects"),
                                        id="tooltip_toggle_overlay",
                                        options={"trigger": "hover"},
                                    ),
                                    class_="event-modal-toolbar-close",
                                ),
                                class_="event-modal-toolbar-row event-modal-toolbar-top",
                            ),
                            ui.div(
                                ui.div(
                                    ui.output_ui("download_single_ui"),
                                    class_="event-modal-toolbar-bottom-left",
                                ),
                                ui.div(
                                    ui.output_ui("event_scrubber_ui"),
                                    class_="event-modal-toolbar-bottom-middle",
                                ),
                                ui.div(
                                    ui.tooltip(
                                        ui.download_button(
                                            id="btn_download",
                                            label="",
                                            icon=icon_svg("file-zipper", margin_left="0", margin_right="0"),
                                            class_="btn-icon-square btn-outline-secondary",
                                        ),
                                        _("Download all pictures of this event (ZIP)"),
                                        id="tooltip_download_zip",
                                        options={"trigger": "hover"},
                                    ),
                                    ui.input_action_button(
                                        id="btn_modal_cancel",
                                        label="",
                                        icon=icon_svg("xmark", margin_left="0", margin_right="0"),
                                        class_="btn-icon-square btn-outline-secondary btn-icon-close",
                                    ),
                                    class_="event-modal-toolbar-bottom-right",
                                ),
                                class_="event-modal-toolbar-row event-modal-toolbar-bottom",
                            ),
                            class_="event-modal-toolbar",
                        ),
                    ),
                    full_screen=False,
                    class_="image-container"
                ),
                footer=ui.div(
                    ui.input_action_button("modal_pulse", "", style_="visibility:hidden; width:1px; height:1px;"),
                    ui.input_action_button("img_loaded_pulse", "", style_="visibility:hidden; width:1px; height:1px;"),
                ),
                size='l',
                easy_close=True,
                class_="transparent-modal-content"
            )
        )

        # Initialize scrubber to the current visible frame (if paused later)
        try:
            # Avoid triggering on_scrub_event from a programmatic update.
            _suppress_next_scrubber_events(1)
            ui.update_slider("event_scrubber", value=1, min=1, max=max(1, len(pictures)))
            last_scrubber_seen[0] = 1
        except Exception:
            pass
    
    @render.text
    def show_event_picture():
        # Dependency hook for explicit navigation / slideshow advances
        try:
            frame_tick.get()
        except Exception:
            pass

        try:
            if len(pictures) > 0:
                vis_idx = int(frame_index[0] or 0) % len(pictures)
                try:
                    shown_idx = int(shown_index[0] or 0) % len(pictures)
                except Exception:
                    shown_idx = vis_idx
                prev_idx = (vis_idx - 1) % len(pictures)
                next_idx = (vis_idx + 1) % len(pictures)

                visible_pid = int(pictures[vis_idx])
                prev_pid = int(pictures[prev_idx]) if len(pictures) > 1 else None
                next_pid = int(pictures[next_idx]) if len(pictures) > 1 else None

                visible_src = f"/thumb/{visible_pid}.jpg"
                prev_src = f"/thumb/{prev_pid}.jpg" if prev_pid is not None else ""
                next_src = f"/thumb/{next_pid}.jpg" if next_pid is not None else ""

                # Keep the scrubber synced to the currently visible frame while playing.
                # Do not use scrubber_suppress here: periodic updates may not always
                # generate input events, which could otherwise accumulate the counter.
                if slideshow_running.get():
                    try:
                        if len(pictures) > 1:
                            ui.update_slider(
                                "event_scrubber",
                                value=int(vis_idx) + 1,
                                min=1,
                                max=len(pictures),
                            )
                    except Exception:
                        pass

                overlay_on = (input.btn_toggle_overlay() % 2 == (1 - int(CONFIG['SHOW_IMAGES_WITH_OVERLAY'])))
                playing = 1 if bool(slideshow_running.get()) else 0

                # Indicator only (JS fully controls <img> rendering).
                indicator_html = (
                    f'<div id="event_modal_indicator" '
                    f'data-vis-idx="{vis_idx}" '
                    f'data-total="{len(pictures)}" '
                    f'data-overlay="{1 if overlay_on else 0}" '
                    f'data-playing="{playing}" '
                    f'data-visible-pid="{visible_pid}" '
                    f'data-visible-src="{visible_src}" '
                    f'data-prev-pid="{prev_pid if prev_pid is not None else ""}" '
                    f'data-prev-src="{prev_src}" '
                    f'data-next-pid="{next_pid if next_pid is not None else ""}" '
                    f'data-next-src="{next_src}" '
                    f'style="display:none"></div>'
                )

                # Overlay layer (kept separate from the JS image layer).
                overlay_html = '<div id="event_modal_overlay" style="position:absolute; inset:0; pointer-events:none;">'

                detected_objects = event_datas[shown_idx] if shown_idx < len(event_datas) else []
                if overlay_on:
                    for detected_object in detected_objects:
                        if detected_object.object_name != "false-accept":
                            obj_label = (detected_object.object_name or "").strip()
                            obj_label_l = obj_label.lower()
                            is_prey = obj_label_l in ("prey", "beute")

                            # Match scrubber marker logic: prey below threshold is light red.
                            prey_strong = False
                            if is_prey:
                                try:
                                    p = float(getattr(detected_object, "probability", 0.0))
                                except Exception:
                                    p = 0.0
                                try:
                                    thr = float(CONFIG.get("MOUSE_THRESHOLD"))
                                except Exception:
                                    thr = 0.0
                                prey_strong = bool(p >= thr)

                            if is_prey and not prey_strong:
                                stroke_rgb = "252, 165, 165"  # light red (tailwind red-300)
                                stroke_hex = "#fca5a5"
                            else:
                                stroke_rgb = "255, 0, 0" if is_prey else "0, 180, 0"
                                stroke_hex = "#ff0000" if is_prey else "#00b400"

                            overlay_html += f'''
                            <div style="position: absolute; 
                                        left: {detected_object.x}%; 
                                        top: {detected_object.y}%; 
                                        width: {detected_object.width}%; 
                                        height: {detected_object.height}%; 
                                        border: 2px solid {stroke_hex}; 
                                        background-color: rgba({stroke_rgb}, 0.05);
                                        pointer-events: none; z-index: 3;">
                                <div style="position: absolute; 
                                            {f'bottom: -26px' if detected_object.y < 16 else 'top: -26px'}; 
                                            left: 0px; 
                                            background-color: rgba({stroke_rgb}, 0.7); 
                                            color: white; 
                                            padding: 2px 5px;
                                            border-radius: 5px;
                                            text-wrap-mode: nowrap;
                                            font-size: 12px;">
                                    {detected_object.object_name} ({detected_object.probability:.0f}%)
                                </div>
                            </div>'''

                ts_display = timestamps[shown_idx][11:-4] if shown_idx < len(timestamps) else ""
                overlay_html += f'''
                    <div id="event_modal_timestamp" style="position: absolute; top: 12px; left: 50%; transform: translateX(-50%); background-color: rgba(0, 0, 0, 0.5); color: white; padding: 2px 5px; border-radius: 3px; z-index: 3;">
                        {ts_display}
                    </div>
                    <div id="event_modal_counter" style="position: absolute; bottom: 12px; right: 8px; background-color: rgba(0, 0, 0, 0.5); color: white; padding: 2px 5px; border-radius: 3px; z-index: 3;">
                        {shown_idx + 1}/{len(pictures)}
                    </div>
                </div>
                '''

                return ui.HTML(indicator_html + overlay_html)

            return ui.HTML('<div class="placeholder-image"><strong>' + _('No pictures found for this event.') + '</strong></div>')
        except Exception as e:
            logging.error(f"Failed to show the picture for event: {e}")
            return ui.HTML('<div class="placeholder-image"><strong>' + _('An error occured while reading the image.') + '</strong></div>')

    @reactive.effect
    @reactive.event(input.img_loaded_pulse)
    def _advance_slideshow_when_loaded():
        # Only advance while playing, and only once per loaded frame.
        if not bool(slideshow_running.get()):
            return

        if not pictures or len(pictures) <= 1:
            return

        try:
            vis_idx = int(frame_index[0] or 0) % len(pictures)
            pid = int(pictures[vis_idx])
        except Exception:
            return

        # Debounce duplicate pulses for the same PID (can happen on cache hits or rebinds)
        try:
            last_pid = int(getattr(_advance_slideshow_when_loaded, "_last_pid", -1))
        except Exception:
            last_pid = -1
        if pid == last_pid:
            return

        try:
            setattr(_advance_slideshow_when_loaded, "_last_pid", pid)
        except Exception:
            pass

        # Confirm that this frame is now actually visible.
        shown_index[0] = vis_idx

        frame_index[0] = (vis_idx + 1) % max(len(pictures), 1)
        _bump_frame_tick()

    @reactive.effect
    @reactive.event(input.img_loaded_pulse)
    def _confirm_frame_when_paused():
        # When paused, JS still pulses after it has swapped the visible <img>.
        # Use this to update overlays/timestamp/counter only after the image is really shown.
        if bool(slideshow_running.get()):
            return

        if not pictures:
            return

        try:
            idx = int(frame_index[0] or 0) % len(pictures)
        except Exception:
            return

        shown_index[0] = idx
        _bump_frame_tick()

    @reactive.effect
    @reactive.event(input.btn_modal_cancel, input.modal_pulse)
    def modal_cancel():
        ui.modal_remove()
        # Clear pictures and timestamps lists and reset frame index
        pictures.clear()
        timestamps.clear()
        event_datas.clear()
        photo_ids.clear()
        photo_ids.clear()
        frame_index[0] = 0
        shown_index[0] = 0
        last_scrubber_seen[0] = None
        try:
            scrubber_suppress.set(0)
        except Exception:
            pass

    def _single_image_filename():
        try:
            # Keep in sync with current frame
            frame_tick.get()
            picture_number = int(frame_index[0]) + 1 if frame_index[0] is not None else 0
        except Exception:
            picture_number = 0
        return f"kittyhack_event_{block_id}_{picture_number}.jpg"

    @render.download(filename=_single_image_filename)
    def btn_download_single():
        try:
            if slideshow_running.get():
                ui.notification_show(
                    _("Pause playback before downloading the current picture."),
                    duration=6,
                    type="warning",
                )
                raise RuntimeError("Playback running")

            vis_idx = int(frame_index[0] or 0)
            if vis_idx is None or vis_idx >= len(photo_ids):
                raise RuntimeError("No visible image")

            pid = photo_ids[int(vis_idx)]
            if pid is None:
                raise RuntimeError("Missing photo ID")

            # Prefer original image from filesystem if present
            img_bytes = None
            try:
                fp = os.path.join(pictures_original_dir(), f"{int(pid)}.jpg")
                if os.path.exists(fp):
                    with open(fp, "rb") as f:
                        img_bytes = f.read()
            except Exception:
                img_bytes = None

            # Fallback: legacy DB BLOB for older rows (if file not present)
            if img_bytes is None:
                try:
                    df = read_df_from_database(
                        CONFIG['KITTYHACK_DATABASE_PATH'],
                        f"SELECT original_image FROM events WHERE id = {int(pid)}",
                    )
                    if not df.empty:
                        ob = df.iloc[0].get('original_image')
                        if isinstance(ob, (bytes, bytearray)) and len(ob) > 0:
                            img_bytes = bytes(ob)
                except Exception:
                    pass

            if not isinstance(img_bytes, (bytes, bytearray)) or len(img_bytes) == 0:
                raise RuntimeError("No image bytes")

            out_path = os.path.join("/tmp", f"kittyhack_event_{block_id}_img_{int(pid)}.jpg")
            with open(out_path, "wb") as f:
                f.write(img_bytes)
            return out_path
        except Exception as e:
            logging.warning(f"[DOWNLOAD] Single image download failed: {e}")
            # Provide a tiny placeholder file rather than crashing the download handler
            out_path = os.path.join("/tmp", f"kittyhack_event_{block_id}_download_failed.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("Single image download failed.\n")
            return out_path
    
    @reactive.effect
    @reactive.event(input.btn_delete_event)
    def delete_event():
        logging.info(f"Delete all pictures of event with block_id {block_id}")
        delete_photos_by_block_id(CONFIG['KITTYHACK_DATABASE_PATH'], block_id)
        reload_trigger_photos.set(reload_trigger_photos.get() + 1)
        ui.modal_remove()
        # Clear pictures and timestamps lists and reset frame index
        pictures.clear()
        timestamps.clear()
        frame_index[0] = 0

    @render.download(filename=f"kittyhack_event_{block_id}.zip")
    def btn_download():
        # Fetch all rows in this block, including original and modified images
        df = db_get_photos_by_block_id(
            CONFIG['KITTYHACK_DATABASE_PATH'],
            block_id,
            ReturnDataPhotosDB.all
        )

        # Collect files to zip
        files: list[tuple[str, bytes]] = []
        if not df.empty:
            for __, row in df.iterrows():
                pid = int(row['id'])
                # Safe timestamp for filename
                try:
                    # get_local_date_from_utc_date expects a single UTC timestamp string
                    local_dt_str = get_local_date_from_utc_date(str(row['created_at']))
                    ts = pd.to_datetime(local_dt_str, errors='coerce')
                    ts = ts.strftime("%Y%m%d_%H%M%S") if isinstance(ts, pd.Timestamp) else "unknown"
                except Exception as e:
                    logging.warning(f"[DOWNLOAD] Failed to format timestamp for ID {pid}: {e}")
                    ts = "unknown"

                # Original image bytes from FS or DB
                orig_bytes = None
                # Try filesystem path
                try:
                    fp = os.path.join(pictures_original_dir(), f"{pid}.jpg")
                    if os.path.exists(fp):
                        with open(fp, "rb") as f:
                            orig_bytes = f.read()
                except Exception as e:
                    logging.warning(f"[DOWNLOAD] Failed reading original file for ID {pid}: {e}")
                # Fallback to DB blob if present
                if orig_bytes is None:
                    ob = row.get('original_image')
                    if isinstance(ob, (bytes, bytearray)) and len(ob) > 0:
                        orig_bytes = bytes(ob)

                # Add original if present
                if isinstance(orig_bytes, (bytes, bytearray)) and len(orig_bytes) > 0:
                    files.append((f"{pid}_{ts}.jpg", bytes(orig_bytes)))

        # If we have no files, add a minimal ZIP with README
        if len(files) == 0:
            logging.warning(f"[DOWNLOAD] No images available for block_id {block_id}. Returning placeholder ZIP.")
            readme = (
                "Kittyhack event download\n"
                f"block_id: {block_id}\n\n"
                "No images are available for this event.\n"
                "- They may have been deleted due to retention limits,\n"
                "- or migrated but missing on disk,\n"
                "- or were never stored.\n"
            ).encode("utf-8")
            files.append(("README.txt", readme))

        # Write ZIP to a temp file and return its path
        tmp_dir = "/tmp"
        zip_path = os.path.join(tmp_dir, f"kittyhack_event_{block_id}_{int(tm.time())}.zip")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, data in files:
                zf.writestr(name, data)

        return zip_path
    
    @reactive.effect
    @reactive.event(input.btn_play_pause)
    def play_pause():
        # Toggle play/pause based on the click count
        new_state = not bool(slideshow_running.get())
        slideshow_running.set(new_state)

        # When resuming playback, allow the next img_loaded_pulse for the current frame
        # to advance the slideshow (avoid getting stuck due to last_pid debounce).
        if new_state:
            try:
                setattr(_advance_slideshow_when_loaded, "_last_pid", -1)
            except Exception:
                pass

        # When switching into paused mode, align the scrubber to the current frame.
        if not new_state:
            try:
                if pictures and len(pictures) > 1:
                    _suppress_next_scrubber_events(1)
                    ui.update_slider(
                        "event_scrubber",
                        value=int(frame_index[0] or 0) + 1,
                        min=1,
                        max=len(pictures),
                    )
            except Exception:
                pass

    @reactive.effect
    @reactive.event(input.btn_toggle_overlay)
    def toggle_overlay():
        # Toggle the overlay visibility based on the click count
        if input.btn_toggle_overlay() % 2 == int(CONFIG['SHOW_IMAGES_WITH_OVERLAY']):
            ui.update_action_button("btn_toggle_overlay", label="", icon=icon_svg('border-none', margin_left="0", margin_right="0"))
        else:
            ui.update_action_button("btn_toggle_overlay", label="", icon=icon_svg('border-all', margin_left="0", margin_right="0"))

    @reactive.effect
    @reactive.event(input.btn_prev)
    def prev_picture():
        if slideshow_running.get():
            return
        if not pictures:
            return
        frame_index[0] = (int(frame_index[0]) - 1) % max(len(pictures), 1)
        _bump_frame_tick()

        # Keep scrubber aligned without triggering on_scrub_event.
        try:
            if len(pictures) > 1:
                _suppress_next_scrubber_events(1)
                ui.update_slider("event_scrubber", value=int(frame_index[0]) + 1)
        except Exception:
            pass

    @reactive.effect
    @reactive.event(input.btn_next)
    def next_picture():
        if slideshow_running.get():
            return
        if not pictures:
            return
        frame_index[0] = (int(frame_index[0]) + 1) % max(len(pictures), 1)
        _bump_frame_tick()

        # Keep scrubber aligned without triggering on_scrub_event.
        try:
            if len(pictures) > 1:
                _suppress_next_scrubber_events(1)
                ui.update_slider("event_scrubber", value=int(frame_index[0]) + 1)
        except Exception:
            pass

    @reactive.effect
    @reactive.event(input.event_scrubber)
    def on_scrub_event():
        # Ignore scrubber changes that are caused by server-side updates/re-renders.
        try:
            pending = int(scrubber_suppress.get() or 0)
        except Exception:
            pending = 0
        if pending > 0:
            try:
                scrubber_suppress.set(max(0, pending - 1))
            except Exception:
                pass

            # If we're playing, this is almost certainly a programmatic update.
            # If we're paused, a stale suppress counter can happen when the server calls
            # ui.update_slider() but the browser doesn't emit an input event (same value).
            # In paused mode we continue and rely on the target==current guard.
            if bool(slideshow_running.get()):
                return

        # If we're playing, the server may be updating the slider programmatically each frame.
        # Those updates can still trigger an input event; do not treat them as user scrubs.
        # We allow large jumps (delta > 1) to act as user-initiated seeks (auto-pausing).

        if not pictures or len(pictures) == 0:
            return

        try:
            # Slider is 1-based
            target_idx = int(input.event_scrubber()) - 1
        except Exception:
            return

        # Compute current index for delta heuristics.
        try:
            current_idx = int(frame_index[0] or 0)
        except Exception:
            current_idx = 0

        is_playing_now = bool(slideshow_running.get())
        delta = abs(int(target_idx) - int(current_idx))

        # Track last seen (1-based) for the fallback watcher.
        try:
            last_scrubber_seen[0] = int(target_idx) + 1
        except Exception:
            pass

        if is_playing_now and delta <= 1:
            return

        target_idx = max(0, min(len(pictures) - 1, target_idx))

        try:
            logging.debug(
                f"Scrubber clicked to frame {int(target_idx) + 1}/{len(pictures)} (block_id={block_id}, playing={1 if is_playing_now else 0}, suppress={pending}, delta={delta})"
            )
        except Exception:
            pass

        _handle_scrub_to(target_idx, source="event")

    @reactive.effect
    def _scrubber_value_fallback_watch():
        """Fallback: ensure seeking works even if the explicit input event is flaky.

        If the input value changes while paused, seek to it.
        """
        if bool(slideshow_running.get()):
            return
        if not pictures:
            return

        try:
            v1 = int(input.event_scrubber() or 0)
        except Exception:
            return
        if v1 <= 0:
            return

        # Ignore programmatic updates
        try:
            pending = int(scrubber_suppress.get() or 0)
        except Exception:
            pending = 0
        if pending > 0:
            try:
                scrubber_suppress.set(max(0, pending - 1))
            except Exception:
                pass
            last_scrubber_seen[0] = v1
            return

        if last_scrubber_seen[0] is None:
            last_scrubber_seen[0] = v1
            return
        if int(last_scrubber_seen[0]) == v1:
            return

        last_scrubber_seen[0] = v1
        target_idx = v1 - 1

        try:
            logging.info(
                f"DEBUG: Scrubber value changed to frame {int(target_idx) + 1}/{len(pictures)} (fallback watcher, block_id={block_id})"
            )
        except Exception:
            pass

        _handle_scrub_to(target_idx, source="watch")

@module.server
def wlan_connect_server(input, output, session, ssid: str):
    @reactive.effect
    @reactive.event(input.btn_wlan_connect)
    def wlan_connect():
        _set_wlan_action_in_progress(True)
        ui.modal_show(
            ui.modal(
                _("The WLAN connection will be interrupted now!"),
                ui.br(),
                _("Please wait a few seconds. If the page does not reload automatically within 30 seconds, please reload it manually."),
                title=_("Updating WLAN configuration..."),
                footer=None
            )
        )
        try:
            switch_wlan_connection(ssid)
            reload_trigger_wlan.set(reload_trigger_wlan.get() + 1)
        finally:
            _set_wlan_action_in_progress(False)
            ui.modal_remove()

@module.server
def wlan_modify_server(input, output, session, ssid: str):
    @reactive.effect
    @reactive.event(input.btn_wlan_modify)
    def wlan_modify():
        # We need to read the configured wlans again here to get the current connection status
        configured_wlans = get_wlan_connections()
        wlan = next((w for w in configured_wlans if w['ssid'] == ssid), None)
        if wlan is None:
            logging.error(f"WLAN with SSID {ssid} not found.")
            return
        connected = wlan['connected']
        if connected:
            additional_note = ui.div(
                _("This WLAN is currently connected. You can not delete it or change the password."),
                class_="alert alert-info",
                style_="margin-bottom: 0px; margin-top: 10px; padding: 10px"
            )
        else:
            additional_note = ""

        m = ui.modal(
            ui.div(ui.input_text("txtWlanSSID", _("SSID"), wlan['ssid']), class_="disabled-wrapper"),
            ui.div(ui.input_password("txtWlanPassword", _("Password"), "", placeholder=_("Leave empty to keep the current password")), class_="disabled-wrapper" if connected else ""),
            ui.input_numeric("numWlanPriority", _("Priority"), wlan['priority'], min=0, max=100, step=1),
            ui.help_text(_("The priority determines the order in which the WLANs are tried to connect. Higher numbers are tried first.")),
            additional_note,
            title=_("Change WLAN configuration"),
            easy_close=False,
            footer=ui.div(
                ui.input_action_button(
                    id="btn_wlan_save",
                    label=_("Save"),
                    class_="btn-vertical-margin btn-narrow"
                ),
                ui.input_action_button(
                    id="btn_modal_cancel",
                    label=_("Cancel"),
                    class_="btn-vertical-margin btn-narrow"
                ),
                ui.input_action_button(
                    id=f"btn_wlan_delete", 
                    label=_("Delete"), 
                    icon=icon_svg("trash"), 
                    class_=f"btn-vertical-margin btn-narrow btn-danger {'disabled-wrapper' if connected else ''}"
                ),
            )
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_wlan_save)
    def wlan_save():
        _set_wlan_action_in_progress(True)
        ssid = input.txtWlanSSID()
        password = input.txtWlanPassword()
        priority = input.numWlanPriority()
        password_changed = True if password else False
        logging.info(f"Updating WLAN configuration: SSID={ssid}, Priority={priority}, Password changed={password_changed}")
        ui.modal_remove()
        ui.modal_show(
            ui.modal(
                _("The WLAN connection will be interrupted now!"),
                ui.br(),
                _("Please wait a few seconds. If the page does not reload automatically within 30 seconds, please reload it manually."),
                title=_("Updating WLAN configuration..."),
                footer=None
            )
        )
        try:
            success = manage_and_switch_wlan(ssid, password, priority, password_changed)
            if success:
                ui.notification_show(_("WLAN configuration for {} updated successfully.").format(ssid), duration=5, type="message")
                reload_trigger_wlan.set(reload_trigger_wlan.get() + 1)
            else:
                ui.notification_show(_("Failed to update WLAN configuration for {}").format(ssid), duration=10, type="error")
        finally:
            _set_wlan_action_in_progress(False)
            ui.modal_remove()

    @reactive.effect
    @reactive.event(input.btn_modal_cancel)
    def modal_cancel():
        ui.modal_remove()

    @reactive.effect
    @reactive.event(input.btn_wlan_delete)
    def wlan_delete():
        ssid = input.txtWlanSSID()
        success = delete_wlan_connection(ssid)
        if success:
            ui.notification_show(_("WLAN connection {} deleted successfully.").format(ssid), duration=5, type="message")
            ui.modal_remove()
            reload_trigger_wlan.set(reload_trigger_wlan.get() + 1)
        else:
            ui.notification_show(_("Failed to delete WLAN connection {}").format(ssid), duration=10, type="error")

@module.server
def manage_yolo_model_server(input, output, session, unique_id: str):
    @reactive.effect
    @reactive.event(input.btn_yolo_modify)
    def on_modify_yolo_model():
        # We need to read the available models again here to get the metadata
        available_models = YoloModel.get_model_list()
        model = next((mdl for mdl in available_models if mdl['unique_id'] == unique_id), None)
        if model is None:
            logging.error(f"YOLO model with unique_id {unique_id} not found.")
            return
        model_in_use = model['unique_id'] == CONFIG['YOLO_MODEL']
        if model_in_use:
            additional_note = ui.div(
                _("This model is currently in use. You can not delete it."),
                class_="alert alert-info",
                style_="margin-bottom: 0px; margin-top: 10px; padding: 10px"
            )
        else:
            additional_note = ""

        m = ui.modal(
            ui.div(ui.input_text("txtModelName", _("Name"), model['display_name'], width="100%"), style_="width: 100%;"),
            ui.div(ui.input_text("txtModelUniqueID", _("Unique ID"), model['unique_id'], width="100%"), class_="disabled-wrapper", style_="width: 100%;"),
            ui.hr(),
            ui.markdown(_("Model created at: {}").format(model['creation_date'])),
            additional_note,
            title=_("Change model configuration"),
            easy_close=False,
            footer=ui.div(
                ui.input_action_button(
                    id="btn_model_save",
                    label=_("Save"),
                    class_="btn-vertical-margin btn-narrow"
                ),
                ui.input_action_button(
                    id="btn_modal_cancel",
                    label=_("Cancel"),
                    class_="btn-vertical-margin btn-narrow"
                ),
                ui.input_action_button(
                    id=f"btn_model_delete", 
                    label=_("Delete"), 
                    icon=icon_svg("trash"), 
                    class_=f"btn-vertical-margin btn-narrow btn-outline-danger {'disabled-wrapper' if model_in_use else ''}"
                ),
            )
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_model_save)
    def model_save():
        unique_id = input.txtModelUniqueID()
        model_name = input.txtModelName()
        logging.info(f"Updating Model configuration for Unique ID {unique_id}: Name={model_name}")
        ui.modal_remove()
        success = YoloModel.rename_model(unique_id, model_name)
        if success:
            ui.notification_show(_("Model configuration {} updated successfully.").format(model_name), duration=5, type="message")
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)
            reload_trigger_config.set(reload_trigger_config.get() + 1)
        else:
            ui.notification_show(_("Failed to update model configuration {}").format(model_name), duration=10, type="error")
        ui.modal_remove()

    @reactive.effect
    @reactive.event(input.btn_modal_cancel)
    def modal_cancel():
        ui.modal_remove()

    # Ask the user for confirmation before deleting the model
    @reactive.effect
    @reactive.event(input.btn_model_delete)
    def model_delete():
        # First remove the previous modal correctly to avoid multiple modals
        unique_id = input.txtModelUniqueID()
        model_name = input.txtModelName()
        ui.modal_remove()

        m = ui.modal(
            _("Do you really want to delete this model?"),
            title=_("Delete Model"),
            easy_close=False,
            footer=ui.div(
                ui.div(
                    ui.input_action_button("btn_modal_delete_model_ok", _("OK")),
                    ui.input_action_button("btn_modal_cancel", _("Cancel")),
                ),
                ui.div(
                    # Helper input to pass the unique_id to the delete function
                    ui.input_text("txtModelName", "", model_name),
                    ui.input_text("txtModelUniqueID", "", unique_id),
                    style_="display:none;"
                )
            ), 
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_modal_delete_model_ok)
    def modal_delete_model_ok():
        unique_id = input.txtModelUniqueID()
        success = YoloModel.delete_model(unique_id)
        if success:
            ui.notification_show(_("Model {} deleted successfully.").format(input.txtModelName()), duration=5, type="message")
            ui.modal_remove()
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)
            reload_trigger_config.set(reload_trigger_config.get() + 1)
        else:
            ui.notification_show(_("Failed to delete Model {}").format(input.txtModelName()), duration=10, type="error")
        ui.modal_remove()


@module.server
def activate_yolo_model_server(input, output, session, unique_id: str):
    @reactive.effect
    @reactive.event(input.btn_yolo_activate)
    def on_activate_yolo_model():
        global model_handler

        if not (unique_id or "").strip():
            ui.notification_show(_("This model cannot be selected because it has no unique ID."), duration=8, type="error")
            return

        if CONFIG.get('YOLO_MODEL') == unique_id and not CONFIG.get('TFLITE_MODEL_VERSION'):
            ui.notification_show(_("This model is already active."), duration=5, type="message")
            return

        model_path = YoloModel.get_model_path(unique_id)
        if not model_path:
            ui.notification_show(_("The selected model was not found on disk."), duration=8, type="error")
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)
            reload_trigger_config.set(reload_trigger_config.get() + 1)
            return

        CONFIG['YOLO_MODEL'] = unique_id
        CONFIG['TFLITE_MODEL_VERSION'] = ""
        update_single_config_parameter("YOLO_MODEL")
        update_single_config_parameter("TFLITE_MODEL_VERSION")

        reload_ok, active_model_handler = reload_model_handler_runtime()
        if reload_ok:
            model_handler = active_model_handler
            ui.notification_show(_("Model switched successfully."), duration=6, type="message")
        else:
            ui.notification_show(
                _("Model was selected, but live reload failed. Please reboot to apply the change."),
                duration=10,
                type="warning",
            )

        reload_trigger_ai.set(reload_trigger_ai.get() + 1)
        reload_trigger_config.set(reload_trigger_config.get() + 1)

#######################################################################
# The main server application
#######################################################################
def server(input, output, session):

    # Create reactive triggers
    reload_trigger_cats = reactive.Value(0)
    reload_trigger_info = reactive.Value(0)

    # Hold last uploaded file paths until user confirms restore
    last_uploaded_db_path = reactive.Value(None)
    last_uploaded_cfg_path = reactive.Value(None)
    remote_setup_active = reactive.Value(bool(is_remote_mode() and remote_setup_required))

    def _remote_synced_marker_path() -> str:
        # Written by RemoteControlClient when a sync tar.gz was extracted successfully.
        return str(CONFIG.get("KITTYHACK_DATABASE_PATH", "kittyhack.db")) + ".remote_synced"

    def _remote_restart_pending_marker_path() -> str:
        # Marker to persist "restart required" across browser refreshes.
        return str(CONFIG.get("KITTYHACK_DATABASE_PATH", "kittyhack.db")) + ".remote_restart_pending"

    remote_sync_status = reactive.Value({})
    remote_sync_modal_open = reactive.Value(False)
    remote_restart_modal_open = reactive.Value(False)
    remote_sync_finalized = reactive.Value(False)

    # Live status for overlay in live view
    live_status = reactive.Value(
        {
            "ok": False,
            "ts": "",
            "remote_waiting": False,
            "inside_lock": False,
            "outside_lock": False,
            "inside_motion": False,
            "outside_motion": False,
            "forced_lock_due_prey": False,
            "time_until_release": 0.0,
            "delta_to_last_prey_detection": 0.0,
        }
    )
    live_view_warning_html = reactive.Value("")
    live_view_warning_dismissed = reactive.Value(False)
    live_view_warning_signature = reactive.Value("")

    @reactive.effect
    def update_live_view_warning_html():
        # Keep warning rendering independent from live image rendering.
        __ = live_view_refresh_nonce.get()

        refresh_s = float(CONFIG.get('LIVE_VIEW_REFRESH_INTERVAL', 2.0) or 2.0)
        refresh_s = max(0.25, refresh_s)

        warning_html = ""
        warning_signature = ""
        if (not is_remote_mode()) and CONFIG.get("CAMERA_SOURCE") == "ip_camera":
            try:
                res = model_handler.get_camera_resolution()
                if res and isinstance(res, (tuple, list)) and len(res) == 2:
                    width, height = int(res[0]), int(res[1])
                    if width > 0 and height > 0 and (width * height > 1280 * 720):
                        warning_signature = f"ip_camera:{width}x{height}"
                        warning_html = str(
                            ui.div(
                                ui.div(
                                    icon_svg("triangle-exclamation", margin_left="0", margin_right="0.2em"),
                                    _("Warning") + ": "
                                    + _("Your IP camera resolution is higher than recommended (max. 1280x720).")
                                    + " "
                                    + _(
                                        "Current: {width}x{height}. This may have negative effects on performance."
                                    ).format(width=width, height=height),
                                    class_="generic-container warning-container",
                                ),
                                style_="text-align: center;",
                            )
                        )
            except Exception as e:
                logging.error(f"Failed to check IP camera resolution for warning: {e}")

        # Reset dismiss state when the warning context changes (e.g. resolution changed).
        try:
            prev_signature = live_view_warning_signature.get()
        except Exception:
            prev_signature = ""
        if prev_signature != warning_signature:
            live_view_warning_signature.set(warning_signature)
            live_view_warning_dismissed.set(False)

        # Respect user dismissal while warning context stays unchanged.
        if warning_html and bool(live_view_warning_dismissed.get()):
            warning_html = ""

        try:
            prev_warning_html = live_view_warning_html.get()
            if prev_warning_html != warning_html:
                # Log only on state changes to avoid log spam.
                if warning_html:
                    logging.info("[LIVE_VIEW] IP camera resolution warning enabled.")
                elif prev_warning_html:
                    logging.info("[LIVE_VIEW] IP camera resolution warning cleared.")
                live_view_warning_html.set(warning_html)
        except Exception:
            live_view_warning_html.set(warning_html)

        reactive.invalidate_later(refresh_s)

    @reactive.Effect
    @reactive.event(input.btn_dismiss_live_view_warning)
    def dismiss_live_view_warning():
        live_view_warning_dismissed.set(True)
        live_view_warning_html.set("")

    def show_user_notifications():
        user_notifications = UserNotifications.get_all()
        if len(user_notifications) > 0:
            # Create a combined message from all notifications
            combined_message = ""
            for i, notification in enumerate(user_notifications):
                combined_message += f"## {notification['header']}\n\n{notification['message']}"
                # Add a separator between notifications, but not after the last one
                if i < len(user_notifications) - 1:
                    combined_message += "\n\n---\n\n"
                UserNotifications.remove(notification['id'])
            
            # Show all notifications in a single modal
            ui.modal_show(
                ui.modal(
                    ui.div(
                        ui.markdown(combined_message),
                    ),
                    title=_("Notifications"),
                    easy_close=False,
                    size="lg",
                    footer=ui.div(
                        ui.input_action_button("btn_modal_cancel", _("Close")),
                    )
                )
            )

    @output
    @render.ui
    def ui_remote_connection_badge():
        # Navbar indicator: show if we are in remote-mode and currently connected to the target.
        if not is_remote_mode():
            return ui.HTML("")

        reactive.invalidate_later(1.0)

        host = (CONFIG.get("REMOTE_TARGET_HOST") or "").strip()
        if not host:
            return ui.tags.span(
                {"class": "badge rounded-pill text-bg-secondary", "title": _("Remote target host not configured")},
                _("Remote: not configured"),
            )

        try:
            from src.remote.control_client import RemoteControlClient

            client = RemoteControlClient.instance()
            client.ensure_started()
            is_ready = bool(client.wait_until_ready(timeout=0))
        except Exception:
            is_ready = False

        if is_ready:
            return ui.tags.span(
                {"class": "badge rounded-pill text-bg-success", "title": _("Connected to Kittyflap") + f": {host}"},
                _("Remote: connected"),
            )

        # Connecting / reconnecting
        return ui.tags.span(
            {"class": "badge rounded-pill text-bg-warning", "title": _("Connecting to Kittyflap") + f": {host}"},
            ui.tags.span(
                {"class": "spinner-border spinner-border-sm me-1", "role": "status", "aria-hidden": "true"}
            ),
            _("Remote: connecting"),
        )

    def show_remote_setup_modal():
        if not (is_remote_mode() and remote_setup_required):
            return

        # If a restart is pending (e.g. user refreshed after sync), block with restart modal.
        try:
            if os.path.exists(_remote_restart_pending_marker_path()):
                show_remote_restart_required_modal()
                return
        except Exception:
            pass

        # If an initial sync is currently in progress (or pending), show the sync modal instead
        # of the settings form. This also covers browser refresh during sync.
        if bool(CONFIG.get("REMOTE_SYNC_ON_FIRST_CONNECT", True)):
            try:
                from src.remote.control_client import RemoteControlClient

                client = RemoteControlClient.instance()
                client.ensure_started()
                st = client.get_sync_status()
                in_progress = bool(st.get("in_progress"))
                requested = bool(st.get("requested"))
                ok = st.get("ok")
                if requested and (in_progress or ok is None):
                    remote_sync_status.set(st)
                    show_remote_initial_sync_modal()
                    remote_sync_modal_open.set(True)
                    return
            except Exception:
                pass

        values = read_remote_config_values()
        ui.modal_show(
            ui.modal(
                ui.div(
                    ui.markdown(
                        _("Remote-mode setup is required before Kittyhack can start.")
                        + "\n\n"
                        + _("After saving, an initial sync can take several minutes (depending on pictures/models size).")
                        + "\n"
                        + _("Please keep this browser tab open and do not close or reload it until synchronization is finished.")
                        + "\n"
                        + _("Startup continues after a successful sync and service restart.")
                    ),
                    ui.input_text(
                        "remote_target_host",
                        _("Remote target host IP:"),
                        value=values["remote_target_host"],
                        width="100%"
                    ),
                    ui.input_switch(
                        "remote_sync_on_first_connect",
                        _("Sync on first connect"),
                        values["remote_sync_on_first_connect"]
                    ),
                    ui.input_switch(
                        "remote_sync_labelstudio",
                        _("Sync Label Studio user data"),
                        values.get("remote_sync_labelstudio", True),
                    ),
                ),
                title=_("Remote-mode setup"),
                easy_close=False,
                footer=ui.div(
                    ui.input_action_button("btn_remote_setup_save", _("Save settings"))
                ),
                size="lg",
            )
        )

    def show_remote_initial_sync_modal(error_reason: str | None = None):
        body = ui.div(
            ui.div(
                ui.tags.div(
                    {
                        "class": "spinner-border text-primary",
                        "role": "status",
                    },
                    ui.tags.span({"class": "visually-hidden"}, _("Loading...")),
                ),
                class_="d-flex align-items-center gap-3",
            ),
            ui.div(ui.output_ui("remote_initial_sync_status_ui"), class_="mt-3"),
        )

        footer = ui.div(
            ui.input_action_button("btn_remote_sync_abort", _("Abort"), class_="btn-danger")
        )
        if error_reason:
            body = ui.div(
                ui.markdown(_("Initial sync failed.")),
                ui.markdown(error_reason),
                ui.div(ui.output_ui("remote_initial_sync_status_ui"), class_="mt-3"),
            )
            footer = ui.div(
                ui.input_action_button("btn_remote_sync_failed_close", _("Back")),
                ui.input_action_button("btn_remote_sync_abort", _("Abort"), class_="btn-danger")
            )

        ui.modal_show(
            ui.modal(
                body,
                title=_("Initial sync"),
                easy_close=False,
                footer=footer,
                size="lg",
            )
        )

    def show_remote_restart_required_modal():
        ui.modal_show(
            ui.modal(
                ui.div(
                    ui.markdown(
                        _("Initial sync has completed.")
                        + "\n\n"
                        + _("To fully apply all settings, the Kittyhack service must be restarted now.")
                        + "\n"
                        + _("The web interface will be briefly unavailable during the restart.")
                    )
                ),
                title=_("Restart required"),
                easy_close=False,
                footer=ui.div(
                    ui.input_action_button("btn_remote_restart_kittyhack", _("Restart Kittyhack service"))
                ),
                size="lg",
            )
        )

    if is_remote_mode() and remote_setup_required:
        show_remote_setup_modal()

    # If a restart is pending (e.g. browser refreshed after sync), show the restart modal.
    if is_remote_mode():
        try:
            if os.path.exists(_remote_restart_pending_marker_path()):
                show_remote_restart_required_modal()
                remote_restart_modal_open.set(True)
        except Exception:
            pass

    @output
    @render.ui
    def remote_initial_sync_status_ui():
        st = remote_sync_status.get() or {}
        requested = bool(st.get("requested"))
        in_progress = bool(st.get("in_progress"))
        ok = st.get("ok")
        reason = (st.get("reason") or "").strip()
        bytes_mb = float(st.get("bytes_received") or 0) / (1024.0 * 1024.0)
        items = st.get("items") or []
        items_count = len(items) if isinstance(items, list) else 0

        if not requested:
            headline = _("Waiting for sync to start...")
        elif in_progress:
            headline = _("Initial sync in progress...")
        elif ok is True:
            headline = _("Initial sync completed.")
        elif ok is False:
            headline = _("Initial sync failed.")
        else:
            headline = _("Connecting to Kittyflap...")

        detail = _("Transferred: {:.1f} MB").format(bytes_mb)
        if items_count:
            detail = _("Transferred ({} items): {:.1f} MB").format(items_count, bytes_mb)

        extra = ""
        if requested and ok is False and reason:
            extra = _("Reason: {} ").format(reason)

        return ui.div(
            ui.markdown(f"**{headline}**"),
            ui.div(detail),
            ui.div(extra) if extra else ui.HTML(""),
        )

    @reactive.Effect
    def _remote_sync_poller():
        # Poll RemoteControlClient to keep the modal state in sync and survive refreshes.
        if not is_remote_mode():
            return

        # Always keep restart marker-driven flow visible.
        try:
            if os.path.exists(_remote_restart_pending_marker_path()):
                if not bool(remote_restart_modal_open.get()):
                    show_remote_restart_required_modal()
                    remote_restart_modal_open.set(True)
                return
        except Exception:
            pass

        if not bool(remote_setup_active.get()):
            return
        if not bool(CONFIG.get("REMOTE_SYNC_ON_FIRST_CONNECT", True)):
            return

        try:
            from src.remote.control_client import RemoteControlClient
            client = RemoteControlClient.instance()
            client.ensure_started()
            st = client.get_sync_status()
        except Exception:
            reactive.invalidate_later(1.0)
            return

        remote_sync_status.set(st)
        requested = bool(st.get("requested"))
        in_progress = bool(st.get("in_progress"))
        ok = st.get("ok")

        # If a sync was requested (even if not yet in_progress), show modal.
        if requested and (in_progress or ok is None) and not bool(remote_sync_modal_open.get()):
            ui.modal_remove()
            show_remote_initial_sync_modal()
            remote_sync_modal_open.set(True)

        # Timeouts (best effort): if requested but never finishes, mark as failed.
        try:
            timeout_s = float(CONFIG.get("REMOTE_CONTROL_TIMEOUT") or 10.0)
        except Exception:
            timeout_s = 10.0
        connect_timeout = max(20.0, min(120.0, float(timeout_s or 10.0) * 4.0))
        sync_timeout = max(180.0, float(timeout_s or 10.0) * 120.0)
        try:
            requested_at = float(st.get("requested_at") or 0.0)
            started_at = float(st.get("started_at") or 0.0)
            requested_at_mono = float(st.get("requested_at_mono") or 0.0)
            started_at_mono = float(st.get("started_at_mono") or 0.0)
        except Exception:
            requested_at = 0.0
            started_at = 0.0
            requested_at_mono = 0.0
            started_at_mono = 0.0

        if requested_at_mono:
            now_mono = monotonic_time()
            age = max(0.0, now_mono - requested_at_mono)
        else:
            now_wall = tm.time()
            age = max(0.0, now_wall - requested_at) if requested_at else 0.0

        if started_at_mono:
            now_mono = monotonic_time()
            sync_age = max(0.0, now_mono - started_at_mono)
        else:
            now_wall = tm.time()
            sync_age = max(0.0, now_wall - started_at) if started_at else 0.0

        if requested and ok is None and not in_progress and age > connect_timeout:
            show_remote_initial_sync_modal(error_reason=_("Could not connect to the remote Kittyflap."))
            remote_sync_modal_open.set(True)
            reactive.invalidate_later(2.0)
            return

        if requested and ok is None and (in_progress or started_at) and sync_age > sync_timeout:
            show_remote_initial_sync_modal(error_reason=_("Initial sync timed out."))
            remote_sync_modal_open.set(True)
            reactive.invalidate_later(2.0)
            return

        # Sync finished.
        if requested and not in_progress and ok is True and not bool(remote_sync_finalized.get()):
            remote_sync_finalized.set(True)

            # Sync may have replaced config.ini: reload it now so next startup uses the synced values.
            try:
                load_config()
            except Exception as e:
                ui.notification_show(_("Failed to reload synced config.ini: {}.").format(e), duration=12, type="error")
                return

            # Trigger immediate UI refresh after synced DB/config are applied.
            try:
                reload_trigger_photos.set(reload_trigger_photos.get() + 1)
                reload_trigger_cats.set(reload_trigger_cats.get() + 1)
                reload_trigger_config.set(reload_trigger_config.get() + 1)
                reload_trigger_info.set(reload_trigger_info.get() + 1)
            except Exception:
                pass

            # Mark "restart required" to persist across refresh.
            try:
                with open(_remote_restart_pending_marker_path(), "w", encoding="utf-8") as f:
                    f.write(str(tm.time()))
            except Exception:
                pass

            global remote_setup_required
            remote_setup_required = False
            remote_setup_active.set(False)
            ui.modal_remove()
            show_remote_restart_required_modal()
            remote_restart_modal_open.set(True)
            return

        if requested and not in_progress and ok is False:
            reason = (st.get("reason") or "").strip()
            show_remote_initial_sync_modal(error_reason=reason or _("unknown error"))
            remote_sync_modal_open.set(True)
            reactive.invalidate_later(2.0)
            return

        reactive.invalidate_later(0.25)

    @reactive.Effect
    @reactive.event(input.btn_remote_setup_save)
    def on_remote_setup_save():
        if not (is_remote_mode() and remote_setup_active.get()):
            return
        host = (input.remote_target_host() or "").strip()
        if not host:
            ui.notification_show(_("Remote target host must not be empty."), duration=8, type="error")
            return
        port = 8888
        try:
            timeout = float(read_remote_config_values().get("remote_control_timeout", 30.0) or 30.0)
        except Exception:
            timeout = 30.0
        if timeout <= 0:
            timeout = 30.0

        sync_first = bool(input.remote_sync_on_first_connect())
        sync_labelstudio = bool(input.remote_sync_labelstudio())
        remote_cfg_path = os.path.join(kittyhack_root(), "config.remote.ini")
        parser = configparser.ConfigParser()
        parser["Settings"] = {
            "remote_target_host": host,
            "remote_control_port": str(port),
            "remote_control_timeout": str(timeout),
            "remote_sync_on_first_connect": str(sync_first),
            "remote_sync_labelstudio": str(sync_labelstudio),
        }
        try:
            with open(remote_cfg_path, "w", encoding="utf-8") as f:
                parser.write(f)
        except Exception as e:
            ui.notification_show(_("Failed to write config.remote.ini: {}").format(e), duration=10, type="error")
            return

        CONFIG["REMOTE_TARGET_HOST"] = host
        CONFIG["REMOTE_CONTROL_PORT"] = port
        CONFIG["REMOTE_CONTROL_TIMEOUT"] = timeout
        CONFIG["REMOTE_SYNC_ON_FIRST_CONNECT"] = sync_first
        CONFIG["REMOTE_SYNC_LABELSTUDIO"] = sync_labelstudio

        if sync_first:
            # Start sync (non-blocking). Progress is shown in a non-closable modal and
            # survives browser refresh by polling the RemoteControlClient status.
            try:
                from src.remote.control_client import RemoteControlClient

                client = RemoteControlClient.instance()
                client.ensure_started()
                client.start_initial_sync(force=True)
                remote_sync_status.set(client.get_sync_status())
            except Exception as e:
                ui.notification_show(_("Failed to initialize remote control client: {}.").format(e), duration=12, type="error")
                return

            ui.modal_remove()
            show_remote_initial_sync_modal()
            remote_sync_modal_open.set(True)
            return

        global remote_setup_required
        remote_setup_required = False
        remote_setup_active.set(False)
        ui.modal_remove()
        ui.notification_show(_("Remote-mode configuration saved. Starting services..."), duration=8, type="message")

        start_backend_if_needed()
        start_background_task_if_needed()

    @reactive.Effect
    @reactive.event(input.btn_remote_sync_failed_close)
    def _remote_sync_failed_close():
        # Allow user to go back to the remote setup form after a sync failure.
        if not is_remote_mode():
            return
        remote_sync_modal_open.set(False)
        ui.modal_remove()
        show_remote_setup_modal()

    @reactive.Effect
    @reactive.event(input.btn_remote_sync_abort)
    def _remote_sync_abort():
        if not is_remote_mode():
            return

        try:
            from src.remote.control_client import RemoteControlClient

            client = RemoteControlClient.instance()
            client.abort_initial_sync(reason="aborted by user")
        except Exception:
            pass

        # Clear target host and persist cleanup in config.remote.ini/config.ini.
        CONFIG["REMOTE_TARGET_HOST"] = ""
        try:
            update_single_config_parameter("REMOTE_TARGET_HOST")
        except Exception:
            pass

        remote_sync_status.set(
            {
                "requested": False,
                "in_progress": False,
                "ok": False,
                "reason": "aborted by user",
                "requested_at": 0.0,
                "started_at": 0.0,
                "finished_at": tm.time(),
                "bytes_received": 0,
                "items": [],
            }
        )
        remote_sync_modal_open.set(False)
        remote_sync_finalized.set(False)
        remote_setup_active.set(True)

        ui.modal_remove()
        ui.notification_show(_("Initial sync aborted. Remote target host has been cleared."), duration=8, type="message")
        show_remote_setup_modal()

    @reactive.Effect
    @reactive.event(input.btn_remote_restart_kittyhack)
    def _remote_restart_after_sync():
        if not is_remote_mode():
            return
        # Best effort: clear marker before restart so it doesn't reappear after restart.
        try:
            if os.path.exists(_remote_restart_pending_marker_path()):
                os.remove(_remote_restart_pending_marker_path())
        except Exception:
            pass

        ui.notification_show(_("Restarting Kittyhack service..."), duration=10, type="message")

        def _do_restart():
            try:
                tm.sleep(0.75)
                systemctl("restart", "kittyhack")
            except Exception:
                pass

        threading.Thread(target=_do_restart, daemon=True).start()
        # The service restart will typically terminate this process shortly.
        ui.modal_remove()
    
    # Show a notification if a new version of Kittyhack is available
    if CONFIG['LATEST_VERSION'] != "unknown" and CONFIG['LATEST_VERSION'] != git_version and CONFIG['PERIODIC_VERSION_CHECK']:
        ui.notification_show(_("A new version of Kittyhack is available: {}. Go to the [INFO] section for update instructions.").format(CONFIG['LATEST_VERSION']), duration=10, type="message")

    # Show a warning if the remaining disk space is below the critical threshold
    kittyflap_db_file_exists = os.path.exists(CONFIG['DATABASE_PATH'])
    if free_disk_space < 500:
        if kittyflap_db_file_exists:
            additional_info = _(" or consider deleting pictures from the original kittyflap database file. For more details, see the [INFO] section.")
        else:
            additional_info = ""
        ui.notification_show(_("Remaining disk space is low: {:.1f} MB. Please free up some space (e.g. reduce the max amount of pictures in the database{}).").format(free_disk_space, additional_info), duration=20, type="warning")
    
    # Add new WLAN dialog
    def wlan_add_dialog():
        m = ui.modal(
            ui.div(ui.input_text("txtWlanSSID", _("SSID"), "")),
            ui.div(ui.input_password("txtWlanPassword", _("Password"), "")),
            ui.input_numeric("numWlanPriority", _("Priority"), 0, min=0, max=100, step=1),
            ui.help_text(_("The priority determines the order in which the WLANs are tried to connect. Higher numbers are tried first.")),
            title=_("Add new WLAN configuration"),
            easy_close=False,
            footer=ui.div(
                ui.input_action_button(
                    id="btn_wlan_save",
                    label=_("Save"),
                    class_="btn-vertical-margin btn-narrow"
                ),
                ui.input_action_button(
                    id="btn_modal_cancel",
                    label=_("Cancel"),
                    class_="btn-vertical-margin btn-narrow"
                ),
            )
        )
        ui.modal_show(m)

    # Monitor updates of the database
    sess_last_imgblock_ts = [last_imgblock_ts.get_timestamp()]

    # Auto-refresh AI Training view while a finalized model is being downloaded/installed.
    # This keeps the UI responsive and updates the progress text without requiring manual reload.
    _model_dl_last_finished_at = [0.0]

    @reactive.effect
    def auto_refresh_ai_training_during_model_download():
        reactive.invalidate_later(2)
        try:
            state = RemoteModelTrainer.get_model_download_state()
        except Exception:
            return

        status = (state or {}).get("status")
        if status in ("downloading", "extracting"):
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)

        finished_at = float((state or {}).get("finished_at") or 0.0)
        if finished_at > 0.0 and finished_at != float(_model_dl_last_finished_at[0] or 0.0):
            _model_dl_last_finished_at[0] = finished_at
            # Finalize completion in the main process (clears MODEL_TRAINING, stores notification)
            try:
                RemoteModelTrainer.check_model_training_result(show_notification=False)
            except Exception:
                pass
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)
            reload_trigger_config.set(reload_trigger_config.get() + 1)

    # Show user notifications if there are any
    show_user_notifications()

    # Migration in progress notice
    @reactive.effect
    def migration_progress_notification():
        reactive.invalidate_later(15)
        global ids_with_original_blob
        if len(ids_with_original_blob) > 0:
            # Estimate remaining time: 15 seconds per 200 IDs (10s processing + 5s delay)
            batches_remaining = math.ceil(len(ids_with_original_blob) / 200)
            remaining_seconds = batches_remaining * 15
            remaining_minutes = max(1, round(remaining_seconds / 60))
            ui.notification_show(
                _("Database migration in progress. Performance may be a bit slower until finished ({} pictures remaining, ~{} min).").format(
                    len(ids_with_original_blob), remaining_minutes
                ),
                duration=14,
                type="warning"
            )

    @reactive.effect
    def ext_trigger_reload_photos():
        reactive.invalidate_later(3)
        if last_imgblock_ts.get_timestamp() != sess_last_imgblock_ts[0]:
            sess_last_imgblock_ts[0] = last_imgblock_ts.get_timestamp()
            reload_trigger_photos.set(reload_trigger_photos.get() + 1)
            logging.info("Reloading photos due to external trigger.")

    @reactive.effect
    def periodic_ram_check():
        reactive.invalidate_later(60)
        used_ram_space = get_used_ram_space()
        total_ram_space = get_total_ram_space()
        ram_usage_percentage = (used_ram_space / total_ram_space) * 100
        if ram_usage_percentage >= 90:
            if get_labelstudio_status() == True:
                additional_text = " " + _("LabelStudio is running. If you do not need it anymore to label images, please stop it in the [AI TRAINING] section.")
            else:
                additional_text = ""
            ui.notification_show(_("Warning: RAM usage is at {:.1f}%!{}").format(ram_usage_percentage, additional_text), duration=20, type="warning")

    @reactive.effect
    def update_live_status():
        reactive.invalidate_later(0.25)

        def _set_live_status_if_changed(new_state: dict) -> None:
            try:
                prev = live_status.get() or {}
            except Exception:
                prev = {}
            if prev != new_state:
                live_status.set(new_state)

        try:
            if is_remote_mode():
                remote_ready = False
                try:
                    from src.remote.control_client import RemoteControlClient

                    client = RemoteControlClient.instance()
                    client.ensure_started()
                    remote_ready = bool(client.wait_until_ready(timeout=0))
                except Exception:
                    remote_ready = False

                if not remote_ready:
                    _set_live_status_if_changed({"ok": False, "remote_waiting": True})

                    now = monotonic_time()
                    last_log = getattr(update_live_status, "_last_remote_not_ready_log", 0.0)
                    if (now - float(last_log or 0.0)) > 10.0:
                        logging.info("[LIVE_STATUS] Remote control not connected yet; skipping live status update.")
                        update_live_status._last_remote_not_ready_log = now

                    try:
                        ui.update_action_button(
                            "bManualOverride",
                            label=_("Manual unlock not yet initialized..."),
                            icon=icon_svg("unlock"),
                            disabled=True,
                        )
                    except Exception:
                        pass
                    try:
                        ui.update_action_button("bResetPreyCooldown", disabled=True)
                    except Exception:
                        pass
                    return

            magnets = getattr(Magnets, "instance", None)
            if magnets is None:
                # During boot the backend (and thus Magnets.init()) may not be ready yet.
                _set_live_status_if_changed({"ok": False, "remote_waiting": False})

                now = monotonic_time()
                last_log = getattr(update_live_status, "_last_hw_not_ready_log", 0.0)
                if (now - float(last_log or 0.0)) > 10.0:
                    logging.info("[LIVE_STATUS] Hardware not yet initialized; skipping live status update.")
                    update_live_status._last_hw_not_ready_log = now

                # Keep buttons disabled until we have a valid magnet instance.
                try:
                    ui.update_action_button(
                        "bManualOverride",
                        label=_("Manual unlock not yet initialized..."),
                        icon=icon_svg("unlock"),
                        disabled=True,
                    )
                except Exception:
                    pass
                try:
                    ui.update_action_button("bResetPreyCooldown", disabled=True)
                except Exception:
                    pass
                return

            inside_lock_state = magnets.get_inside_state()
            outside_lock_state = magnets.get_outside_state()

            from src.backend import motion_state, motion_state_lock

            with motion_state_lock:
                outside_motion_state = motion_state["outside"]
                inside_motion_state = motion_state["inside"]

            prey_detection_mono = float(getattr(backend_main, "prey_detection_mono", 0.0) or 0.0)
            if prey_detection_mono > 0.0:
                delta_to_last_prey_detection = monotonic_time() - prey_detection_mono
                time_until_release = float(CONFIG['LOCK_DURATION_AFTER_PREY_DETECTION']) - delta_to_last_prey_detection
                forced_lock_due_prey = time_until_release > 0
            else:
                delta_to_last_prey_detection = 0
                time_until_release = 0
                forced_lock_due_prey = False

            if not forced_lock_due_prey:
                # Keep stable values when no prey lock is active to avoid unnecessary UI re-renders.
                delta_to_last_prey_detection = 0
                time_until_release = 0

            _set_live_status_if_changed(
                {
                    "ok": True,
                    "remote_waiting": False,
                    "inside_lock": bool(inside_lock_state),
                    "outside_lock": bool(outside_lock_state),
                    "inside_motion": bool(inside_motion_state),
                    "outside_motion": bool(outside_motion_state),
                    "forced_lock_due_prey": bool(forced_lock_due_prey),
                    "time_until_release": float(max(0, time_until_release)),
                    "delta_to_last_prey_detection": float(max(0, delta_to_last_prey_detection)),
                }
            )

            try:
                if inside_lock_state:
                    ui.update_action_button("bManualOverride", label=_("Close inside now"), icon=icon_svg("lock"), disabled=False)
                else:
                    ui.update_action_button("bManualOverride", label=_("Open inside now"), icon=icon_svg("lock-open"), disabled=False)
            except Exception:
                pass

            try:
                if forced_lock_due_prey:
                    ui.update_action_button("bResetPreyCooldown", disabled=False)
                else:
                    ui.update_action_button("bResetPreyCooldown", disabled=True)
            except Exception:
                pass
        except Exception as e:
            # Throttle errors here; during boot transient None/IO errors can happen.
            now = monotonic_time()
            last_log = getattr(update_live_status, "_last_error_log", 0.0)
            if (now - float(last_log or 0.0)) > 10.0:
                logging.exception("Failed to update live status: %s", e)
                update_live_status._last_error_log = now
            _set_live_status_if_changed({"ok": False, "remote_waiting": False})
    
    @render.ui
    def live_view_aspect_style():
        # Updated whenever the image renderer learns a new aspect ratio.
        w, h = (live_view_aspect.get() or (4, 3))
        w = int(w or 4)
        h = int(h or 3)
        w = max(1, w)
        h = max(1, h)
        return ui.HTML(f'<style>#live_view_stage{{--kh-live-aspect:{w} / {h};}}</style>')

    @render.ui
    def live_view_warning():
        return ui.HTML(live_view_warning_html.get() or '')

    @render.ui
    def live_view_image():
        # Allows other code paths (e.g., config save) to force an immediate refresh.
        __ = live_view_refresh_nonce.get()

        refresh_s = float(CONFIG.get('LIVE_VIEW_REFRESH_INTERVAL', 2.0) or 2.0)
        refresh_s = max(0.1, refresh_s)

        # Mark running to avoid concurrent forced refresh triggers.
        live_view_image._is_running = True

        if not hasattr(live_view_image, "last_frame_hash"):
            live_view_image.last_frame_hash = None
            live_view_image.last_change_time = monotonic_time()
            live_view_image.no_frame_since = None
            live_view_image.last_ar_w = 4
            live_view_image.last_ar_h = 3
            live_view_image.visible_frame_jpg = None
            live_view_image.visible_frame_hash = None
            live_view_image.preload_frame_jpg = None
            live_view_image.preload_frame_hash = None
            live_view_image.last_camera_key = None
            live_view_image._last_aspect_set = (4, 3)

        def _set_aspect(w: int, h: int) -> None:
            try:
                w = int(w)
                h = int(h)
                if w <= 0 or h <= 0:
                    return
                prev = getattr(live_view_image, "_last_aspect_set", None)
                if tuple(prev or ()) != (w, h):
                    live_view_image._last_aspect_set = (w, h)
                    live_view_aspect.set((w, h))
            except Exception:
                return

        camera_key = (CONFIG.get("CAMERA_SOURCE"), CONFIG.get("IP_CAMERA_URL"))
        if live_view_image.last_camera_key is None:
            live_view_image.last_camera_key = camera_key
        elif camera_key != live_view_image.last_camera_key:
            # Camera config changed: never show an old buffered frame.
            live_view_image.last_camera_key = camera_key
            live_view_image.last_frame_hash = None
            live_view_image.last_change_time = monotonic_time()
            live_view_image.visible_frame_jpg = None
            live_view_image.visible_frame_hash = None
            live_view_image.preload_frame_jpg = None
            live_view_image.preload_frame_hash = None
            live_view_image.no_frame_since = None
            live_view_image.last_ar_w = 4
            live_view_image.last_ar_h = 3
            _set_aspect(4, 3)

        try:
            if getattr(live_view_image, 'preload_frame_jpg', None) is not None:
                live_view_image.visible_frame_jpg = live_view_image.preload_frame_jpg
                live_view_image.visible_frame_hash = live_view_image.preload_frame_hash
                live_view_image.preload_frame_jpg = None
                live_view_image.preload_frame_hash = None

            frame = model_handler.get_camera_frame()
            if frame is None:
                if getattr(live_view_image, 'no_frame_since', None) is None:
                    live_view_image.no_frame_since = monotonic_time()
                no_frame_elapsed = max(0.0, monotonic_time() - float(live_view_image.no_frame_since or 0.0))

                if CONFIG.get("CAMERA_SOURCE") == "ip_camera":
                    cam_state = model_handler.get_camera_state()
                    cam_state_text = str(cam_state) if cam_state is not None else _('Unknown')
                    startup_grace_s = 12.0

                    if no_frame_elapsed < startup_grace_s:
                        img_html = (
                            '<div class="placeholder-image" style="padding-top: 20px; padding-bottom: 20px;">'
                            '<div></div>'
                            '<div><strong>' + _('Connecting to the IP camera...') + '</strong></div>'
                            '<div>' + _('Please wait a few seconds while the stream is initialized.') + '</div>'
                            '<div class="spinner-container"><div class="spinner"></div></div>'
                            '<div>' + _('Current status: ') + cam_state_text + '</div>'
                            '<div></div>'
                            '</div>'
                        )
                    else:
                        reconnect_hint = ''
                        if no_frame_elapsed >= 25.0:
                            reconnect_hint = '<div>' + _('If the stream does not recover, verify the URL, camera network connection, and camera credentials.') + '</div>'
                        img_html = (
                            '<div class="placeholder-image" style="padding-top: 20px; padding-bottom: 20px;">'
                            '<div></div>'
                            '<div><strong>' + _('Connection to the IP camera failed.') + '</strong></div>'
                            '<div>' + _("Please check the stream URL and the network connection of your IP camera.") + '</div>'
                            '<div class="spinner-container"><div class="spinner"></div></div>'
                            '<div>' + _('If you have just changed the camera settings, please wait a few seconds for the camera to reconnect.') + '</div>'
                            '<div>' + _('Current status: ') + cam_state_text + '</div>'
                            + reconnect_hint +
                            '<div></div>'
                            '</div>'
                        )
                else:
                    extra_hint = ''
                    if (not is_remote_mode()) and (no_frame_elapsed >= 25.0):
                        extra_hint = '<div>' + _('If this message does not disappear within 60 seconds, please (re-)install the required camera drivers with the "Reinstall Camera Driver" button in the "System" section.') + '</div>'
                    img_html = (
                        '<div class="placeholder-image" style="padding-top: 20px; padding-bottom: 20px;">'
                        '<div></div>'
                        '<div><strong>' + _('Connection to the camera failed.') + '</strong></div>'
                        '<div>' + _("Please wait...") + '</div>'
                        '<div class="spinner-container"><div class="spinner"></div></div>'
                        + extra_hint +
                        '<div></div>'
                        '</div>'
                    )
            else:
                live_view_image.no_frame_since = None
                frame_jpg = model_handler.encode_jpg_image(frame)
                frame_hash = hashlib.md5(frame_jpg).hexdigest() if frame_jpg else None

                if frame_jpg:
                    try:
                        h, w = frame.shape[:2]
                        if w > 0 and h > 0:
                            live_view_image.last_ar_w = int(w)
                            live_view_image.last_ar_h = int(h)
                            _set_aspect(live_view_image.last_ar_w, live_view_image.last_ar_h)
                    except Exception:
                        pass

                    if frame_hash != live_view_image.last_frame_hash:
                        live_view_image.last_change_time = monotonic_time()
                        live_view_image.last_frame_hash = frame_hash
                        live_view_image.preload_frame_jpg = frame_jpg
                        live_view_image.preload_frame_hash = frame_hash

                    if (
                        monotonic_time() - live_view_image.last_change_time > 5
                        and CONFIG.get("CAMERA_SOURCE") == "ip_camera"
                    ):
                        img_html = (
                            '<div class="placeholder-image" style="padding-top: 20px; padding-bottom: 20px;">'
                            '<div></div>'
                            '<div><strong>' + _('Camera stream appears to be frozen.') + '</strong></div>'
                            '<div>' + _("Please check your external IP camera connection or network settings.") + '</div>'
                            '<div class="spinner-container"><div class="spinner"></div></div>'
                            '<div></div>'
                            '</div>'
                        )
                    else:
                        front_style = 'style="position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;z-index:2;"'
                        back_style = 'style="position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;z-index:1;"'
                        visible_html = ''
                        preload_html = ''

                        if getattr(live_view_image, 'visible_frame_jpg', None):
                            visible_b64 = base64.b64encode(live_view_image.visible_frame_jpg).decode('utf-8')
                            visible_html = f'<img src="data:image/jpeg;base64,{visible_b64}" {front_style} />'
                        else:
                            now_b64 = base64.b64encode(frame_jpg).decode('utf-8')
                            visible_html = f'<img src="data:image/jpeg;base64,{now_b64}" {front_style} />'
                            live_view_image.visible_frame_jpg = frame_jpg
                            live_view_image.visible_frame_hash = frame_hash
                            live_view_image.preload_frame_jpg = None
                            live_view_image.preload_frame_hash = None

                        if getattr(live_view_image, 'preload_frame_jpg', None):
                            preload_b64 = base64.b64encode(live_view_image.preload_frame_jpg).decode('utf-8')
                            preload_html = f'<img src="data:image/jpeg;base64,{preload_b64}" {back_style} aria-hidden="true" />'

                        img_html = f'{preload_html}{visible_html}'
                else:
                    img_html = f'<div class="placeholder-image"><strong>' + _('Could not read the picture from the camera.') + '</strong></div>'

            result = ui.HTML(img_html)
        except Exception as e:
            logging.error(f"Failed to fetch the live view image: {e}")
            result = ui.HTML('<div class="placeholder-image"><strong>' + _('An error occured while fetching the live view image.') + '</strong></div>')

        finally:
            live_view_image._is_running = False
            if getattr(live_view_image, "_refresh_after_run", False):
                live_view_image._refresh_after_run = False
                try:
                    live_view_refresh_nonce.set(live_view_refresh_nonce.get() + 1)
                except Exception:
                    pass

        # Schedule the next refresh only after this render completed.
        reactive.invalidate_later(refresh_s)
        return result

    @render.ui
    def live_view_overlay_clock():
        # Clock stays on top of the picture.
        reactive.invalidate_later(1.0)
        ts = datetime.now(ZoneInfo(CONFIG['TIMEZONE'])).strftime('%H:%M:%S')

        clock_label = _("Clock")
        clock_title = _("Current time")
        return ui.HTML(
            f'<div class="live-view-overlay-clock" aria-label="{clock_label}" title="{clock_title}">{ts}</div>'
        )

    @render.ui
    def live_view_overlay_status():
        # Status chips (inside/outside lock + motion) are rendered below the picture.
        st = live_status.get() or {}

        def _pill_html(inner_html: str, variant: str, *, aria_label: str, title: str) -> str:
            return (
                f'<span class="live-view-pill live-view-pill--{variant}" '
                f'aria-label="{aria_label}" title="{title}">'
                f'{inner_html}'
                f'</span>'
            )

        def _icon_html(name: str) -> str:
            try:
                return str(icon_svg(name, margin_left="0", margin_right="0"))
            except Exception:
                return "•"

        inside_label = _("Inside")
        outside_label = _("Outside")
        status_unavailable_label = _("Status unavailable")

        remote_indicator_html = ""
        if is_remote_mode():
            host = (CONFIG.get("REMOTE_TARGET_HOST") or "").strip()

            is_ready = False
            if host:
                try:
                    from src.remote.control_client import RemoteControlClient

                    client = RemoteControlClient.instance()
                    client.ensure_started()
                    is_ready = bool(client.wait_until_ready(timeout=0))
                except Exception:
                    is_ready = False

            remote_variant = "muted"
            remote_title = _("Remote target host not configured")
            remote_aria = _("Remote: not configured")

            if host and is_ready:
                remote_variant = "ok"
                remote_title = _("Connected to Kittyflap") + f": {host}"
                remote_aria = _("Remote: connected")
            elif host:
                remote_variant = "blocked"
                remote_title = _("Connecting to Kittyflap") + f": {host}"
                remote_aria = _("Remote: connecting")

            remote_indicator_html = (
                '<div class="remote-connection-indicator" '
                f'aria-label="{remote_aria}" title="{remote_title}" data-bs-title="{remote_title}" '
                'data-bs-toggle="tooltip" data-bs-trigger="hover focus" data-bs-placement="top" tabindex="0">'
                + (
                    f'<span class="remote-status-chip remote-status-chip--{remote_variant}" '
                    f'role="img" aria-label="{remote_aria}"></span>'
                )
                + "</div>"
            )

        if st.get("ok"):
            inside_lock_is_open = bool(st.get("inside_lock"))
            outside_lock_is_open = bool(st.get("outside_lock"))
            inside_motion = bool(st.get("inside_motion"))
            outside_motion = bool(st.get("outside_motion"))

            inside_lock_variant = "ok" if inside_lock_is_open else "muted"
            outside_lock_variant = "ok" if outside_lock_is_open else "muted"
            inside_motion_variant = "active" if inside_motion else "muted"
            outside_motion_variant = "active" if outside_motion else "muted"

            inside_lock_icon = _icon_html("lock-open" if inside_lock_is_open else "lock")
            outside_lock_icon = _icon_html("lock-open" if outside_lock_is_open else "lock")
            inside_motion_icon = _icon_html("eye" if inside_motion else "eye-slash")
            outside_motion_icon = _icon_html("eye" if outside_motion else "eye-slash")

            prey_banner_html = ""
            if st.get("forced_lock_due_prey"):
                prey_banner_html = (
                    '<div class="live-view-banner live-view-banner-warn live-view-overlay-banner" role="status">'
                    + _(
                        "Prey detected {0:.0f}s ago. Inside lock remains closed for {1:.0f}s."
                    ).format(
                        float(st.get("delta_to_last_prey_detection", 0.0)),
                        float(st.get("time_until_release", 0.0)),
                    )
                    + "</div>"
                )

            status_html = (
                '<div class="live-view-statusbar">'
                '<div class="live-view-overlay-row">'
                '<div class="live-view-overlay-chip">'
                f'<span class="live-view-overlay-title">{inside_label}</span>'
                + _pill_html(
                    inside_lock_icon,
                    inside_lock_variant,
                    aria_label=_("Inside lock: Open") if inside_lock_is_open else _("Inside lock: Closed"),
                    title=_("Inside lock: Open") if inside_lock_is_open else _("Inside lock: Closed"),
                )
                + _pill_html(
                    inside_motion_icon,
                    inside_motion_variant,
                    aria_label=_("Inside motion: Motion") if inside_motion else _("Inside motion: No motion"),
                    title=_("Inside motion: Motion") if inside_motion else _("Inside motion: No motion"),
                )
                + '</div>'
                '<div class="live-view-overlay-chip">'
                f'<span class="live-view-overlay-title">{outside_label}</span>'
                + _pill_html(
                    outside_lock_icon,
                    outside_lock_variant,
                    aria_label=_("Outside lock: Open") if outside_lock_is_open else _("Outside lock: Closed"),
                    title=_("Outside lock: Open") if outside_lock_is_open else _("Outside lock: Closed"),
                )
                + _pill_html(
                    outside_motion_icon,
                    outside_motion_variant,
                    aria_label=_("Outside motion: Motion") if outside_motion else _("Outside motion: No motion"),
                    title=_("Outside motion: Motion") if outside_motion else _("Outside motion: No motion"),
                )
                + '</div>'
                + '</div>'
                + remote_indicator_html
                + prey_banner_html
                + '</div>'
            )
        else:
            unavailable_text = status_unavailable_label
            if is_remote_mode() and bool(st.get("remote_waiting")):
                unavailable_text = _("Remote control not connected")

            status_html = (
                '<div class="live-view-statusbar">'
                '<div class="live-view-overlay-row">'
                f'<div class="live-view-overlay-chip live-view-overlay-chip--error">{unavailable_text}</div>'
                + '</div>'
                + remote_indicator_html
                + '</div>'
            )

        return ui.HTML(status_html)

    @reactive.Effect
    def immediate_bg_task_site_load():
        immediate_bg_task("site load")

    @reactive.Effect
    @reactive.event(input.button_reload)
    def immediate_bg_task_reload_button():
        immediate_bg_task("reload button")

    @reactive.Effect
    @reactive.event(input.button_today)
    def immediate_bg_task_reload_button():
        immediate_bg_task("today button")

    @reactive.Effect
    @reactive.event(input.button_detection_overlay)
    def update_config_images_with_overlay():
        CONFIG['SHOW_IMAGES_WITH_OVERLAY'] = input.button_detection_overlay()
        update_single_config_parameter("SHOW_IMAGES_WITH_OVERLAY")

    @reactive.Effect
    @reactive.event(input.button_events_view)
    def update_config_group_pictures_to_events():
        CONFIG['GROUP_PICTURES_TO_EVENTS'] = input.button_events_view()
        update_single_config_parameter("GROUP_PICTURES_TO_EVENTS")

    @output
    @render.ui
    def ui_photos_date():
        """
        Creates a UI component for selecting and filtering photos by date.

        The UI component includes:
        - A date selector with decrement and increment buttons.
        - A "Today" button to quickly select the current date.
        - Switches to filter photos to show only detected cats or mice.

        Returns:
            uiDateBar (ui.div): A UI div element containing the date selection and filtering controls.
        """
        uiDateBar = ui.div(
            ui.row(
                ui.div(
                    ui.div(ui.input_action_button("button_decrement", "", icon=icon_svg("angle-left", margin_right="auto"), class_="btn-date-control"), class_="col-auto px-1"),
                    ui.div(ui.input_date("date_selector", "", format=CONFIG['DATE_FORMAT']), class_="col-auto px-1"),
                    ui.div(ui.input_action_button("button_increment", "", icon=icon_svg("angle-right", margin_right="auto"), class_="btn-date-control"), class_="col-auto px-1"),
                    class_="d-flex justify-content-center align-items-center flex-nowrap"
                ),
                ui.div(ui.input_action_button("button_today", _("Today"), icon=icon_svg("calendar-day"), class_="btn-date-filter"), class_="col-auto px-1"),
                ui.div(ui.input_action_button("button_reload", "", icon=icon_svg("rotate", margin_right="auto"), class_="btn-date-filter"), class_="col-auto px-1"),
                class_="d-flex justify-content-center align-items-center"  # Centers elements horizontally and prevents wrapping
            ),
            ui.br(),
            ui.row(
                ui.div(ui.input_switch("button_cat_only", _("Show detected cats only"), CONFIG['SHOW_CATS_ONLY']), class_="col-auto btn-date-filter px-1"),
                ui.div(ui.input_switch("button_mouse_only", _("Show detected mice only"), CONFIG['SHOW_MICE_ONLY']), class_="col-auto btn-date-filter px-1"),
                ui.div(ui.input_switch("button_detection_overlay", _("Show detection overlay"), CONFIG['SHOW_IMAGES_WITH_OVERLAY']), class_="col-auto btn-date-filter px-1"),
                ui.div(ui.input_switch("button_events_view", _("Group pictures to events"), CONFIG['GROUP_PICTURES_TO_EVENTS']), class_="col-auto btn-date-filter px-1"),
                class_="d-flex justify-content-center align-items-center"  # Centers elements horizontally
            ),
            class_="container"  # Adds centering within a smaller container
            )
        return uiDateBar
    
    @reactive.Effect
    @reactive.event(input.button_cat_only)
    def update_config_show_cats_only():
        CONFIG['SHOW_CATS_ONLY'] = input.button_cat_only()
        update_single_config_parameter("SHOW_CATS_ONLY")

    @reactive.Effect
    @reactive.event(input.button_mouse_only)
    def update_config_show_mice_only():
        CONFIG['SHOW_MICE_ONLY'] = input.button_mouse_only()
        update_single_config_parameter("SHOW_MICE_ONLY")

    @reactive.Effect
    @reactive.event(input.button_decrement, ignore_none=True)
    def dec_ui_photos_date():
        """
        Decrease the date in the UI date selector by one day.
        This function retrieves the current date from the input date selector,
        decreases it by one day, and updates the date input using the session's
        send_input_message method.
        Returns:
            None
        """
        # Get the current date from the input
        current_date = input.date_selector()
        
        # Only proceed if the date is set
        if current_date:
            new_date = pd.to_datetime(current_date).date() - timedelta(days=1)
            # Update the date input using session.send_input_message
            session.send_input_message("date_selector", {"value": new_date.strftime("%Y-%m-%d")})

    @reactive.Effect
    @reactive.event(input.button_increment, ignore_none=True)
    def inc_ui_photos_date():
        """
        Increments the date selected in the UI by one day.
        This function retrieves the current date from a date selector input,
        increments it by one day, and updates the date selector input with
        the new date.
        Returns:
            None
        """
        # Get the current date from the input
        current_date = input.date_selector()
        
        # Only proceed if the date is set
        if current_date:
            new_date = pd.to_datetime(current_date).date() + timedelta(days=1)
            # Update the date input using session.send_input_message
            session.send_input_message("date_selector", {"value": new_date.strftime("%Y-%m-%d")})

    def _photos_filters_to_utc_range() -> tuple[str, str]:
        date_start = format_date_minmax(input.date_selector(), True)
        date_end = format_date_minmax(input.date_selector(), False)
        timezone = ZoneInfo(CONFIG['TIMEZONE'])
        date_start_utc = datetime.strptime(date_start, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S%z')
        date_end_utc = datetime.strptime(date_end, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S%z')
        return date_start_utc, date_end_utc

    def _photos_total_pages() -> tuple[int, int]:
        date_start_utc, date_end_utc = _photos_filters_to_utc_range()
        total_count = db_count_photos(
            CONFIG['KITTYHACK_DATABASE_PATH'],
            date_start_utc,
            date_end_utc,
            input.button_cat_only(),
            input.button_mouse_only(),
            CONFIG['MOUSE_THRESHOLD'],
        )
        per_page = max(1, int(CONFIG['ELEMENTS_PER_PAGE']))
        total_pages = max(1, int(math.ceil(float(total_count) / float(per_page))))
        return total_count, total_pages

    @reactive.Effect
    @reactive.event(input.button_reload, input.date_selector, input.button_cat_only, input.button_mouse_only, reload_trigger_photos, ignore_none=True)
    def reset_photos_page_on_filter_change():
        if input.button_events_view():
            return
        try:
            __count, total_pages = _photos_total_pages()
            session.send_input_message("photos_page", {"value": 1, "min": 1, "max": total_pages})
        except Exception:
            pass

    @reactive.Effect
    @reactive.event(input.photos_prev_page, ignore_none=True)
    def photos_prev_page():
        if input.button_events_view():
            return
        try:
            __count, total_pages = _photos_total_pages()
            current = int(input.photos_page() or 1)
            new_val = max(1, min(total_pages, current - 1))
            session.send_input_message("photos_page", {"value": new_val, "min": 1, "max": total_pages})
        except Exception:
            pass

    @reactive.Effect
    @reactive.event(input.photos_next_page, ignore_none=True)
    def photos_next_page():
        if input.button_events_view():
            return
        try:
            __count, total_pages = _photos_total_pages()
            current = int(input.photos_page() or 1)
            new_val = max(1, min(total_pages, current + 1))
            session.send_input_message("photos_page", {"value": new_val, "min": 1, "max": total_pages})
        except Exception:
            pass

    @reactive.Effect
    @reactive.event(input.button_today, ignore_none=True)
    def reset_ui_photos_date():
        # Get the current date
        now = datetime.now()
        session.send_input_message("date_selector", {"value": now.strftime("%Y-%m-%d")})

    @output
    @render.ui
    @reactive.event(input.button_events_view, ignore_none=True)
    def ui_photos_events():
        if input.button_events_view():
            return ui.output_ui("ui_events_by_date")
        else:
            return ui.div(
                ui.output_ui("ui_photos_cards_nav"),
                ui.output_ui("ui_photos_cards"),
            )
    
    @output
    @render.ui
    @reactive.event(input.button_reload, input.date_selector, input.button_cat_only, input.button_mouse_only, reload_trigger_photos, ignore_none=True)
    def ui_photos_cards_nav():
        if input.button_events_view():
            return ui.div()

        date_start = format_date_minmax(input.date_selector(), True)
        date_end = format_date_minmax(input.date_selector(), False)
        timezone = ZoneInfo(CONFIG['TIMEZONE'])
        date_start = datetime.strptime(date_start, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S%z')
        date_end = datetime.strptime(date_end, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S%z')

        total_count = db_count_photos(
            CONFIG['KITTYHACK_DATABASE_PATH'],
            date_start,
            date_end,
            input.button_cat_only(),
            input.button_mouse_only(),
            CONFIG['MOUSE_THRESHOLD'],
        )

        per_page = max(1, int(CONFIG['ELEMENTS_PER_PAGE']))
        total_pages = max(1, int(math.ceil(float(total_count) / float(per_page))))

        try:
            current_page = int(input.photos_page())
        except Exception:
            current_page = 1
        current_page = max(1, min(total_pages, current_page))

        # Keep input bounds synced (use raw input message since update_numeric isn't used elsewhere)
        try:
            session.send_input_message("photos_page", {"value": current_page, "min": 1, "max": total_pages})
        except Exception:
            pass

        return ui.div(
            ui.div(
                ui.input_action_button("photos_prev_page", "", icon=icon_svg("angle-left"), class_="btn-page-control"),
                ui.input_numeric("photos_page", _("Page"), value=current_page, min=1, max=total_pages, step=1, width="4rem"),
                ui.tags.span(f"/ {total_pages}", class_="photos-page-total"),
                ui.input_action_button("photos_next_page", "", icon=icon_svg("angle-right"), class_="btn-page-control"),
                ui.tags.span(
                    f"{total_count} " + _("pictures"),
                    class_="photos-count",
                ),
                class_="photos-pager",
            ),
            class_="container",
        )

    @output
    @render.ui
    @reactive.event(
        input.photos_page,
        input.photos_prev_page,
        input.photos_next_page,
        input.button_events_view,         # to clear when switching view mode
        input.button_detection_overlay,   # to toggle overlays without extra reloads
        input.button_reload,
        input.date_selector,
        input.button_cat_only,
        input.button_mouse_only,
        ignore_none=True
    )
    def ui_photos_cards():
        ui_cards = []

        if input.button_events_view():
            return ui.div()

        date_start = format_date_minmax(input.date_selector(), True)
        date_end = format_date_minmax(input.date_selector(), False)
        timezone = ZoneInfo(CONFIG['TIMEZONE'])
        date_start = datetime.strptime(date_start, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S%z')
        date_end = datetime.strptime(date_end, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S%z')

        total_count = db_count_photos(
            CONFIG['KITTYHACK_DATABASE_PATH'],
            date_start,
            date_end,
            input.button_cat_only(),
            input.button_mouse_only(),
            CONFIG['MOUSE_THRESHOLD'],
        )
        per_page = max(1, int(CONFIG['ELEMENTS_PER_PAGE']))
        total_pages = max(1, int(math.ceil(float(total_count) / float(per_page))))

        try:
            page_number = int(input.photos_page())
        except Exception:
            page_number = 1
        page_number = max(1, min(total_pages, page_number))

        # db_get_photos uses a reverse paging scheme; convert "newest page=1" into its index.
        page_index = max(0, total_pages - page_number)

        df_photos = db_get_photos(
            CONFIG['KITTYHACK_DATABASE_PATH'],
            ReturnDataPhotosDB.all_except_photos,
            date_start,
            date_end,
            input.button_cat_only(),
            input.button_mouse_only(),
            CONFIG['MOUSE_THRESHOLD'],
            page_index,
            per_page,
        )

        if df_photos.empty:
            logging.info("No pictures for the selected filter criteria found.")
            return ui.help_text(_("No pictures for the selected filter criteria found."), class_="no-images-found")

        # Get a dictionary mapping RFIDs to cat names
        cat_name_dict = get_cat_name_rfid_dict(CONFIG['KITTYHACK_DATABASE_PATH'])

        for index, data_row in df_photos.iterrows():
            mouse_probability = data_row["mouse_probability"]

            event_text = data_row['event_text']
            if event_text:
                detected_objects = read_event_from_json(event_text)
            else:
                detected_objects = []

            try:
                photo_timestamp = pd.to_datetime(get_local_date_from_utc_date(data_row["created_at"])).strftime('%H:%M:%S')
            except ValueError:
                photo_timestamp = "Unknown date"
            
            if data_row["rfid"]:
                cat_name = cat_name_dict.get(data_row["rfid"], _("Unknown RFID: {}".format(data_row["rfid"])))
            else:
                cat_name = _("No RFID found")

            card_footer_mouse = f"{icon_svg('magnifying-glass')} {mouse_probability:.1f}%"
            if cat_name:
                card_footer_cat = f" | {icon_svg('cat')} {cat_name}"
            else:
                card_footer_cat = ""
            
            pid = int(data_row['id'])
            thumb_src = f"/thumb/{pid}.jpg"
            orig_src = f"/orig/{pid}.jpg"

            img_html = f'''
                <div class="kh-photo-thumb">
                    <a class="kh-photo-open" href="{orig_src}" target="_blank" rel="noopener" aria-label="Open original">{icon_svg('up-right-from-square')}</a>
                    <img src="{thumb_src}" loading="lazy" decoding="async" />'''

            if input.button_detection_overlay() and detected_objects:
                for detected_object in detected_objects:
                    label_pos = 'bottom: -26px' if detected_object.y < 16 else 'top: -26px'
                    img_html += f'''
                    <div class="kh-detect-box" style="left:{detected_object.x}%; top:{detected_object.y}%; width:{detected_object.width}%; height:{detected_object.height}%;">
                        <div class="kh-detect-label" style="{label_pos};">
                            {detected_object.object_name} ({detected_object.probability:.0f}%)
                        </div>
                    </div>'''

            img_html += "</div>"
            
            ui_cards.append(
                ui.card(
                    ui.card_header(
                        ui.div(
                            ui.HTML(f"{photo_timestamp} | {data_row['id']}"),
                            ui.div(ui.input_checkbox(id=f"delete_photo_{data_row['id']}", label="", value=False), class_="kh-photo-delete"),
                        ),
                    ),
                    ui.HTML(img_html),
                    ui.card_footer(
                        ui.div(
                            ui.tooltip(ui.HTML(card_footer_mouse), _("Mouse probability"), options={"trigger": "hover"}),
                            ui.HTML(card_footer_cat),
                        )
                    ),
                    full_screen=True,
                    class_="image-container kh-photo-card" + (" image-container-alert" if mouse_probability >= CONFIG['MOUSE_THRESHOLD'] else "")
                )
            )
            pass

        return ui.div(
            ui.tags.div(*ui_cards, class_="kh-photo-grid"),
            ui.panel_absolute(
                ui.panel_well(
                    ui.input_action_button(id="delete_selected_photos", label=_("Delete selected photos"), icon=icon_svg("trash")),
                    class_="sticky-action-well",
                ),
                draggable=False, width="100%", left="0px", right="0px", bottom="0px", fixed=True,
            ),
            ui.br(),
            ui.br(),
            ui.br(),
        )
        
    @reactive.Effect
    @reactive.event(input.delete_selected_photos)
    def delete_selected_photos():
        deleted_photos = []

        try:
            # Only consider IDs that are currently rendered on the page
            date_start_utc, date_end_utc = _photos_filters_to_utc_range()
            total_count = db_count_photos(
                CONFIG['KITTYHACK_DATABASE_PATH'],
                date_start_utc,
                date_end_utc,
                input.button_cat_only(),
                input.button_mouse_only(),
                CONFIG['MOUSE_THRESHOLD'],
            )
            per_page = max(1, int(CONFIG['ELEMENTS_PER_PAGE']))
            total_pages = max(1, int(math.ceil(float(total_count) / float(per_page))))
            try:
                page_number = int(input.photos_page() or 1)
            except Exception:
                page_number = 1
            page_number = max(1, min(total_pages, page_number))
            page_index = max(0, total_pages - page_number)
            df_photos = db_get_photos(
                CONFIG['KITTYHACK_DATABASE_PATH'],
                ReturnDataPhotosDB.only_ids,
                date_start_utc,
                date_end_utc,
                input.button_cat_only(),
                input.button_mouse_only(),
                CONFIG['MOUSE_THRESHOLD'],
                page_index,
                per_page,
            )
        except Exception:
            df_photos = pd.DataFrame()

        for id in (df_photos['id'] if not df_photos.empty and 'id' in df_photos.columns else []):
            try:
                card_del = input[f"delete_photo_{id}"]()
            except:
                card_del = False

            if card_del:
                deleted_photos.append(id)
                result = delete_photo_by_id(CONFIG['KITTYHACK_DATABASE_PATH'], id)
                if result.success:
                    ui.notification_show(_("Photo {} deleted successfully.").format(id), duration=5, type="message")
                else:
                    ui.notification_show(_("An error occurred while deleting the photo: {}").format(result.message), duration=10, type="error")

        if deleted_photos:
            # Reload the dataset
            reload_trigger_photos.set(reload_trigger_photos.get() + 1)
        else:
            ui.notification_show(_("No photos selected for deletion."), duration=5, type="message")

    @output
    @render.ui
    def ui_live_view():
        live_view = ui.card(
            ui.output_ui("live_view_aspect_style"),
            ui.output_ui("live_view_warning_panel"),
            ui.div(
                ui.output_ui("live_view_image"),
                ui.output_ui("live_view_overlay_clock"),
                id="live_view_stage",
                class_="live-view-stage",
            ),
            ui.output_ui("live_view_overlay_status"),
            full_screen=False,
            class_="image-container live-view-card"
        )
        return ui.div(
            live_view,
        )

    @render.ui
    def live_view_warning_panel():
        # Dedicated, stable warning slot outside image processing/render path.
        html = live_view_warning_html.get() or ""
        if not html:
            return ui.HTML("")
        return ui.div(
            ui.HTML(html),
            ui.div(
                ui.input_action_button(
                    "btn_dismiss_live_view_warning",
                    _("Dismiss"),
                    class_="btn btn-sm btn-outline-secondary",
                ),
                class_="live-view-warning-dismiss",
            ),
            class_="live-view-warning-panel",
        )
    
    @output
    @render.ui
    def ui_live_view_footer():
        # Quick access controls for ALLOWED_TO_ENTER and ALLOWED_TO_EXIT
        return ui.div(
            ui.card(
                ui.row(
                    ui.column(
                        6,
                        ui.input_select(
                            id="quick_allowed_to_enter",
                            label=_("Inside direction:"),
                            choices={
                                AllowedToEnter.ALL.value: _("All cats (unlock on every detected motion)"),
                                AllowedToEnter.ALL_RFIDS.value: _("All cats with a RFID chip"),
                                AllowedToEnter.KNOWN.value: _("Only registered cats"),
                                AllowedToEnter.NONE.value: _("No cats"),
                                AllowedToEnter.CONFIGURE_PER_CAT.value: _("Individual configuration per cat (Beta)"),
                            },
                            selected=str(CONFIG['ALLOWED_TO_ENTER'].value),
                            width="100%",
                        ),
                    ),
                    ui.column(
                        6,
                        ui.input_select(
                            id="quick_allowed_to_exit",
                            label=_("Outside direction:"),
                            choices={
                                'allow': _("Allow exit"),
                                'deny': _("Do not allow exit"),
                                'configure_per_cat': _("Individual configuration per cat (Beta)"),
                            },
                            selected=str(CONFIG['ALLOWED_TO_EXIT'].value),
                            width="100%",
                        ),
                    ),
                ),
                ui.div(
                    ui.tooltip(
                        icon_svg("circle-info", margin_left="-0.1em", margin_right="auto"),
                        _("The individual configuration per cat can be set in the CATS section. Note that this is a beta feature and may not work completely reliably."),
                        id="tooltip_configure_per_cat_quick",
                        options={"trigger": "hover click"},
                    ),
                    style_="position: absolute; top: 6px; right: 10px;"
                ),
                class_="image-container",
                style_="margin-top: 0px; margin-bottom: 20px; padding-top: 8px; padding-bottom: 0px; position: relative;",
            ),
            ui.card(
                ui.input_action_button(id="bManualOverride", label=_("Manual unlock not yet initialized..."), icon=icon_svg("unlock"), disabled=True),
                class_="image-container",
                style_="margin-top: 0px;"
            ),
            ui.card(
                ui.input_action_button(id="bResetPreyCooldown", label=_("Reset prey cooldown now"), icon=icon_svg("clock-rotate-left"), disabled=True),
                class_="image-container",
                style_="margin-top: 10px;"
            ),
        )

    @reactive.Effect
    @reactive.event(input.quick_allowed_to_enter)
    def quick_update_allowed_to_enter():
        CONFIG['ALLOWED_TO_ENTER'] = AllowedToEnter(input.quick_allowed_to_enter())
        update_single_config_parameter("ALLOWED_TO_ENTER")
        update_mqtt_config('ALLOWED_TO_ENTER')
        # Sync config page input
        reload_trigger_config.set(reload_trigger_config.get() + 1)

    @reactive.Effect
    @reactive.event(input.quick_allowed_to_exit)
    def quick_update_allowed_to_exit():
        from src.baseconfig import AllowedToExit as ATE
        CONFIG['ALLOWED_TO_EXIT'] = ATE(input.quick_allowed_to_exit())
        update_single_config_parameter("ALLOWED_TO_EXIT")
        update_mqtt_config('ALLOWED_TO_EXIT')
        # Sync config page input
        reload_trigger_config.set(reload_trigger_config.get() + 1)
    
    @reactive.Effect
    @reactive.event(input.bManualOverride)
    def on_action_let_kitty_in():
        magnets = getattr(Magnets, "instance", None)
        if magnets is None:
            logging.info("[SERVER] Manual override ignored: magnets not initialized yet.")
            return

        inside_state = magnets.get_inside_state()
        if inside_state == False:
            logging.info(f"[SERVER] Manual override from Live View - letting Kitty in now")
            manual_door_override['unlock_inside'] = True
        else:
            logging.info(f"[SERVER] Manual override from Live View - close inside now")
            manual_door_override['lock_inside'] = True

    @reactive.Effect
    @reactive.event(input.bResetPreyCooldown)
    def on_action_reset_prey_cooldown():
        logging.info(f"[SERVER] Resetting prey cooldown now")
        backend_main.prey_detection_tm = 0.0
        backend_main.prey_detection_mono = 0.0

    # Add a delayed-load flag for the Last Events table
    last_events_ready = reactive.Value(False)

    @reactive.Effect
    def init_last_events_delay():
        # Schedule one re-run ~1s later; set the flag only on that re-run
        if not hasattr(init_last_events_delay, "scheduled"):
            init_last_events_delay.scheduled = False

        if not last_events_ready.get():
            if not init_last_events_delay.scheduled:
                reactive.invalidate_later(0.5)
                init_last_events_delay.scheduled = True
                return
            # Second run (after 1s): mark ready
            last_events_ready.set(True)

    @output
    @render.ui
    def ui_last_events():
        return ui.layout_column_wrap(
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h5(_("Last events"))
                    ),
                    ui.output_ui("ui_last_events_table"),
                    full_screen=False,
                    class_="generic-container",
                    style_="margin-bottom: 40px;",
                    min_height="150px"
                ),
                width="400px"
            )
        )
    
    @render.text
    @reactive.event(reload_trigger_photos, last_events_ready, ignore_none=True)
    def ui_last_events_table():
        # Show a loading spinner until the delayed flag is set
        if not last_events_ready.get():
            return ui.HTML('<div class="spinner-container"><div class="spinner"></div></div>')
        return get_events_table_html(block_count=25)
    
    @output
    @render.ui
    def ui_events_by_date():
        return ui.layout_column_wrap(
            ui.div(
                ui.card(
                    ui.output_ui("ui_events_by_date_table"),
                    full_screen=False,
                    class_="generic-container",
                    style_="margin-bottom: 40px;",
                    min_height="150px"
                ),
                width="400px"
            )
        )
    
    @render.text
    @reactive.event(input.button_reload, input.date_selector, input.button_cat_only, input.button_mouse_only, reload_trigger_photos, ignore_none=True)
    def ui_events_by_date_table():
        date_start = format_date_minmax(input.date_selector(), True)
        date_end = format_date_minmax(input.date_selector(), False)
        timezone = ZoneInfo(CONFIG['TIMEZONE'])
        # Convert date_start and date_end to timezone-aware datetime strings in the UTC timezone
        date_start = datetime.strptime(date_start, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S%z')
        date_end = datetime.strptime(date_end, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S%z')
        
        # Use the refactored function that returns HTML
        return get_events_table_html(0, date_start, date_end, input.button_cat_only(), input.button_mouse_only(), CONFIG['MOUSE_THRESHOLD'])

    def get_events_table_html(block_count=0, date_start="2020-01-01 00:00:00", date_end="2100-12-31 23:59:59", cats_only=False, mouse_only=False, mouse_probability=0.0):
        try:
            logging.info(f"Reading events from the database for block_count={block_count}, date_start={date_start}, date_end={date_end}, cats_only={cats_only}, mouse_only={mouse_only}, mouse_probability={mouse_probability}")
            df_events = db_get_motion_blocks(CONFIG['KITTYHACK_DATABASE_PATH'], block_count, date_start, date_end, cats_only, mouse_only, mouse_probability)

            if df_events.empty:
                return ui.HTML(
                    '<table class="dataframe shiny-table table w-auto">'
                    '<tbody><tr><td>' + _('No events found.') + '</td></tr></tbody>'
                    '</table>'
                )
                
            # Convert UTC timestamps to local timezone
            df_events['created_at'] = pd.to_datetime(df_events['created_at']).dt.tz_convert(CONFIG['TIMEZONE'])
            df_events = df_events.sort_values(by='created_at', ascending=False)
            df_events['date'] = df_events['created_at'].dt.date
            df_events['time'] = df_events['created_at'].dt.strftime('%H:%M:%S')

            # Replace dates with "Today" and "Yesterday"
            today = datetime.now(ZoneInfo(CONFIG['TIMEZONE'])).date()
            yesterday = today - timedelta(days=1)
            date_format = CONFIG['DATE_FORMAT'].lower().replace('yyyy', '%Y').replace('mm', '%m').replace('dd', '%d')
            df_events['date_display'] = df_events['date'].apply(
                lambda date: _("Today") if date == today else (_("Yesterday") if date == yesterday else date.strftime(date_format))
            )

            # Show the cat name instead of the RFID, and prepare thumbnails
            cat_name_dict = get_cat_name_rfid_dict(CONFIG['KITTYHACK_DATABASE_PATH'])
            # Build a dict: rfid -> (cat_id, name)
            df_cats = db_get_cats(CONFIG['KITTYHACK_DATABASE_PATH'], ReturnDataCatDB.all)
            rfid_to_catid = {row['rfid']: row['id'] for __, row in df_cats.iterrows() if row['rfid']}
            cat_thumbnails = {}
            for rfid, cat_id in rfid_to_catid.items():
                thumb = get_cat_thumbnail(CONFIG['KITTYHACK_DATABASE_PATH'], cat_id)
                if thumb:
                   
                    cat_thumbnails[rfid] = thumb

            def cat_name_with_icon(rfid):
                name = cat_name_dict.get(rfid, _("Unknown RFID") + f": {rfid}" if rfid else _("No RFID found"))
                thumb = cat_thumbnails.get(rfid)
                if thumb:
                    return f'<img src="data:image/jpeg;base64,{thumb}" style="width:24px;height:24px;border-radius:50%;vertical-align:middle;margin-right:6px;"> {name}'
                else:
                    return name

            df_events['cat_name'] = df_events['rfid'].apply(cat_name_with_icon)

            # Process event types into HTML with icons
            event_icons = {}
            for __, row in df_events.iterrows():
                event_type = row['event_type']
                
                # Handle comma-separated event types
                if ',' in event_type:
                    event_type_list = event_type.split(',')
                    # Process each event type and combine the results
                    icons_list = []
                    tooltip_parts = []
                    for et in event_type_list:
                        icons_list.extend(EventType.to_icons(et.strip()))
                        tooltip_parts.append(EventType.to_pretty_string(et.strip()))
                    
                    icons_html = " ".join(str(icon) for icon in icons_list)
                    tooltip_text = " + ".join(tooltip_parts)
                else:
                    # Process single event type as before
                    icons_html = " ".join(str(icon) for icon in EventType.to_icons(event_type))
                    tooltip_text = EventType.to_pretty_string(event_type)
                
                event_icons[row.name] = {
                    'icons_html': icons_html,
                    'tooltip_text': tooltip_text
                }

            # Start building the HTML table
            html = '<table class="dataframe shiny-table table w-100">'
            html += '<tbody>'

            # Iterate through the events and add date rows when the date changes
            last_date = None
            for idx, row in df_events.iterrows():
                if row['date_display'] != last_date:
                    html += f'<tr class="date-separator-row"><td colspan="4" class="event-date-separator">{row["date_display"]}</td></tr>'
                    last_date = row['date_display']
                
                html += '<tr>'
                html += f'<td>{row["time"]}</td>'
                event_info = event_icons[idx]
                html += f'<td><div class="tooltip-wrapper" title="{event_info["tooltip_text"]}"><div>{event_info["icons_html"]}</div></div></td>'
                html += f'<td>{row["cat_name"]}</td>'
                unique_id = hashlib.md5(os.urandom(16)).hexdigest()
                btn_id = f"btn_show_event_{unique_id}"
                html += f'<td><div>{btn_show_event(btn_id)}</div></td>'
                show_event_server(btn_id, row['block_id'])
                html += '</tr>'

            html += '</tbody></table>'
            return ui.HTML(html)
        except Exception as e:
            logging.error(f"Failed to read events from the database: {e}")
            return ui.HTML(
                '<table class="dataframe shiny-table table w-auto">'
                '<tbody><tr><td class="error">' + _('Failed to read events from the database.') + '</td></tr></tbody>'
                '</table>'
            )

    @output
    @render.ui
    def ui_system():
        camera_driver_action = ui.div(
            ui.hr(),
            ui.div(
                ui.input_task_button("reinstall_camera_driver", _("Reinstall Camera Driver"), icon=icon_svg("rotate-right"), class_="btn-default"),
                style_="text-align: center;"
            ),
            ui.help_text(_("Reinstall the camera driver if the live view does not work properly.")),
            ui.br(),
        ) if not is_remote_mode() else ui.HTML("")

        return ui.div(
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(_("Kittyflap System Actions"), style_="text-align: center;"),
                    ),
                    ui.br(),
                    ui.markdown(_("Start tasks/actions on the Kittyflap")),
                    ui.br(),
                    ui.div(
                        ui.input_action_button("bRestartKittyflap", _("Restart Kittyflap"), class_="btn-default"),
                        style_="text-align: center;"
                    ),
                    ui.br(),
                    ui.div(
                        ui.input_action_button("bShutdownKittyflap", _("Shutdown Kittyflap"), class_="btn-default"),
                        style_="text-align: center;"
                    ),
                    ui.help_text(_("To avoid data loss, always shut down the Kittyflap properly before unplugging the power cable. After a shutdown, wait 30 seconds before unplugging the power cable. To start the Kittyflap again, just plug in the power again.")),
                    camera_driver_action,
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),
            ui.br(),
            ui.br()
        )
    
    @reactive.Effect
    @reactive.event(input.reinstall_camera_driver)
    def on_action_reinstall_camera_driver():
        if is_remote_mode():
            return
        m = ui.modal(
            _("Do you really want to reinstall the camera driver? This operation can take several minutes."),
            title=_("Reinstall Camera Driver"),
            easy_close=False,
            footer=ui.div(
                ui.input_task_button("btn_modal_reinstall_cam_ok", _("OK")),
                ui.input_action_button("btn_modal_cancel", _("Cancel")),
            )
        )
        ui.modal_show(m)
    
    @reactive.effect
    @reactive.event(input.btn_modal_reinstall_cam_ok)
    def reinstall_camera_driver_process():
        if is_remote_mode():
            return
        # Read the dependencies (*.deb packages) from the "camera_dependencies.txt" file
        dependencies_file = "./camera_dependencies.txt"
        dependencies_url = "https://github.com/floppyFK/kittyhack-dependencies/raw/refs/heads/main/camera/"
        download_path = "/tmp/kittyhack-dependencies/camera/"

        with open(dependencies_file, "r") as file:
            dependencies = file.readlines()
        installation_steps = 4 + len(dependencies)

        with ui.Progress(min=1, max=installation_steps) as p:
            p.set(message=_("Reinstall Camera Driver in progress."), detail=_("This may take a while..."))
            i = 0

            try:
                # Step 1: Stop the camera
                msg = "Stopping the camera"
                msg_localized = _("Stopping the camera")
                i += 1
                p.set(i, message=msg_localized)
                logging.info(msg)
                sigterm_monitor.halt_backend()
                tm.sleep(1.0)

                # Step 2: Download all dependencies for the camera driver
                msg = "Downloading all dependencies"
                msg_localized = _("Downloading all dependencies")
                i += 1
                p.set(i, message=msg_localized)
                logging.info(msg)
                
                # Ensure the download path exists
                os.makedirs(download_path, exist_ok=True)                

                for dependency in dependencies:
                    dependency = dependency.strip()
                    if dependency:
                        dependency_url = f"{dependencies_url}{dependency}"
                        dependency_path = os.path.join(download_path, dependency)
                        msg = f"Downloading {dependency}"
                        msg_localized = _("Download") + " " + dependency
                        i += 1
                        p.set(i, message=msg_localized)
                        logging.info(msg)
                        if not execute_update_step(f"wget -O {dependency_path} {dependency_url}", msg):
                            raise subprocess.CalledProcessError(1, f"wget {dependency_url}")

                # Step 3: Uninstall all libcamera packages above version 0.3 (if there are any)
                msg = "Uninstalling libcamera packages above version 0.3"
                msg_localized = _("Uninstalling libcamera packages above version 0.3")
                i += 1
                p.set(i, message=msg_localized)
                logging.info(msg)
                result = subprocess.run("dpkg -l | grep libcamera", shell=True, capture_output=True, text=True, check=True)
                pattern = r'libcamera(\d+)\.(\d+)'
                for line in result.stdout.splitlines():
                    if line.startswith("ii"):
                        parts = line.split()
                        package_name = parts[1].split(":")[0]  # Remove architecture suffix
                        if package_name.startswith("libcamera"):
                            # Try to match the version pattern
                            match = re.search(pattern, package_name)
                            if match:
                                major_version = int(match.group(1))
                                minor_version = int(match.group(2))
                                version = float(f"{major_version}.{minor_version}")
                                if version > 0.3:
                                    logging.info(f"Uninstalling package {parts[1]} (version {version})")
                                    execute_update_step(f"apt-get remove -y {parts[1]}", f"Uninstalling package {parts[1]}")
                
                # Step 4: Install all dependencies
                msg = "Installing all dependencies"
                msg_localized = _("Installing all dependencies")
                i += 1
                p.set(i, message=msg_localized)
                logging.info(msg)
                dependencies_paths = " ".join([os.path.join(download_path, dep.strip()) for dep in dependencies if dep.strip()])
                if not execute_update_step(f"dpkg -i {dependencies_paths}", msg):
                    raise subprocess.CalledProcessError(1, f"dpkg -i {dependencies_paths}")

            except subprocess.CalledProcessError as e:
                ui.modal_remove()
                logging.error(f"An error occurred during the installation process: {e}")
                ui.notification_show(_("An error occurred during the installation process. Please check the logs for details."), duration=None, type="error")

            else:
                logging.info(f"Camera driver reinstallation successful.")
                # Show the restart dialog
                ui.modal_remove()
                m = ui.modal(
                    _("A restart is required to apply the update. Do you want to restart the Kittyflap now?"),
                    title=_("Restart required"),
                    easy_close=False,
                    footer=ui.div(
                        ui.input_action_button("btn_modal_reboot_ok", _("OK")),
                        ui.input_action_button("btn_modal_cancel", _("Cancel")),
                    )
                )
                ui.modal_show(m)
    
    @reactive.effect
    @reactive.event(input.btn_modal_cancel)
    def modal_cancel():
        ui.modal_remove()
        # Reset update progress result if the restart-required modal was open
        state = get_update_progress()
        if state["result"] == "ok" or state["result"] == "reboot_dialog":
            set_update_progress(in_progress=False, result=None)

    @reactive.effect
    @reactive.event(input.btn_modal_reboot_ok)
    def modal_reboot():
        state = get_update_progress()
        if state["result"] == "ok" or state["result"] == "reboot_dialog":
            set_update_progress(in_progress=False, result=None)

        if is_remote_mode():
            try:
                from src.remote.control_client import RemoteControlClient

                client = RemoteControlClient.instance()
                client.ensure_started()
                if not client.wait_until_ready(timeout=5.0):
                    ui.notification_show(
                        _("Remote target is not connected. Cannot reboot both devices right now."),
                        duration=10,
                        type="error",
                    )
                    return

                if not client.request_target_reboot(timeout=5.0):
                    ui.notification_show(
                        _("Failed to trigger reboot on target device. Please check remote connection and try again."),
                        duration=10,
                        type="error",
                    )
                    return
            except Exception as e:
                logging.error(f"[REMOTE_MODE] Failed to trigger target reboot: {e}")
                ui.notification_show(
                    _("Failed to trigger reboot on target device: {}.").format(e),
                    duration=12,
                    type="error",
                )
                return

        ui.modal_remove()
        reboot_message = _("Kittyflap is rebooting now... This will take 1 or 2 minutes. Please reload the page after the restart.")
        if is_remote_mode():
            reboot_message = _("Both devices are rebooting now... This may take 1 or 2 minutes. Please reconnect and reload the page afterwards.")
        ui.modal_show(ui.modal(reboot_message, title=_("Restart Kittyflap"), footer=None))
        systemcmd(["/sbin/reboot"], CONFIG['SIMULATE_KITTYFLAP'])

    @reactive.Effect
    @reactive.event(input.bRestartKittyflap)
    def on_action_restart_system():
        m = ui.modal(
            _("Do you really want to restart the Kittyflap?"),
            title=_("Restart Kittyflap"),
            easy_close=True,
            footer=ui.div(
                ui.input_action_button("btn_modal_reboot_ok", _("OK")),
                ui.input_action_button("btn_modal_cancel", _("Cancel")),
            )
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_modal_shutdown_ok)
    def modal_shutdown():
        ui.modal_remove()
        ui.modal_show(ui.modal(_("Kittyflap is shutting down now... Please wait 30 seconds before unplugging the power."), title=_("Shutdown Kittyflap"), footer=None))
        systemcmd(["/usr/sbin/shutdown", "-H", "now"], CONFIG['SIMULATE_KITTYFLAP'])

    @reactive.Effect
    @reactive.event(input.bShutdownKittyflap)
    def on_action_shutdown_system():
        m = ui.modal(
            _("Do you really want to shut down the Kittyflap?"),
            title=_("Shutdown Kittyflap"),
            easy_close=True,
            footer=ui.div(
                ui.input_action_button("btn_modal_shutdown_ok", _("OK")),
                ui.input_action_button("btn_modal_cancel", _("Cancel")),
            )
        )
        ui.modal_show(m)

    @output
    @render.ui
    @reactive.event(reload_trigger_cats, reload_trigger_config, ignore_none=True)
    def ui_manage_cats():
        ui_cards = []
        df_cats = db_get_cats(CONFIG['KITTYHACK_DATABASE_PATH'], ReturnDataCatDB.all)
        if not df_cats.empty:
            for __, data_row in df_cats.iterrows():
                # --- Picture ---
                if data_row["cat_image"]:
                    try:
                        decoded_picture = base64.b64encode(data_row["cat_image"]).decode('utf-8')
                    except:
                        decoded_picture = None
                else:
                    decoded_picture = None
                img_html = (
                    f'<div style="text-align: center;"><img style="max-width: 400px !important;" '
                    f'src="data:image/jpeg;base64,{decoded_picture}" /></div>'
                    if decoded_picture else
                    '<div class="placeholder-image"><strong>' + _('No picture found!') + '</strong></div>'
                )

                entry_mode_per_cat = (CONFIG['ALLOWED_TO_ENTER'].value == 'configure_per_cat')
                exit_mode_per_cat  = (CONFIG['ALLOWED_TO_EXIT'].value == 'configure_per_cat')
                entry_style = "padding-bottom: 20px;" if entry_mode_per_cat else "pointer-events: none; opacity: 0.6;"
                exit_style  = "padding-bottom: 20px;" if exit_mode_per_cat else "pointer-events: none; opacity: 0.6;"
                prey_style  = "padding-bottom: 20px;" if CONFIG['MOUSE_CHECK_ENABLED'] else "pointer-events: none; opacity: 0.6;"

                settings_rows = []

                # Cat specific settings header
                settings_rows.append([
                    ui.row(
                        ui.column(
                            12,
                            ui.div(
                                ui.markdown(_("##### Cat-specific settings")),
                                style_="text-align: center;"
                            )
                        )
                    ),
                    ui.br(),
                ])

                # Prey detection
                settings_rows.append(
                    ui.row(
                        ui.column(
                            12,
                            ui.div(
                                ui.input_switch(
                                    id=f"mng_cat_prey_{data_row['id']}",
                                    label=_("Enable Prey detection"),
                                    value=bool(int(data_row.get('enable_prey_detection', 1)))
                                ),
                                style_=prey_style
                            )
                        )
                    )
                )
                if not CONFIG['MOUSE_CHECK_ENABLED']:
                    settings_rows.append(
                        ui.row(
                            ui.column(
                                12,
                                ui.markdown(
                                    _("**Disabled:** Global prey detection is turned off in the `CONFIGURATION` section. Enable `Detect prey` to use per-cat settings.")
                                ), style_="color: grey;"
                            ),
                            style_="padding-bottom: 20px;"
                        )
                    )

                # Allow entry switch
                settings_rows.append(
                    ui.row(
                        ui.column(
                            12,
                            ui.div(
                                ui.input_switch(
                                    id=f"mng_cat_allow_entry_{data_row['id']}",
                                    label=_("Allow entry"),
                                    value=bool(int(data_row.get('allow_entry', 1)))
                                ),
                                style_=entry_style
                            )
                        )
                    )
                )
                if not entry_mode_per_cat:
                    settings_rows.append(
                        ui.row(
                            ui.column(
                                12,
                                ui.markdown(
                                    _("**Disabled:** This feature is only available in `Individual configuration per cat` mode for entry.")
                                ), style_="color: grey;"
                            ),
                            style_="padding-bottom: 20px;"
                        )
                    )

                # Allow exit switch
                settings_rows.append(
                    ui.row(
                        ui.column(
                            12,
                            ui.div(
                                ui.input_switch(
                                    id=f"mng_cat_allow_exit_{data_row['id']}",
                                    label=_("Allow exit"),
                                    value=bool(int(data_row.get('allow_exit', 1)))
                                ),
                                style_=exit_style
                            )
                        )
                    )
                )
                # Show warning if per-cat exit mode is active but no RFID assigned
                if exit_mode_per_cat and not data_row.get('rfid'):
                    settings_rows.append(
                        ui.row(
                            ui.column(
                                12,
                                ui.markdown(
                                    f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                                    + _("This cat has no RFID configured. The individual exit per cat works only for cats with a RFID chip!")
                                ),
                                style_="color:#b94a48;"
                            ),
                            style_="padding-bottom: 20px;"
                        )
                    )
                if not exit_mode_per_cat:
                    settings_rows.append(
                        ui.row(
                            ui.column(
                                12,
                                ui.markdown(
                                    _("**Disabled:** This feature is only available in `Individual configuration per cat` mode for exit.")
                                ), style_="color: grey;"
                            ),
                            style_="padding-bottom: 20px;"
                        )
                    )

                settings_section = ui.div(
                    ui.div(
                        *settings_rows,
                        class_="cat-settings-container",
                    ),
                    class_="align-left",
                )

                # --- Assemble card ---
                ui_cards.append(
                    ui.card(
                        ui.card_header(
                            ui.div(
                                ui.column(
                                    12,
                                    ui.input_text(
                                        id=f"mng_cat_name_{data_row['id']}",
                                        label=_("Name"),
                                        value=data_row['name'],
                                        width="100%"
                                    )
                                ),
                                ui.br(),
                                ui.column(
                                    12,
                                    ui.input_text(
                                        id=f"mng_cat_rfid_{data_row['id']}",
                                        label=_("RFID"),
                                        value=data_row['rfid'],
                                        width="100%"
                                    )
                                ),
                                ui.column(
                                    12,
                                    ui.div(
                                        id=f"mng_cat_rfid_status_{data_row['id']}",
                                        class_="rfid-status rfid-empty"
                                    )
                                ),
                                ui.column(12, ui.help_text(_("NOTE: This is NOT the number which stands in the booklet of your vet! You must use the the ID, which is read by the Kittyflap. It is 16 characters long and consists of numbers (0-9) and letters (A-F)."))),
                                ui.column(12, ui.help_text(_("If you have entered the RFID correctly here, the name of the cat will be displayed in the [PICTURES] section."))),
                                ui.br(),
                                settings_section,
                                ui.br(),
                                ui.column(
                                    12,
                                    uix.input_file(
                                        id=f"mng_cat_pic_{data_row['id']}",
                                        label=_("Change Picture"),
                                        accept=[".jpg", ".png"],
                                        width="100%"
                                    )
                                ),
                            )
                        ),
                        ui.HTML(img_html),
                        ui.card_footer(
                            ui.div(
                                ui.input_checkbox(
                                    id=f"mng_cat_del_{data_row['id']}",
                                    label=_("Delete {} from the database").format(data_row['name']),
                                    value=False
                                ),
                                style_="padding-top: 20px; display: flex; justify-content: center;"
                            )
                        ),
                        full_screen=False,
                        class_="image-container"
                    )
                )
            return ui.div(
                                ui.tags.div(
                                        {
                                                "id": "kh_manage_cats_i18n",
                                                "style": "display:none;",
                                                "data-msg-empty": _("No RFID entered. Cat identification only via camera (if enabled). See CONFIGURATION section for details."),
                                                "data-msg-valid": _("Valid RFID"),
                                                "data-msg-invalid": _("Invalid RFID. Must be exactly 16 hex characters (0-9, A-F)."),
                                        }
                                ),
                ui.div(
                    *ui_cards,
                    id="manage_cats_container",
                    style_="display: flex; flex-direction: column; align-items: center; gap: 20px;"
                ),
                ui.panel_absolute(
                    ui.panel_well(
                        ui.input_action_button(
                            id="mng_cat_save_changes",
                            label=_("Save all changes"),
                            icon=icon_svg("floppy-disk")
                        ),
                        class_="sticky-action-well",
                        style_="text-align: center;"
                    ),
                    draggable=False, width="100%", left="0px", right="0px", bottom="0px", fixed=True,
                ),
            )
        else:
            return ui.div(
                ui.help_text(_("No cats found in the database. Please go to the [ADD NEW CAT] section to add a new cat.")),
            )
        
    @reactive.Effect
    @reactive.event(input.mng_cat_save_changes)
    def manage_cat_save():
        df_cats = db_get_cats(CONFIG['KITTYHACK_DATABASE_PATH'], ReturnDataCatDB.all_except_photos)
        updated_cats = []
        if not df_cats.empty:
            for index, data_row in df_cats.iterrows():
                db_id = data_row["id"]
                db_name = data_row["name"]
                db_rfid = data_row["rfid"]
                db_prey = bool(int(data_row.get("enable_prey_detection", 1)))
                db_allow_entry = bool(int(data_row.get("allow_entry", 1)))
                db_allow_exit = bool(int(data_row.get("allow_exit", 1)))

                card_name = input[f"mng_cat_name_{db_id}"]()
                card_rfid = input[f"mng_cat_rfid_{db_id}"]().strip().upper()
                card_prey = input[f"mng_cat_prey_{db_id}"]()
                # Only read new per-cat switches if the mode is per-cat; otherwise keep previous DB values
                if CONFIG['ALLOWED_TO_ENTER'].value == 'configure_per_cat':
                    card_allow_entry = input[f"mng_cat_allow_entry_{db_id}"]()
                else:
                    try:
                        row = db_get_cats(CONFIG['KITTYHACK_DATABASE_PATH'], ReturnDataCatDB.all_except_photos)
                        prev = row[row['id']==db_id].iloc[0]
                        card_allow_entry = bool(int(prev.get('allow_entry', 1)))
                    except Exception:
                        card_allow_entry = True
                if CONFIG['ALLOWED_TO_EXIT'].value == 'configure_per_cat':
                    card_allow_exit = input[f"mng_cat_allow_exit_{db_id}"]()
                else:
                    try:
                        row = db_get_cats(CONFIG['KITTYHACK_DATABASE_PATH'], ReturnDataCatDB.all_except_photos)
                        prev = row[row['id']==db_id].iloc[0]
                        card_allow_exit = bool(int(prev.get('allow_exit', 1)))
                    except Exception:
                        card_allow_exit = True
                card_del = input[f"mng_cat_del_{db_id}"]()

                # Check if the cat should be deleted
                if card_del:
                    updated_cats.append(db_id)
                    result = db_delete_cat_by_id(CONFIG['KITTYHACK_DATABASE_PATH'], db_id)
                    if result.success:
                        ui.notification_show(_("{} deleted successfully from the database.").format(db_name), duration=5, type="message")
                    else:
                        ui.notification_show(_("Failed to delete {} from the database: {}").format(db_name, result.message), duration=10, type="error")
                else:                    
                    # Get image path, if a file was uploaded
                    card_pic: list[FileInfo] | None = input[f"mng_cat_pic_{db_id}"]()
                    if card_pic is not None:
                        card_pic_path = card_pic[0]['datapath']
                    else:
                        card_pic_path = None

                    # Only update the cat data if the values have changed
                    if (db_name != card_name) or (db_rfid != card_rfid) or (db_prey != card_prey) or (db_allow_entry != card_allow_entry) or (db_allow_exit != card_allow_exit) or (card_pic_path is not None):
                        # Add the ID to the list of updated cats
                        updated_cats.append(db_id)

                        result = db_update_cat_data_by_id(CONFIG['KITTYHACK_DATABASE_PATH'], db_id, card_name, card_rfid, card_pic_path, card_prey, card_allow_entry, card_allow_exit)
                        if result.success:
                            ui.notification_show(_("Data for {} updated successfully.").format(card_name), duration=5, type="message")
                        else:
                            ui.notification_show(_("Failed to update cat details: {}").format(result.message), duration=10, type="error")
            
            if not updated_cats:
                ui.notification_show(_("No changes detected. Nothing to save."), duration=5, type="message")
            else:
                reload_trigger_cats.set(reload_trigger_cats.get() + 1)
        
    @output
    @render.ui
    @reactive.event(reload_trigger_cats, reload_trigger_config, ignore_none=True)
    def ui_add_new_cat():
        ui_cards = []

        # Flags for per-cat modes
        entry_mode_per_cat = (CONFIG['ALLOWED_TO_ENTER'].value == 'configure_per_cat')
        exit_mode_per_cat  = (CONFIG['ALLOWED_TO_EXIT'].value == 'configure_per_cat')
        entry_style = "padding-bottom: 20px;" if entry_mode_per_cat else "pointer-events: none; opacity: 0.6;"
        exit_style  = "padding-bottom: 20px;" if exit_mode_per_cat else "pointer-events: none; opacity: 0.6;"
        prey_style  = "padding-bottom: 20px;" if CONFIG['MOUSE_CHECK_ENABLED'] else "pointer-events: none; opacity: 0.6;"

        # Build settings section inline
        settings_rows_new = [
            ui.row(
                ui.column(
                    12,
                    ui.div(
                        ui.markdown(_("##### Cat-specific settings")),
                        style_="text-align: center;"
                    )
                )
            ),
            ui.br(),
            ui.row(
                ui.column(
                    12,
                    ui.div(
                        ui.input_switch("add_new_cat_prey", _("Enable Prey detection"), True),
                        style_=prey_style
                    )
                )
            )
        ]
        if not CONFIG['MOUSE_CHECK_ENABLED']:
            settings_rows_new.append(
                ui.row(
                    ui.column(
                        12,
                        ui.markdown(
                            _("**Disabled:** Global prey detection is turned off in the `CONFIGURATION` section. Enable `Detect prey` to use per-cat settings.")
                        ), style_="color: grey;"
                    ),
                    style_="padding-bottom: 20px;"
                )
            )
        settings_rows_new.append(
            ui.row(
                ui.column(
                    12,
                    ui.div(
                        ui.input_switch("add_new_cat_allow_entry", _("Allow entry"), True),
                        style_=entry_style
                    )
                )
            )
        )
        if not entry_mode_per_cat:
            settings_rows_new.append(
                ui.row(
                    ui.column(
                        12,
                        ui.markdown(
                            _("**Disabled:** This feature is only available in `Individual configuration per cat` mode for entry.")
                        ), style_="color: grey;"
                    ),
                    style_="padding-bottom: 20px;"
                )
            )
        settings_rows_new.append(
            ui.row(
                ui.column(
                    12,
                    ui.div(
                        ui.input_switch("add_new_cat_allow_exit", _("Allow exit"), True),
                        style_=exit_style
                    )
                )
            )
        )
        if not exit_mode_per_cat:
            settings_rows_new.append(
                ui.row(
                    ui.column(
                        12,
                        ui.markdown(
                            _("**Disabled:** This feature is only available in `Individual configuration per cat` mode for exit.")
                        ), style_="color: grey;"
                    ),
                    style_="padding-bottom: 20px;"
                )
            )

        settings_rows_new.append([
            ui.hr(),
            ui.row(
                ui.column(
                    12,
                    ui.div(
                        ui.markdown(
                            _(
                                "**Important:** Please read the notes about the prerequisites for the "
                                "individual entry/exit configuration in the `MANAGE CATS` section "
                                "if you want to use these features."
                            )
                        ),
                        style_="text-align: center;"
                    )
                )
            )
        ])

        settings_section_new_cat = ui.div(
            ui.div(
                *settings_rows_new,
                class_="cat-settings-container",
            ),
            class_="align-left"
        )

        ui_cards.append(
            ui.card(
                ui.card_header(
                    ui.div(
                        ui.h5(_("Add new cat")),
                        ui.column(12, ui.input_text("add_new_cat_name", label=_("Name"), value="", width="100%")),
                        ui.br(),
                        ui.column(12, ui.input_text("add_new_cat_rfid", label=_("RFID"), value="", width="100%")),
                        ui.column(12, ui.output_ui("add_new_cat_rfid_status")),
                        ui.br(),
                        ui.column(12, ui.help_text(_("You can find the RFID in the [PICTURES] section, if the chip of your cat was recognized by the Kittyflap. To read the RFID, just set the entrance mode to 'All Cats' and let pass your cat through the Kittyflap."))),
                        ui.column(12, ui.help_text(_("NOTE: This is NOT the number which stands in the booklet of your vet! You must use the the ID, which is read by the Kittyflap. It is 16 characters long and consists of numbers (0-9) and letters (A-F)."))),
                        ui.br(),
                        settings_section_new_cat,
                        ui.br(),
                        ui.column(12, uix.input_file("add_new_cat_pic", label=_("Upload Picture"), accept=".jpg", width="100%")),
                        ui.hr(),
                        ui.column(12, ui.input_action_button("add_new_cat_save", label=_("Save"), icon=icon_svg("floppy-disk"))),
                    )
                ),
                full_screen=False,
                class_="image-container"
            )
        )
        return ui.layout_column_wrap(*ui_cards, width="400px"),

    @output
    @render.ui
    def add_new_cat_rfid_status():
        val = (input.add_new_cat_rfid() or "").strip()
        if val == "":
            return ui.div(
                _("No RFID entered. Cat identification only via camera (if enabled). See CONFIGURATION section for details."),
                class_="rfid-status rfid-empty"
            )
        if re.fullmatch(r"[0-9A-Fa-f]{16}", val):
            return ui.div(
                _("Valid RFID"),
                class_="rfid-status rfid-valid"
            )
        return ui.div(
            _("Invalid RFID. Must be exactly 16 hex characters (0-9, A-F)."),
            class_="rfid-status rfid-invalid"
        )
    
    @reactive.Effect
    @reactive.event(input.add_new_cat_save)
    def add_new_cat_save():
        cat_name = input.add_new_cat_name()
        cat_rfid = input.add_new_cat_rfid().strip().upper()
        cat_pic: list[FileInfo] | None = input.add_new_cat_pic()
        cat_prey = input.add_new_cat_prey()
        if CONFIG['ALLOWED_TO_ENTER'].value == 'configure_per_cat':
            cat_allow_entry = input.add_new_cat_allow_entry()
        else:
            cat_allow_entry = True
        if CONFIG['ALLOWED_TO_EXIT'].value == 'configure_per_cat':
            cat_allow_exit = input.add_new_cat_allow_exit()
        else:
            cat_allow_exit = True
        
        # Get image path, if a file was uploaded
        if cat_pic is not None:
            cat_pic_path = cat_pic[0]['datapath']
        else:
            cat_pic_path = None

        result = db_add_new_cat(CONFIG['KITTYHACK_DATABASE_PATH'], cat_name, cat_rfid, cat_pic_path, cat_prey, cat_allow_entry, cat_allow_exit)
        if result.success:
            ui.notification_show(_("New cat {} added successfully.").format(cat_name), duration=5, type="message")
            ui.update_text(id="add_new_cat_name", value="")
            ui.update_text(id="add_new_cat_rfid", value="")
            ui.update_switch(id="add_new_cat_prey", value=True)
            ui.update_switch(id="add_new_cat_allow_entry", value=True)
            ui.update_switch(id="add_new_cat_allow_exit", value=True)
            reload_trigger_cats.set(reload_trigger_cats.get() + 1)
        else:
            ui.notification_show(_("An error occurred while adding the new cat: {}").format(result.message), duration=10, type="error")

    @output
    @render.ui
    @reactive.event(reload_trigger_ai, ignore_none=True)
    def ui_ai_training():

        # Check labelstudio
        if CONFIG["LABELSTUDIO_VERSION"] is not None:
            labelstudio_latest_version = get_labelstudio_latest_version()
            labelstudio_latest_version_display = labelstudio_latest_version or _("unknown")
            
            # Check if labelstudio is running
            if get_labelstudio_status() == True:
                ui_labelstudio = ui.div(
                    ui.row(
                        ui.column(
                            12,
                            ui.input_task_button("btn_labelstudio_stop", _("Stop Label Studio"), icon=icon_svg("stop")),
                            ui.br(),
                            ui.help_text(_("Label Studio is running. Click the button to stop it.")),
                            ui.br(),
                            ui.help_text(_("Remember to stop Label Studio after you are done with the labeling process, to free up resources for KittyHack.")),
                            style_="text-align: center;"
                        ),
                    ),
                    ui.row(
                        ui.column(
                            12,
                            # Inject an HTML button that links to the Label Studio web interface
                            ui.HTML('<a href="http://{}:8080" target="_blank" class="btn-default">{}</a>'.format(get_current_ip(), _("Open Label Studio"))),
                            style_="text-align: center;"
                        ),
                        ui.column(
                            12,
                            ui.help_text(_("Installed Version: ") + CONFIG["LABELSTUDIO_VERSION"]),
                            style_="text-align: center; padding-top: 20px;"
                        ),
                        ui.column(
                            12,
                            ui.help_text(_("Latest version: ") + labelstudio_latest_version_display),
                            style_="text-align: center;"
                        ),
                        style_ ="padding-top: 50px;"
                    )
                )
            else:
                ui_labelstudio = ui.row(
                    ui.column(
                        12,
                        ui.input_task_button("btn_labelstudio_start", _("Start Label Studio"), icon=icon_svg("play")),
                        ui.br(),
                        ui.help_text(_("Label Studio is not running. Click the button to start it.")),
                        style_="text-align: center;"
                    ),
                )

            if labelstudio_latest_version is not None and labelstudio_latest_version != CONFIG["LABELSTUDIO_VERSION"]:
                ui_labelstudio = ui_labelstudio, ui.row(
                    ui.column(
                        12,
                        ui.input_task_button("btn_labelstudio_update", _("Update Label Studio"), icon=icon_svg("circle-up"), class_="btn-primary"),
                        ui.br(),
                        ui.help_text(_("Click the button to update Label Studio to the latest version.")),
                        ui.br(),
                        ui.help_text(_('Current Version') + ": " + CONFIG['LABELSTUDIO_VERSION']),
                        ui.br(),
                        ui.help_text(_('Latest Version') + ": " + labelstudio_latest_version_display),
                        style_="text-align: center;"
                    ),
                    style_="padding-top: 50px;"
                )

            ui_labelstudio = ui_labelstudio, ui.hr(), ui.row(
                ui.column(
                    12,
                    ui.input_task_button("btn_labelstudio_remove", _("Remove Label Studio"), icon=icon_svg("trash"), class_="btn-danger"),
                    ui.br(),
                    ui.help_text(_("Click the button to remove Label Studio.")),
                    ui.br(),
                    ui.help_text(_("(Your project data will not be deleted. You can re-install Label Studio later.)")),
                    style_="text-align: center;"
                ),
                style_ ="padding-top: 20px;"

            )

        # If labelstudio is not installed, show the install button
        else:
            ui_labelstudio = ui.div(
                ui.markdown(
                    _("#### You have two choices:") + "  \n" +
                    _("1. `EASY` **Install Label Studio automatically on the Kittyflap**:") + "  \n" +
                    _("Easy, but you may be limited in the number of images you can label. The performance may vary, depending on the wlan connection to the Kittyflap.") + "  \n" +
                    _("2. `EXPERT` **Install Label Studio on your own computer**:") + "  \n" +
                    _("This is a bit harder, but you are more flexible and the performance while labeling the images may be better. Also, you don't need to worry about the limited disk space on the Kittyflap. See the [Label Studio](https://labelstud.io/) website for instructions.") + "  \n" +
                    _("> **Please note, if you want to install Label Studio on the Kittyflap:**") + "  \n" +
                    _("Some Kittyflaps have only 1GB of RAM. In this case, it is strongly recommended to always stop the Label Studio server after you are done with the labeling process, otherwise the Kittyflap may run out of memory.") + "  \n" +
                    _("You can check the available disk space and the RAM configuration in the `INFO` section.")
                ),
                ui.hr(),
                ui.column(
                    12,
                    ui.input_task_button("btn_labelstudio_install", _("Install Label Studio on the Kittyflap")),
                    ui.br(),
                    ui.help_text(_("Click the button to install Label Studio.")),
                    ui.br(),
                    ui.help_text(_("This will take 5-10 minutes, so please be patient.")),
                    ui.br(),
                    ui.help_text(_("The Kittyflap may not be reachable during the installation. This is normal.")),
                    style_="text-align: center;"
                ),
            )

        # Check if a model training is in progress
        training_status = RemoteModelTrainer.check_model_training_result(show_in_progress=True, return_pretty_status=True)

        # Show user notifications, if they are any
        show_user_notifications()

        # URLs for different languages
        wiki_url = {
            "de": "https://github.com/floppyFK/kittyhack/wiki/%5BDE%5D-Kittyhack-v2.0-%E2%80%90-Eigene-KI%E2%80%90Modelle-trainieren",
            "en": "https://github.com/floppyFK/kittyhack/wiki/%5BEN%5D-Kittyhack-v2.0-%E2%80%90-Train-own-AI%E2%80%90Models"
        }.get(CONFIG["LANGUAGE"], "https://github.com/floppyFK/kittyhack/wiki/%5BEN%5D-Kittyhack-v2.0-%E2%80%90-Train-own-AI%E2%80%90Models")
        
        # --- Server maintenance check when no training is in progress ---
        server_status = None
        training_in_progress = is_valid_uuid4(CONFIG["MODEL_TRAINING"])
        if not training_in_progress:
            server_status = RemoteModelTrainer.get_server_status()

        # Build Model Training section content depending on maintenance state or active training
        if training_in_progress:
            # Already in progress: show existing status UI only
            training_content = ui.div(
                ui.markdown(_("A model training is currently in progress.") + (_("You will be notified by email when the training is finished.") if CONFIG['EMAIL'] else "")),
                ui.markdown(_("Current status: {}").format(training_status)),
                ui.br(),
                ui.br(),
                ui.input_task_button("btn_reload_model_training_status", _("Reload Model Training Status"), class_="btn-primary"),
                ui.hr(),
                ui.markdown(_("Your individual Training ID:") + "\n\n" + f"`{CONFIG['MODEL_TRAINING']}`"),
                ui.help_text(_("(use this ID for support requests)")),
                ui.br(),
                ui.br(),
                ui.input_task_button("btn_cancel_model_training", _("Cancel Model Training"), class_="btn-danger"),
                id="model_training_status",
                style_="text-align: center;"
            )
        else:
            # No training in progress: check maintenance or timeout/error
            is_maintenance = bool(server_status and server_status.get("maintenance", False))
            maintenance_msg = (server_status or {}).get("message", "")
            if server_status is None:
                # Timeout or request failure: inform user
                training_content = ui.div(
                    ui.markdown(f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} " +
                                _("Unable to reach the model training server right now. Please check your network or try again later.")),
                    ui.br(),
                    ui.markdown(_("If the issue persists, the server may be temporarily unavailable.")),
                    style_="text-align: center;"
                )
            elif is_maintenance:
                # Show maintenance notice and do not render upload inputs
                msg = _("The model training server is currently in maintenance. Please try again later.")
                if maintenance_msg:
                    msg += "\n\n" + maintenance_msg
                training_content = ui.div(
                    ui.markdown(f"{icon_svg('wrench', margin_left='-0.1em')} {msg}"),
                    style_="text-align: center;"
                )
            else:
                # Show the original upload form
                model_training_base_model_input = ui.input_select(
                    "model_training_base_model",
                    _("YOLOv8 model"),
                    {
                        "n": "YOLOv8n",
                        "s": "YOLOv8s",
                        "m": "YOLOv8m",
                        "l": "YOLOv8l",
                        "x": "YOLOv8x",
                    },
                    selected="n",
                    width="90%",
                )
                model_training_image_size_input = ui.input_select(
                    "model_training_image_size",
                    _("Image size"),
                    {str(s): str(s) for s in YoloModel.get_supported_image_sizes()},
                    selected="320",
                    width="90%",
                )

                if not is_remote_mode():
                    model_training_base_model_input = _disable_input(model_training_base_model_input)
                    model_training_image_size_input = _disable_input(model_training_image_size_input)

                training_content = ui.div(
                    ui.div(
                        ui.div(
                            uix.input_file("model_training_data", _("Upload Label-Studio Training Data (ZIP file)"), accept=".zip", multiple=False, width="90%"),
                            ui.input_text("model_name", _("Model Name (optional)"), placeholder=_("Enter a name for your model"), width="90%"),
                            ui.input_text("user_name", _("Username (optional)"), value=CONFIG['USER_NAME'], placeholder=_("Enter your name"), width="90%"),
                            ui.input_text("email_notification", _("Email for Notification (optional)"), value=CONFIG['EMAIL'], placeholder=_("Enter your email address"), width="90%"),
                            ui.help_text(_("If you provide an email address, you will be notified when the model training is finished.")),
                            ui.tags.details(
                                ui.br(),
                                ui.tags.summary(_("Advanced options")),
                                ui.br(),
                                model_training_base_model_input,
                                ui.help_text(
                                    _(
                                        "Base model (YOLOv8n/s/m/l/x): larger variants are usually more accurate, but they run slower and need more CPU/RAM. "
                                        "Choose smaller variants (e.g. 'n') if your device struggles to keep up in real-time."
                                    )
                                ),
                                model_training_image_size_input,
                                ui.help_text(
                                    _(
                                        "Image size (imgsz): higher values can improve detection of small objects, but increase computation and can reduce FPS."
                                    )
                                ),
                                (
                                    ui.help_text(_("These options are only available if you run Kittyhack on a dedicated system in remote mode."))
                                    if not is_remote_mode()
                                    else ui.help_text(_("Defaults: YOLOv8n and image size 320."))
                                ),
                                style="width: 90%; text-align: left; margin: 0 auto;",
                            ),
                            ui.br(),
                            ui.br(),
                            ui.input_task_button("submit_model_training", _("Submit Model for Training"), class_="btn-primary"),
                            id="model_training_form",
                            style_="display: flex; flex-direction: column; align-items: center; justify-content: center;"
                        ),
                        style_="text-align: center;"
                    ),
                )

        ui_ai_training =  ui.div(
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(_("Description"), style_="text-align: center;"),
                    ),
                    ui.br(),
                    ui.markdown(
                        _("In this section, you can train your own individual AI model for the Kittyflap. The training process consists of two steps:") + "  \n" +
                        _("1. **Label Studio**: In this step, you can label your cat(s) and their prey in the captured images. This is done using the Label Studio tool.") + "  \n" +
                        _("2. **Model Training**: In this step, the labeled images are used to train a new AI model.") + "  \n" +
                        _("To achieve the best results, it is recommended to label at least 100 images of each cat and their prey. The more images you label, the better the model will be.") + " " +
                        _("The training process can take from several minutes up to an hour, depending on the number of images.") + "  \n" +
                        _("Please read the instructions before creating your own model! Following these exact instructions is crucial for successful training:")
                    ),
                    ui.div(
                        ui.HTML(f'<a href="{wiki_url}" target="_blank" class="btn btn-default">' +
                               '<i class="fa fa-clipboard-list" style="margin-right: 5px;"></i>' + 
                               _("Instructions for training your own model") + '</a>'),
                        style_="text-align: center;"
                    ),
                    ui.br(),
                    full_screen=False,
                    class_="generic-container align-left",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4("Label Studio", style_="text-align: center;"),
                    ),
                    ui.br(),
                    ui_labelstudio,
                    ui.br(),
                    full_screen=False,
                    class_="generic-container align-left",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(_("Model Training"), style_="text-align: center;"),
                    ),
                    ui.br(),
                    training_content,
                    ui.br(),
                    full_screen=False,
                    class_="generic-container align-left",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(_("Model Management"), style_="text-align: center;"),
                    ),
                    ui.br(),
                    ui.div(_("Here you can rename or remove your own models. To activate a model, go to the [CONFIGURATION] section.")),
                    ui.output_ui("manage_yolo_models_table"),
                    full_screen=False,
                    class_="generic-container align-left",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),

            ui.br(),
            ui.br(),
            ui.br(),
            ui.br(),
            ui.br(),
        )
        return ui_ai_training
    
    @render.ui
    @reactive.event(reload_trigger_ai, ignore_none=True)
    def manage_yolo_models_table():
        # Keep the table reasonably fresh while the AI tab is open.
        reactive.invalidate_later(60)

        try:
            available_models = YoloModel.get_model_list() or []
            if not available_models:
                return ui.div(_("Nothing here yet. Please train a model first."), class_="text-muted small")

            rows = []
            for model in available_models:
                unique_btn_id = hashlib.md5(os.urandom(16)).hexdigest()
                unique_model_id = model.get('unique_id')
                if not unique_model_id:
                    continue

                is_active_model = (
                    (CONFIG.get('YOLO_MODEL') == unique_model_id) and
                    (not (CONFIG.get('TFLITE_MODEL_VERSION') or '').strip())
                )

                fps = model.get('effective_fps', None)
                fps_text = "—"
                try:
                    if fps is not None:
                        fps_text = f"{float(fps):.1f}"
                except Exception:
                    fps_text = "—"

                model_image_size = model.get('model_image_size', 320) or 320
                yolo_variant = (model.get('yolo_variant') or 'yolov8n.pt').strip() or 'yolov8n.pt'

                variant_letter = "N"
                variant_title = _("YOLO v8 Nano")
                variant_match = re.search(r"yolov\d+\s*([nslmx])", yolo_variant.lower())
                if variant_match:
                    v = variant_match.group(1)
                    variant_map = {
                        "n": ("N", _("YOLO v8 Nano")),
                        "s": ("S", _("YOLO v8 Small")),
                        "m": ("M", _("YOLO v8 Medium")),
                        "l": ("L", _("YOLO v8 Large")),
                        "x": ("X", _("YOLO v8 Extra Large")),
                    }
                    variant_letter, variant_title = variant_map.get(v, (v.upper(), _("YOLO v8 model")))

                def build_badges(id_suffix: str):
                    _badges = [
                        ui.tooltip(
                            ui.span(variant_letter, class_="badge text-bg-secondary"),
                            variant_title,
                            id=f"tooltip_yolo_variant_{unique_btn_id}_{id_suffix}",
                            options={"trigger": "hover"},
                        ),
                        ui.tooltip(
                            ui.span(str(int(model_image_size)), class_="badge text-bg-secondary"),
                            _("Model image size: {}px").format(int(model_image_size)),
                            id=f"tooltip_yolo_img_size_{unique_btn_id}_{id_suffix}",
                            options={"trigger": "hover"},
                        ),
                        ui.tooltip(
                            ui.span(f"{fps_text} FPS", class_="badge text-bg-secondary"),
                            _("Average FPS with this model"),
                            id=f"tooltip_yolo_fps_{unique_btn_id}_{id_suffix}",
                            options={"trigger": "hover"},
                        ),
                    ]
                    if is_active_model:
                        _badges.insert(
                            0,
                            ui.tooltip(
                                ui.span(_("Active"), class_="badge text-bg-success"),
                                _("Currently active model"),
                                id=f"tooltip_yolo_active_{unique_btn_id}_{id_suffix}",
                                options={"trigger": "hover"},
                            ),
                        )
                    return _badges

                badges_desktop = build_badges("desktop")
                badges_mobile = build_badges("mobile")

                actions = ui.div(
                    ui.tooltip(
                        btn_yolo_activate(f"btn_yolo_activate_{unique_btn_id}"),
                        _("Use this model now"),
                        id=f"tooltip_yolo_activate_{unique_btn_id}",
                        options={"trigger": "hover"},
                    ),
                    ui.tooltip(
                        btn_yolo_modify(f"btn_yolo_modify_{unique_btn_id}"),
                        _("Modify or delete this model"),
                        id=f"tooltip_yolo_modify_{unique_btn_id}",
                        options={"trigger": "hover"},
                    ),
                    class_="d-flex gap-1 justify-content-end",
                )

                # Register event listeners for the buttons
                activate_yolo_model_server(f"btn_yolo_activate_{unique_btn_id}", unique_model_id)
                manage_yolo_model_server(f"btn_yolo_modify_{unique_btn_id}", unique_model_id)

                name_cell = ui.div(
                    ui.div(
                        ui.span(model.get('display_name', ''), class_="kh-model-name"),
                        class_="d-flex align-items-start gap-2",
                    ),
                    ui.div(model.get('creation_date', ''), class_="kh-model-sub"),
                    ui.div(*badges_mobile, class_="d-flex d-sm-none flex-wrap gap-1 mt-1"),
                )

                specs_cell = ui.div(
                    ui.div(*badges_desktop, class_="d-none d-sm-flex flex-wrap gap-1 justify-content-center"),
                )

                rows.append(
                    ui.tags.tr(
                        ui.tags.td(name_cell),
                        ui.tags.td(specs_cell, class_="kh-model-specs kh-model-specs-col"),
                        ui.tags.td(actions, class_="kh-model-actions"),
                    )
                )

            table = ui.tags.table(
                ui.tags.tbody(*rows),
                class_="table table-sm align-middle table_models_overview",
            )

            return ui.div(table, class_="table-responsive")

        except Exception:
            return ui.div(_("Nothing here yet. Please train a model first."), class_="text-muted small")
    
    @reactive.Effect
    @reactive.event(input.btn_reload_model_training_status)
    def on_reload_model_training_status():
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)
        reload_trigger_config.set(reload_trigger_config.get() + 1)
    
    @reactive.Effect
    @reactive.event(input.btn_cancel_model_training)
    def on_cancel_model_training():
        # Check if a model training is in progress
        if not is_valid_uuid4(CONFIG["MODEL_TRAINING"]):
            ui.notification_show(_("No model training in progress."), duration=15, type="error")
            CONFIG["MODEL_TRAINING"] = ""
            update_single_config_parameter("MODEL_TRAINING")
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)
            return
    
        # Show confirmation dialog
        m = ui.modal(
            _("Do you really want to cancel the model training?"),
            title=_("Cancel Model Training"),
            easy_close=True,
            footer=ui.div(
                ui.input_action_button("btn_modal_cancel_training_ok", _("OK")),
                ui.input_action_button("btn_modal_cancel", _("Cancel")),
            )
        )
        ui.modal_show(m)
    
    @reactive.effect
    @reactive.event(input.btn_modal_cancel_training_ok)
    def modal_cancel_training():
        ui.modal_remove()
        # Cancel the model training
        RemoteModelTrainer.cancel_model_training(CONFIG["MODEL_TRAINING"])
        CONFIG["MODEL_TRAINING"] = ""
        update_single_config_parameter("MODEL_TRAINING")
        ui.notification_show(_("Model training cancelled."), duration=15, type="message")
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)
    
    @reactive.Effect
    @reactive.event(input.submit_model_training)
    def on_submit_model_training():
        # Check if a file was uploaded
        if input.model_training_data() is None:
            ui.notification_show(_("Please upload a ZIP file with the Label Studio training data."), duration=10, type="error")
            return

        # Check if the file is a ZIP file
        if not input.model_training_data()[0]['name'].endswith('.zip'):
            ui.notification_show(_("The uploaded file is not a ZIP file. Please upload a valid ZIP file."), duration=10, type="error")
            return

        # Get the uploaded file path
        zip_file_path = input.model_training_data()[0]['datapath']
        model_name = input.model_name()
        email_notification = input.email_notification()
        user_name = input.user_name()
        model_variant = str(input.model_training_base_model() or "n").strip().lower()
        image_size_raw = str(input.model_training_image_size() or "320").strip()

        # Advanced model parameters are only enabled in remote-mode.
        if not is_remote_mode():
            model_variant = "n"
            image_size_raw = "320"

        if model_variant not in {"n", "s", "m", "l", "x"}:
            ui.notification_show(_("Invalid YOLOv8 model selection."), duration=10, type="error")
            return

        try:
            image_size_candidate = int(image_size_raw)
        except Exception:
            ui.notification_show(_("Invalid image size selection."), duration=10, type="error")
            return

        supported_sizes = set(YoloModel.get_supported_image_sizes())
        if image_size_candidate not in supported_sizes:
            ui.notification_show(_("Invalid image size selection."), duration=10, type="error")
            return

        image_size = int(image_size_candidate)

        if email_notification:
            # Validate the email address
            if not re.match(r"[^@]+@[^@]+\.[^@]+", email_notification):
                ui.notification_show(_("Please enter a valid email address."), duration=10, type="error")
                return
            if email_notification != CONFIG['EMAIL']:
                # Update the email address in the config
                CONFIG['EMAIL'] = email_notification
                update_single_config_parameter("EMAIL")

        if user_name and (user_name != CONFIG['USER_NAME']):
            CONFIG['USER_NAME'] = user_name
            update_single_config_parameter("USER_NAME")
            
        logging.info(
            f"Enqueued model training: Model Name: '{model_name}', Email: '{email_notification}', "
            f"ZIP file: '{zip_file_path}', YOLOv8 variant: '{model_variant}', image size: {image_size}"
        )
        
        # Start the model training process
        result = RemoteModelTrainer.enqueue_model_training(
            zip_file_path,
            model_name,
            user_name,
            email_notification,
            yolo_model_variant=model_variant,
            image_size=image_size,
        )
        if is_valid_uuid4(result):
            # Update the config with the training details
            CONFIG["MODEL_TRAINING"] = result
            update_single_config_parameter("MODEL_TRAINING")
        elif result == "invalid_file":
            ui.notification_show(_("The uploaded file is not a valid Label Studio training data ZIP file."), duration=15, type="error")
            return
        elif result in ["destination_unreachable", "destination_not_found"]:
            ui.notification_show(_("The destination for the model training is unreachable. Please check your network connection or try again later."), duration=15, type="error")
            return
        else:
            ui.notification_show(_("An error occurred while starting the model training: {}").format(result), duration=15, type="error")
            return

        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    # --- Label Studio installation and update ---
    @reactive.Effect
    @reactive.event(input.btn_labelstudio_install)
    def btn_labelstudio_install():
        with ui.Progress(min=1, max=5) as p:
            # Check available disk space
            if get_free_disk_space() < 1500:
                ui.notification_show(
                    _("Insufficient disk space. At least 1.5GB of free space is required to install Label Studio. Please free up some space and try again."),
                    duration=15,
                    type="error"
                )
                return
                
            def update_progress(step, message, detail):
                p.set(step, message=message, detail=detail)
                
            # Start the installation with progress updates
            success = install_labelstudio(progress_callback=update_progress)
            
            if success:
                ui.notification_show(_("Label Studio installed successfully! You can start it now."), duration=15, type="message")
                CONFIG["LABELSTUDIO_VERSION"] = get_labelstudio_installed_version()
            else:
                ui.notification_show(_("Label Studio installation failed. Please check the logs for details."), duration=None, type="error")
                
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)
    
    @reactive.Effect
    @reactive.event(input.btn_labelstudio_update)
    def btn_labelstudio_update():
        with ui.Progress(min=1, max=2) as p:
            def update_progress(step, message, detail):
                p.set(step, message=message, detail=detail)
                
            # Start the installation with progress updates
            success = update_labelstudio(progress_callback=update_progress)
            
            if success:
                ui.notification_show(_("Label Studio updated successfully! You can start it now."), duration=15, type="message")
                CONFIG["LABELSTUDIO_VERSION"] = get_labelstudio_installed_version()
            else:
                ui.notification_show(_("Label Studio update failed. Please check the logs for details."), duration=None, type="error")
                
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    @reactive.Effect
    @reactive.event(input.btn_labelstudio_remove)
    def btn_labelstudio_remove():
        m = ui.modal(
            title=_("Remove Label Studio"),
            easy_close=True,
            footer=ui.div(
                ui.input_task_button("btn_labelstudio_remove_ok", _("OK"), class_="btn-default"),
                ui.input_action_button("btn_modal_cancel", _("Cancel")),
            )
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_labelstudio_remove_ok)
    def btn_labelstudio_remove_ok():
        with ui.Progress(min=1, max=2) as p:
            p.set(1, message=_("Removing Label Studio"), detail=_("Please wait..."))
            success = remove_labelstudio()
            p.set(2)
            
            if success:
                ui.notification_show(_("Label Studio removed successfully."), duration=5, type="message")
                CONFIG["LABELSTUDIO_VERSION"] = None
            else:
                ui.notification_show(_("Label Studio removal failed. Please check the logs for details."), duration=None, type="error")
        ui.modal_remove()
        
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    @reactive.Effect
    @reactive.event(input.btn_labelstudio_stop)
    def stop_labelstudio():
        systemctl("stop", "labelstudio")
        
        # Wait up to 10 seconds for the process to stop
        max_wait_time = 10  # seconds
        start_time = monotonic_time()
        
        with ui.Progress(min=0, max=100) as p:
            p.set(message=_("Stopping Label Studio..."), detail=_("Please wait..."))
            
            while monotonic_time() - start_time < max_wait_time:
                if not get_labelstudio_status():
                    ui.notification_show(_("Label Studio stopped successfully."), duration=5, type="message")
                    reload_trigger_ai.set(reload_trigger_ai.get() + 1)
                    return
                
                # Update progress
                progress_percent = int(((monotonic_time() - start_time) / max_wait_time) * 100)
                p.set(progress_percent)
                tm.sleep(0.5)
        
        # If we get here, the process didn't stop within the timeout period
        ui.notification_show(_("Label Studio may not have stopped completely. Please check the logs."), duration=5, type="warning")
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    @reactive.Effect
    @reactive.event(input.btn_labelstudio_start)
    def start_labelstudio():
        systemctl("start", "labelstudio")
        
        # Wait up to 300 seconds for the process to start
        # The first start may take a lot longer, so we set a higher timeout
        max_wait_time = 300  # seconds
        start_time = monotonic_time()
        
        with ui.Progress(min=0, max=100) as p:
            p.set(message=_("Starting Label Studio..."), detail=_("Please wait...") + " " + _("(The first start of Label Studio may take several minutes!)"))
            
            while monotonic_time() - start_time < max_wait_time:
                # Check if service is running and the web server is responding on port 8080
                if get_labelstudio_status():
                    if is_port_open(8080):
                        ui.notification_show(_("Label Studio started successfully."), duration=5, type="message")
                        reload_trigger_ai.set(reload_trigger_ai.get() + 1)
                        return
                
                # Update progress
                progress_percent = int(((monotonic_time() - start_time) / max_wait_time) * 100)
                p.set(progress_percent)
                tm.sleep(0.5)
        
        # If we get here, the process didn't start within the timeout period
        ui.notification_show(_("Label Studio may not have started completely. Please check the logs."), duration=5, type="warning")
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    def collapsible_section(section_id, title, intro, content):
        return ui.div(
            ui.div(
                ui.HTML(f"""
                    <button class="collapsible-header-btn btn" type="button" data-bs-toggle="collapse" data-bs-target="#{section_id}_body" aria-expanded="false" aria-controls="{section_id}_body">
                        <span class="collapsible-chevron">&#9654;</span>
                        <span class="collapsible-title">{title}</span>
                    </button>
                    <div class="collapsible-section-intro">{intro}</div>
                """),
            ),
            ui.div(
                ui.div(content),
                id=f"{section_id}_body",
                class_="collapse",  # collapsed by default
            ),
            class_="collapsible-section",
            style_="margin-bottom:1.5em;"
        )

    @output
    @render.ui
    @reactive.event(reload_trigger_config, ignore_none=True)
    def ui_configuration():
        # Helper for collapsible info blocks (local to this tab)
        def info_toggle(id_suffix: str, button_label: str, body_md: str):
            # body_md should already be translated before passing in
            return ui.HTML(f"""
            <div class="kh-info-toggle" id="toggle_{id_suffix}">
            <button type="button"
                    class="btn btn-link p-0 info-toggle-btn"
                    data-bs-toggle="collapse"
                    data-bs-target="#toggle_{id_suffix}_body"
                    aria-expanded="false"
                    style="text-decoration:none;">
                <span class="toggle-chevron" style="display:inline-block; transition:transform .2s;">&#9654;</span>
                {button_label}
            </button>
            <div id="toggle_{id_suffix}_body" class="collapse info-toggle-body" style="margin-top:6px;">
                <div class="kh-info-box">
                {ui.markdown(body_md)}
                </div>
            </div>
            </div>
            """)

        # Build a dictionary with TFLite model versions from the tflite folder
        tflite_models = {}
        try:
            for folder in os.listdir("./tflite"):
                if os.path.isdir(os.path.join("./tflite", folder)):
                    tflite_models[folder] = folder.replace('_', ' ').title()
        except Exception as e:
            logging.error(f"Failed to read TFLite model versions: {e}")

        # Create a dict from the yolo_models list, where the key is the "unique_id" and the value is the "full_display_name"
        yolo_models = {model["unique_id"]: model["full_display_name"] for model in YoloModel.get_model_list()}

        # Combine the tflite and yolo models into one list, so that the user can select between both models in the dropdown.
        # Prefix tflite model keys to avoid collision with yolo model unique_ids
        combined_models = {}
        for key, value in tflite_models.items():
            combined_models[f"tflite::{key}"] = value
        if tflite_models and yolo_models:
            # Insert a separator if both types exist
            combined_models["__separator__"] = "────────────"
        for unique_id, display_name in yolo_models.items():
            combined_models[f"yolo::{unique_id}"] = display_name

        hostname = get_hostname()

        lang = CONFIG.get('LANGUAGE', 'en')
        if lang not in ('en', 'de'):
            lang = 'en'
        def logic_svg(name: str) -> str:
            return f"logic/{name}_{lang}.svg"

        ui_config =  ui.div(
            ui.div(
                # --- General settings ---
                ui.br(),
                collapsible_section(
                    "general_settings",
                    _("General settings"),
                    _("Basic language, timezone, and display options for Kittyhack."),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(6, ui.input_select("txtLanguage", _("Language"), {"en":"English", "de":"Deutsch"}, selected=CONFIG['LANGUAGE'])),
                            ui.column(6, ui.input_text("txtConfigTimezone", _("Timezone"), CONFIG['TIMEZONE'])),
                            ui.column(6, ui.markdown("")),
                            ui.column(6, ui.HTML('<span class="help-block">' + _('See') +  ' <a href="https://en.wikipedia.org/wiki/List_of_tz_database_time_zones" target="_blank">Wikipedia</a> ' + _('for valid timezone strings') + '</span>')),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(6, ui.input_text("txtConfigDateformat", _("Date format"), CONFIG['DATE_FORMAT'])),
                            ui.column(
                                6,
                                ui.markdown(
                                    _("Valid placeholders: `yyyy`, `mm`, `dd`") + "\n\n" +
                                    _("Example:") + "\n" +
                                    "- `yyyy-mm-dd` " + _("for") + " `2025-02-28`\n" +
                                    "- `dd.mm.yyyy` " + _("for") + " `28.02.2025`"
                                ), style_="color: grey;"
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(12, ui.input_switch("btnPeriodicVersionCheck", _("Periodic version check"), CONFIG['PERIODIC_VERSION_CHECK'])),
                            ui.column(12, ui.markdown(_("Automatically check for new versions of Kittyhack.")), style_="color: grey;"),
                        ),
                        ui.hr(),
                        (
                            ui.row(
                                ui.column(12, ui.markdown(_("WLAN settings are not available in remote-mode.")), style_="color: grey;")
                            )
                            if is_remote_mode()
                            else ui.row(
                                ui.column(4, ui.input_slider("sldWlanTxPower", _("WLAN TX power (in dBm)"), min=0, max=20, value=CONFIG['WLAN_TX_POWER'], step=1)),
                                ui.column(
                                    8,
                                    ui.markdown(
                                        _("WARNING: You should keep the TX power as low as possible to avoid interference with the PIR Sensors! You should only increase this value, if you have problems with the WLAN connection.") + "\n\n" +
                                        "*(" + _("Default value: {}").format(DEFAULT_CONFIG['Settings']['wlan_tx_power']) + ")*"
                                    ), style_="color: grey;"
                                ),
                            )
                        ),
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),

                # --- Camera settings ---
                collapsible_section(
                    "camera_settings",
                    _("Camera settings"),
                    _("Camera specific settings for the internal and external cameras."),
                    ui.div(
                        ui.column(
                            12,
                            info_toggle(
                                "camera_intro",
                                _("Show IP camera usage tips"),
                                _(
                                    "You can use an **external IP camera** instead of the internal camera to achieve better viewing angles and better night vision.\n\n"
                                    "**Important notes:**\n"
                                    "- Both Kittyflap and the IP camera need a stable WLAN (Ethernet recommended for camera).\n"
                                    "- Use a **static IP** for the camera.\n"
                                    "- Ensure a compatible stream (RTSP / HTTP MJPEG / RTMP / UDP / TCP).\n"
                                    "- Recommended max resolution 1280x720 @ ≤15fps (higher reduces performance).\n"
                                    "- Configure resolution in camera settings or use alternate stream URLs.\n\n"
                                    "If you experience interruptions, check WLAN signal and network configuration."
                                )
                            ),
                        ),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_select(
                                    "camera_source",
                                    _("Camera source"),
                                    {
                                        "internal": _("Internal Camera"),
                                        "ip_camera": _("External IP Camera"),
                                    },
                                    selected=CONFIG['CAMERA_SOURCE'],
                                ),
                            ),
                            ui.column(
                                12,
                                ui.input_text(
                                    "ip_camera_url",
                                    _("External IP Camera URL"),
                                    value=CONFIG['IP_CAMERA_URL'],
                                    placeholder=_("e.g. rtsp://user:pass@192.168.1.100:554/stream"),
                                    width="100%",
                                ),
                                id="ip_camera_url_container",
                            ),
                        ),
                        ui.column(
                            12,
                            info_toggle(
                                "ip_camera_examples",
                                _("Show stream URL examples"),
                                _("**Examples of supported stream URLs:**")
                                + "\n"
                                + "- `rtsp://user:pass@192.168.1.100:554/stream1`  _(RTSP)_"
                                + "\n"
                                + "- `http://192.168.1.101:8080/video`  _(HTTP MJPEG)_"
                                + "\n"
                                + "- `rtmp://192.168.1.102/live/stream`  _(RTMP)_"
                                + "\n"
                                + "- `udp://@239.0.0.1:1234`  _(UDP multicast)_"
                                + "\n"
                                + "- `tcp://192.168.1.103:8554`  _(TCP stream)_"
                            ),
                            id="ip_camera_warning",
                        ),
                        ui.hr(),
                        ui.div(
                            ui.tags.button(
                                ui.tags.span(
                                    "\u25b6",
                                    class_="toggle-chevron",
                                    style_="display:inline-block; transition:transform .2s;",
                                ),
                                " ",
                                _("Advanced IP camera settings"),
                                type="button",
                                class_="btn btn-link p-0 info-toggle-btn",
                                style_="text-decoration:none;",
                                **{
                                    "data-bs-toggle": "collapse",
                                    "data-bs-target": "#ip_camera_pipeline_settings_body",
                                    "aria-expanded": "false",
                                    "aria-controls": "ip_camera_pipeline_settings_body",
                                },
                            ),
                            ui.div(
                                ui.div(
                                    ui.br(),
                                    ui.row(
                                        ui.column(
                                            12,
                                            ui.input_switch(
                                                "btnEnableIpCameraDecodeScalePipeline",
                                                _("Downscale IP camera stream"),
                                                CONFIG.get('ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE', False),
                                            ),
                                        ),
                                        ui.column(
                                            12,
                                            ui.markdown(
                                                _("If enabled, the IP camera stream is downscaled before frames reach Kittyhack.")
                                                + "  \n"
                                                + _("This can reduce CPU load and improve inference FPS for high-resolution streams.")
                                            ),
                                            style_="color: grey;",
                                        ),
                                    ),
                                    ui.row(
                                        ui.column(
                                            12,
                                            ui.input_select(
                                                "ip_camera_target_resolution",
                                                _("Target resolution for IP camera stream"),
                                                {
                                                    "480x320": "320p (480x320)",
                                                    "640x360": "360p (640x360)",
                                                    "640x480": "480p 4:3 (640x480)",
                                                    "854x480": "480p 16:9 (854x480)",
                                                    "960x540": "540p (960x540)",
                                                    "1024x576": "576p (1024x576)",
                                                    "1280x720": "720p (1280x720)",
                                                },
                                                selected=CONFIG.get('IP_CAMERA_TARGET_RESOLUTION', '640x360'),
                                                width="100%",
                                            ),
                                        ),
                                        ui.column(
                                            12,
                                            ui.markdown(
                                                _("Recommendation: choose `640x360` or `1280x720` for best performance/quality tradeoff.")
                                            ),
                                            style_="color: grey;",
                                        ),
                                    ),
                                    ui.row(
                                        ui.column(
                                            12,
                                            ui.input_select(
                                                "ip_camera_pipeline_fps_limit",
                                                _("IP camera FPS limit"),
                                                {
                                                    "5": "5 FPS",
                                                    "10": "10 FPS",
                                                    "15": "15 FPS",
                                                    "20": "20 FPS",
                                                    "25": "25 FPS",
                                                    "0": _("Unlimited"),
                                                },
                                                selected=str(CONFIG.get('IP_CAMERA_PIPELINE_FPS_LIMIT', 10)),
                                                width="100%",
                                            ),
                                        ),
                                        ui.column(
                                            12,
                                            ui.markdown(
                                                _("Default is `10 FPS`. Set to `Unlimited` to disable FPS limiting in the downscale pipeline (may cause higher CPU load).")
                                            ),
                                            style_="color: grey;",
                                        ),
                                    ),
                                ),
                                id="ip_camera_pipeline_settings_body",
                                class_="collapse info-toggle-body",
                                style_="margin-top:6px;",
                            ),
                            id="ip_camera_pipeline_settings",
                            class_="kh-info-toggle",
                            style_="margin-top: 0.75rem;",
                        ),
                        ui.br(),
                        full_screen=False,
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),

                # --- Door control settings ---
                collapsible_section(
                    "door_control_settings",
                    _("Door control settings"),
                    _("Detection thresholds, model selection and other settings for the door control."),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(12, ui.input_slider("sldMinThreshold", _("Minimum detection threshold"), min=0, max=80, width="90%", value=CONFIG['MIN_THRESHOLD'])),
                            ui.column(12, info_toggle(
                                "min_threshold_info",
                                _("Explain minimum detection threshold"),
                                _(
                                    "Used to decide if an outside motion event is logged. "
                                    "At least one picture must exceed this probability or the "
                                    "event is discarded.\n*(Default value: {})*"
                                ).format(DEFAULT_CONFIG['Settings']['min_threshold'])
                            )),
                        ),
                        ui.br(),
                        ui.row(
                            ui.column(12, ui.input_slider("sldMouseThreshold", _("Mouse detection threshold"), min=0, max=100, width="90%", value=CONFIG['MOUSE_THRESHOLD'])),
                            ui.column(12, info_toggle(
                                "mouse_threshold_info",
                                _("Explain mouse threshold"),
                                _(
                                    "Kittyhack decides if a picture contains a mouse based on this value. "
                                    "If probability exceeds it, flap stays closed.\n"
                                    "*(Default value: {})*\n\n"
                                    "**Note:** Minimum is coupled to 'Minimum detection threshold'."
                                ).format(DEFAULT_CONFIG['Settings']['mouse_threshold'])
                            )),
                        ),
                        ui.br(),
                        ui.row(
                            ui.column(12, ui.input_slider("sldCatThreshold", _("Cat detection threshold"), min=0, max=100, width="90%", value=CONFIG['CAT_THRESHOLD'])),
                            ui.column(12, info_toggle(
                                "cat_threshold_info",
                                _("Explain cat threshold"),
                                _("Kittyhack decides based on this value, if a picture contains your cat.") + "  \n" + 
                                _("If the detected probability of your cat exceeds this value in a picture, and the setting `Use camera for cat detection` is enabled, the flap will be opened.") + "  \n" +
                                "*(" + _("Default value: {}").format(DEFAULT_CONFIG['Settings']['cat_threshold']) + ")*" + "  \n\n" +
                                "**" + _("Note: The minimum of this value is always coupled to the configured value of 'Minimum detection threshold' above.") + "**"
                            )),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_numeric(
                                    "numMinSecondsToAnalyze",
                                    _("Seconds before unlock decision"),
                                    float(CONFIG.get('MIN_SECONDS_TO_ANALYZE', 1.5) or 1.5),
                                    min=0.1,
                                    step=0.1,
                                ),
                            ),
                            ui.column(12, info_toggle(
                                "min_seconds_info",
                                _("Explain unlock decision delay"),
                                _("Time in seconds after an outside motion trigger before Kittyhack may unlock.") + "  \n" +
                                _("Unlock is only possible if no prey was detected during this initial analysis window.") + "  \n\n" +
                                _("After unlocking: If a later picture reaches the mouse_threshold, the flap is locked again.") + "  \n\n" +
                                "*(" + _("Default value: {}").format(DEFAULT_CONFIG['Settings']['min_seconds_to_analyze']) + ")*"
                            )),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(12, ui.input_slider("sldLockAfterPreyDetect", _("Lock duration after prey detection (in s)"), min=30, max=1800, step=5, width="90%", value=CONFIG['LOCK_DURATION_AFTER_PREY_DETECTION'])),
                            ui.column(12, info_toggle(
                                "lock_after_prey_info",
                                _("Explain lock duration"),
                                _("The flap will remain closed for this time after a prey detection.")
                            )),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(12, ui.input_switch("btnDetectPrey", _("Detect prey"), CONFIG['MOUSE_CHECK_ENABLED'])),
                            ui.column(12, info_toggle(
                                "detect_prey_info",
                                _("Explain prey detection"),
                                _(
                                    "If the prey detection is enabled and the mouse detection threshold "
                                    "is exceeded in a picture, the flap will remain closed.\n\n"
                                    "**NOTE:** This is the global setting. It can also be configured "
                                    "per cat in the `MANAGE CATS` section."
                                )
                            )),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12, 
                                ui.input_select(
                                    "selectedModel",
                                    _("Version of the object detection model"),
                                    combined_models,
                                    selected=(
                                        f"tflite::{CONFIG['TFLITE_MODEL_VERSION']}"
                                        if f"tflite::{CONFIG['TFLITE_MODEL_VERSION']}" in combined_models
                                        else (
                                            f"yolo::{CONFIG['YOLO_MODEL']}"
                                            if f"yolo::{CONFIG['YOLO_MODEL']}" in combined_models
                                            else next(iter(combined_models), "")
                                        )
                                    ),
                                    width="90%",
                                )
                            ),
                            ui.column(12, info_toggle(
                                "model_versions_info",
                                _("Show model version explanation"),
                                "- **" + _("Original Kittyflap Model v1:") + "** " + _("Always tries to detect objects `Mouse` and `No Mouse`, even if there is no such object in the picture.") + "\n\n" +
                                "- **" + _("Original Kittyflap Model v2:") + "** " + _("Only tries to detect objects `Mouse` and `No Mouse` if there is a cat in the picture.") + "\n\n" +
                                "- **" + _("Custom Models:") + "** " + _("These are your own trained models, which you have created in the `AI TRAINING` section.") + "\n\n" +
                                "> " + _("If you change this setting, the Kittyflap must be restarted to apply the new model version.")
                            )),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(12, ui.input_switch("btnUseCameraForCatDetection", _("Use camera for cat detection"), CONFIG['USE_CAMERA_FOR_CAT_DETECTION'], width="90%")),
                            ui.column(12, info_toggle(
                                "camera_cat_detection_info",
                                _("Explain camera cat detection"),
                                _("If this setting is enabled, the camera will also be used for cat detection (in addition to the RFID reader).") + "  \n\n" +
                                _("You can configure the required threshold for the cat detection with the slider `Cat detection threshold`.") + " " +
                                _("If the detection is successful, the inside direction will be opened.") + "\n\n" +
                                _("**NOTE:** This feature requires a custom trained model for your cat(s). It does not work with the default kittyflap models.") + "\n\n" +
                                _("This feature depends heavily on the quality of your model and sufficient lighting conditions.") + " " +
                                _("If one or both are not good, the detection may either fail or other - similiar looking cats may be detected as your cat.")
                            )),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(12, ui.input_switch("btnUseCameraForMotionDetection", _("Use camera for motion detection"), CONFIG['USE_CAMERA_FOR_MOTION_DETECTION'], width="90%")),
                            ui.column(12, info_toggle(
                                "camera_motion_detection_info",
                                _("Explain camera motion detection"),
                                _("Disables the outside PIR sensor and uses the camera for motion detection instead.") + "  \n\n" +
                                _("**How it works:**") + "  \n" +
                                _("- In regular operation, Kittyhack waits for a trigger from the outside PIR sensor before starting camera analysis") + "  \n" + 
                                _("- With this feature enabled, the PIR sensor is disabled and the camera continuously analyzes images") + "  \n" + 
                                _("- When a cat is detected in the camera feed, it's treated as equivalent to a motion detection outside") + "  \n\n" +
                                _("You can configure the required threshold for the cat detection with the slider `Cat detection threshold`.") + "  \n\n" +
                                _("This may be very helpful in areas where environmental factors (moving trees, people passing by) permanently cause false PIR triggers.") + "  \n\n" +
                                _("**NOTE:** This feature requires a custom trained model for your cat(s). It does not work with the default kittyflap models.") + "\n\n" +
                                _("This feature depends heavily on the quality of your model and sufficient lighting conditions.") + " " +
                                _("If one or both are not good, you may experience false triggers or your cat may not be detected correctly.")
                            )),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(12, ui.input_select(
                                "txtAllowedToEnter",
                                _("Open inside direction for:"),
                                {
                                    AllowedToEnter.ALL.value: _("All cats (unlock on every detected motion)"), 
                                    AllowedToEnter.ALL_RFIDS.value: _("All cats with a RFID chip"), 
                                    AllowedToEnter.KNOWN.value: _("Only registered cats"), 
                                    AllowedToEnter.NONE.value: _("No cats"),
                                    AllowedToEnter.CONFIGURE_PER_CAT.value: _("Individual configuration per cat (Beta)"),
                                },
                                selected=str(CONFIG['ALLOWED_TO_ENTER'].value),
                                width="90%",
                            )),
                            ui.column(12, info_toggle(
                                "allowed_to_enter_info",
                                _("Explain entrance modes"),
                                _("- **All cats:** *Every* detected motion on the outside will unlock the flap.") + "  \n" +
                                _("- **All cats with a RFID chip:** Every successful RFID detection will unlock the flap.") + "  \n" +
                                _("- **Only registered cats:** Only the cats that are registered in the database will unlock the flap (either by RFID or by camera detection, if enabled).") + "  \n" +
                                _("- **Individual configuration per cat:** Configure per cat if it is allowed to enter (in the `MANAGE CATS` section).") + "  \n" +
                                _("- **No cats:** The inside direction will never be opened.")
                            )),
                        ),
                        ui.row(
                            ui.column(
                                12,
                                ui.div(
                                    # Precompute translations to avoid _() inside f-strings
                                    ui.HTML(('''
                                        <button id="btn_toggle_entry_logic"
                                                class="btn-default"
                                                style="margin-top:8px;"
                                                data-show-label="{show}"
                                                data-hide-label="{hide}">
                                            <span>{show}</span>
                                        </button>
                                        <div id="entry_logic_hint"
                                             style="display:none; font-size:0.75rem; margin-top:4px; color:#555;">
                                             {hint}
                                        </div>
                                    ''').format(
                                        show=_("Show decision logic"),
                                        hide=_("Hide decision logic"),
                                        hint=_("Only the flowchart for the currently selected mode is shown.")
                                    )),
                                    ui.HTML(('''
                                        <div id="entry_logic_expand" class="logic-section" style="display:none; margin-top:10px;">
                                          <div id="entry_logic_images">
                                            <div class="logic-img-wrapper" data-mode="all">
                                              <img src="{src_all}" alt="Entry logic (ALL)"/>
                                            </div>
                                            <div class="logic-img-wrapper" data-mode="all_rfids">
                                              <img src="{src_all_rfids}" alt="Entry logic (ALL_RFIDS)"/>
                                            </div>
                                            <div class="logic-img-wrapper" data-mode="known">
                                              <img src="{src_known}" alt="Entry logic (KNOWN)"/>
                                            </div>
                                            <div class="logic-img-wrapper" data-mode="none">
                                              <img src="{src_none}" alt="Entry logic (NONE)"/>
                                            </div>
                                            <div class="logic-img-wrapper" data-mode="configure_per_cat">
                                              <img src="{src_cfg}" alt="Entry logic (CONFIGURE_PER_CAT)"/>
                                            </div>
                                          </div>
                                        </div>
                                    ''').format(
                                        src_all=logic_svg('entry_all'),
                                        src_all_rfids=logic_svg('entry_all_rfids'),
                                        src_known=logic_svg('entry_known'),
                                        src_none=logic_svg('entry_none'),
                                        src_cfg=logic_svg('entry_configure_per_cat'),
                                    )),
                                    class_="d-flex flex-column align-items-center w-100",
                                )
                            )
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(12, ui.input_select(
                                "btnAllowedToExit",
                                _("Outside direction:"),
                                {
                                    'allow': _("Allow exit"),
                                    'deny': _("Do not allow exit"),
                                    'configure_per_cat': _("Individual configuration per cat (Beta)"),
                                },
                                selected=str(CONFIG['ALLOWED_TO_EXIT'].value),
                                width="90%",
                            )),
                            ui.column(12, info_toggle(
                                "allowed_to_exit_info",
                                _("Explain exit modes"),
                                _("- **Allow exit:** The outside direction is always possible. You can also configure time ranges below to restrict the exit times.") + "  \n" +
                                _("- **Do not allow exit:** The outside direction is always closed.") + "  \n" +
                                _("- **Individual configuration per cat:** Configure per cat if it is allowed to exit (in the `MANAGE CATS` section). The time ranges below are applied in addition.")+ "  \n  " +
                                _("**NOTE:** All your cats must be registered **with a RFID chip** to use this mode. Cats without RFID can not go outside in this mode!")
                            )),
                        ),
                        ui.row(
                            ui.column(
                                12,
                                ui.div(
                                    # Precompute translations to avoid _() inside f-strings
                                    ui.HTML(('''
                                        <button id="btn_toggle_exit_logic"
                                                class="btn-default"
                                                style="margin-top:8px;"
                                                data-show-label="{show}"
                                                data-hide-label="{hide}">
                                            <span>{show}</span>
                                        </button>
                                        <div id="exit_logic_hint"
                                             style="display:none; font-size:0.75rem; margin-top:4px; color:#555;">
                                             {hint}
                                        </div>
                                    ''').format(
                                        show=_("Show decision logic"),
                                        hide=_("Hide decision logic"),
                                        hint=_("Only the flowchart for the currently selected mode is shown.")
                                    )),
                                    ui.HTML(('''
                                        <div id="exit_logic_expand" class="logic-section" style="display:none; margin-top:10px;">
                                          <div id="exit_logic_images">
                                            <div class="logic-img-wrapper" data-mode="allow">
                                              <img src="{src_allow}" alt="Exit logic (ALLOW)"/>
                                            </div>
                                            <div class="logic-img-wrapper" data-mode="deny">
                                              <img src="{src_deny}" alt="Exit logic (DENY)"/>
                                            </div>
                                            <div class="logic-img-wrapper" data-mode="configure_per_cat">
                                              <img src="{src_cfg}" alt="Exit logic (CONFIGURE_PER_CAT)"/>
                                            </div>
                                          </div>
                                        </div>
                                    ''').format(
                                        src_allow=logic_svg('exit_allow'),
                                        src_deny=logic_svg('exit_deny'),
                                        src_cfg=logic_svg('exit_configure_per_cat'),
                                    )),
                                    class_="d-flex flex-column align-items-center w-100",
                                    style_="color: grey;"
                                ),
                            ),
                        ),
                        ui.br(),
                        ui.br(),
                        ui.div(
                            ui.row(
                                ui.column(
                                    12, 
                                    info_toggle(
                                        "exit_time_ranges",
                                        _("Show exit time range rules"),
                                        _("You can specify up to 3 time ranges, in which your cats are allowed to exit.") + "  \n\n" +
                                        _("Rules:") + "  \n" +
                                        "- " + _("The time ranges are global and apply to all cats.") + "  \n" +
                                        "- " + _("If no range is enabled, cats may exit at any time (subject to other settings).") + "  \n" +
                                        "- " + _("If any range is enabled, cats may exit only during the configured time windows.") + "  \n" +
                                        "- " + _("Outside these windows, no cat may exit, even if the per‑cat setting in 'Manage Cats' allows exit.") + "  \n" +
                                        _("See the decision logic flowchart above for details.") + "  \n\n" +
                                        _("Enter times in 24h format HH:MM (e.g., 13:00).") + "  \n" +
                                        _("Example: If ranges are 10:00–18:00 and it is 20:00, no cat may exit, even if that cat is allowed per‑cat in 'Manage Cats'.")
                                    )
                                )
                            ),
                            ui.br(),
                            ui.row(
                                ui.column(12, ui.input_switch("btnAllowedToExitRange1", "\n" + _("Time range 1"), CONFIG['ALLOWED_TO_EXIT_RANGE1'])),
                                ui.column(4, ui.input_text("txtAllowedToExitRange1From", label=_("From"), placeholder="00:00", value=CONFIG['ALLOWED_TO_EXIT_RANGE1_FROM'])),
                                ui.column(4, ui.input_text("txtAllowedToExitRange1To", label=_("To"), placeholder="00:00", value=CONFIG['ALLOWED_TO_EXIT_RANGE1_TO']))
                            ),
                            ui.br(),
                            ui.row(
                                ui.column(12, ui.input_switch("btnAllowedToExitRange2", _("Time range 2"), CONFIG['ALLOWED_TO_EXIT_RANGE2'])),
                                ui.column(4, ui.input_text("txtAllowedToExitRange2From", label=_("From"), placeholder="00:00", value=CONFIG['ALLOWED_TO_EXIT_RANGE2_FROM'])),
                                ui.column(4, ui.input_text("txtAllowedToExitRange2To", label=_("To"), placeholder="00:00", value=CONFIG['ALLOWED_TO_EXIT_RANGE2_TO']))
                            ),
                            ui.br(),
                            ui.row(
                                ui.column(12, ui.input_switch("btnAllowedToExitRange3", _("Time range 3"), CONFIG['ALLOWED_TO_EXIT_RANGE3'])),
                                ui.column(4, ui.input_text("txtAllowedToExitRange3From", label=_("From"), placeholder="00:00", value=CONFIG['ALLOWED_TO_EXIT_RANGE3_FROM'])),
                                ui.column(4, ui.input_text("txtAllowedToExitRange3To", label=_("To"), placeholder="00:00", value=CONFIG['ALLOWED_TO_EXIT_RANGE3_TO']))
                            ),
                            id_="allowed_to_exit_ranges",
                        ),
                        ui.hr(),
                        # TODO: Outside PIR shall not yet be configurable. Need to redesign the camera control, otherwise we will have no cat pictures at high PIR thresholds.
                        #ui.column(12, ui.input_slider("sldPirOutsideThreshold", _("Sensitivity of the motion sensor on the outside"), min=0.1, max=6, step=0.1, value=CONFIG['PIR_OUTSIDE_THRESHOLD'])),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_slider(
                                    "sldPirInsideThreshold",
                                    _("Reaction speed (in s) of the motion sensor on the inside"),
                                    min=0.1,
                                    max=6,
                                    step=0.1,
                                    value=CONFIG['PIR_INSIDE_THRESHOLD'],
                                    width="90%",
                                )
                            ),
                            ui.column(
                                12,
                                info_toggle(
                                    "pir_inside_threshold_info",
                                    _("Explain inside PIR reaction speed"),
                                    _("A low value means a fast reaction, but also a higher probability of false alarms. "
                                      "A high value means a slow reaction, but also a lower probability of false alarms.") + "  \n" +
                                    _("The default setting should be a good value for most cases.") + "  \n" +
                                    "*(" + _("Default value: {}").format(DEFAULT_CONFIG['Settings']['pir_inside_threshold']) + ")*"
                                )
                            )
                        ),
                        full_screen=False,
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),

                # --- Live view settings ---
                collapsible_section(
                    "live_view_settings",
                    _("Live view settings"),
                    _("Update interval for the live camera view in the WebUI."),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(
                                4,
                                ui.input_select(
                                    "numLiveViewUpdateInterval",
                                    _("Live-View update interval:"),
                                    {
                                        _("Refresh the live view every..."):
                                        {
                                            0.1: "100ms", 0.2: "200ms", 0.5: "500ms", 1.0: "1s", 2.0: "2s", 3.0: "3s", 5.0: "5s", 10.0: "10s"
                                        },
                                    },
                                    selected=CONFIG['LIVE_VIEW_REFRESH_INTERVAL'],
                                )
                            ),
                            ui.column(
                                8,
                                ui.markdown(
                                    _("NOTE: A high refresh rate could slow down the performance, especially if several users are connected at the same time. Values below 1s require a fast and stable WLAN connection.") +
                                    "  \n" +
                                    _("This setting affects only the view in the WebUI and has no impact on the detection process.")
                                ), style_="color: grey;"
                            ),
                        ),
                        ui.br(),
                        full_screen=False,
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),

                # --- Pictures view settings ---
                collapsible_section(
                    "pictures_view_settings",
                    _("Pictures view settings"),
                    _("Configuration of the pictures view in the WebUI, maximum number of pictures in the database and limits of pictures per event."),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(4, ui.input_numeric("numMaxPhotosCount", _("Maximum number of photos to retain in the database"), CONFIG['MAX_PHOTOS_COUNT'], min=100)),
                            ui.column(
                                8,
                                ui.markdown(
                                    _("The oldest pictures will be deleted if the number of pictures exceeds this value.") + "  \n" +
                                    _("The maximum number of pictures depends on the type of the Raspberry Pi, since some kittyflaps are equipped with 16GB and some with 32GB.") + "  \n" +
                                    _("As a rule of thumb, you can calculate with 200MB per 1000 pictures. You can check the free disk space in the `INFO` section.") + "  \n" +
                                    "*(" + _("Default value: {}").format(DEFAULT_CONFIG['Settings']['max_photos_count']) + ")*"
                                ), style_="color: grey;"
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(4, ui.input_numeric("numMaxPicturesPerEventWithRfid", _("Maximal pictures per event with RFID"), CONFIG['MAX_PICTURES_PER_EVENT_WITH_RFID'], min=0)),
                            ui.column(
                                8,
                                ui.markdown(
                                    _("Maximal number of pictures that will be stored to the database for a motion event, if a cat with a RFID chip is detected.") + "  \n" +
                                    _("NOTE: The internal prey detection will still be active for all pictures of this event. This number just limits the number of pictures that will be stored to the database.") + "  \n" +
                                    _("If you set this to 0, no event will be logged to the database. Too many pictures can slow down the performance drastically.")
                                ), style_="color: grey;"
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(4, ui.input_numeric("numMaxPicturesPerEventWithoutRfid", _("Maximal pictures per event without RFID"), CONFIG['MAX_PICTURES_PER_EVENT_WITHOUT_RFID'], min=0)),
                            ui.column(
                                8,
                                ui.markdown(
                                    _("Maximal number of pictures that will be stored to the database for a motion event, if a motion event without a detected RFID occurs.") + "  \n" +
                                    _("NOTE: The internal prey detection will still be active for all pictures of this event. This number just limits the number of pictures that will be stored to the database.") + " \n" +
                                    _("If you set this to 0, no event will be logged to the database. Too many pictures can slow down the performance drastically.")
                                ), style_="color: grey;"
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(4, ui.input_numeric("numElementsPerPage", _("Maximum pictures per page"), CONFIG['ELEMENTS_PER_PAGE'], min=1)),
                            ui.column(
                                8,
                                ui.markdown(
                                    _("This setting applies only to the `PICTURES` section in the ungrouped view mode.") + "\n\n" +
                                    _("NOTE: Too many pictures per page could slow down the performance drastically!")
                                ), style_="color: grey;"
                            ),
                        ),
                        ui.br(),
                        full_screen=False,
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),

                # --- Home Assistant / MQTT settings ---
                collapsible_section(
                    "home_assistant_settings",
                    _("Home Assistant configuration"),
                    _("Configure MQTT integration for Home Assistant."),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_switch(
                                    "btnMqttEnabled",
                                    _("Enable MQTT"),
                                    CONFIG.get('MQTT_ENABLED', False)
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                8,
                                ui.input_text(
                                    "txtMqttBrokerAddress",
                                    _("MQTT Broker Address"),
                                    value=CONFIG.get('MQTT_BROKER_ADDRESS', ""),
                                    placeholder=_("e.g. 192.168.1.10"),
                                    width="100%",
                                ),
                            ),
                            ui.column(
                                4,
                                ui.input_numeric(
                                    "numMqttBrokerPort",
                                    _("MQTT Broker Port"),
                                    value=CONFIG.get('MQTT_BROKER_PORT', 1883),
                                    min=1,
                                    max=65535,
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                6,
                                ui.input_text(
                                    "txtMqttUsername",
                                    _("MQTT Username"),
                                    value=CONFIG.get('MQTT_USERNAME', ""),
                                    placeholder=_("MQTT username"),
                                    width="100%",
                                ),
                            ),
                            ui.column(
                                6,
                                ui.input_password(
                                    "txtMqttPassword",
                                    _("MQTT Password"),
                                    value=CONFIG.get('MQTT_PASSWORD', ""),
                                    placeholder=_("MQTT password"),
                                    width="100%",
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_select(
                                    id="mqtt_image_publish_interval",
                                    label=_("Camera update interval"),
                                    choices={2: "2s", 3: "3s", 5: "5s", 10: "10s", 20: "20s", 30: "30s", 60: "60s"},
                                    selected=CONFIG['MQTT_IMAGE_PUBLISH_INTERVAL']
                                ),
                                ui.help_text(_("The interval in seconds between publishing camera images to the MQTT broker.")),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.h5(_("Home Assistant Card Example")),
                                ui.markdown(_("Copy this configuration to create a dashboard card in Home Assistant:")),
                                ui.markdown(
                                    "```yaml\n" +
                                    "# " + _("Home Assistant Dashboard Card") + "\n" +
                                    "type: vertical-stack\n" +
                                    "title: " + _("KittyHack Cat Flap") + "\n" +
                                    "cards:\n" +
                                    "  - show_state: false\n" +
                                    "    show_name: false\n" +
                                    "    camera_view: live\n" +
                                    "    fit_mode: cover\n" +
                                    "    type: picture-entity\n" +
                                    "    entity: camera.{}_camera\n".format(CONFIG['MQTT_DEVICE_ID']) +
                                    "  - type: tile\n" +
                                    "    entity: sensor.{}_events\n".format(CONFIG['MQTT_DEVICE_ID']) +
                                    "    features_position: bottom\n" +
                                    "    vertical: false\n" +
                                    "    name: " + _("Last Event") + "\n" +
                                    "    show_entity_picture: false\n" +
                                    "    hide_state: false\n" +
                                    "    state_content:\n" +
                                    "      - event\n" +
                                    "      - detected_cat\n" +
                                    "  - type: entities\n" +
                                    "    show_header_toggle: false\n" +
                                    "    entities:\n" +
                                    "      - entity: lock.{}_inside_lock\n".format(CONFIG['MQTT_DEVICE_ID']) +
                                    "        name: " + _("Inside") + "\n" +
                                    "        icon: \"\"\n" +
                                    "        secondary_info: none\n" +
                                    "      - entity: binary_sensor.{}_outside_lock\n".format(CONFIG['MQTT_DEVICE_ID']) +
                                    "        name: " + _("Outside") + "\n" +
                                    "        icon: \"\"\n" +
                                    "    state_color: false\n" +
                                    "    title: " + _("Magnetic Locks") + "\n" +
                                    "  - type: entities\n" +
                                    "    title: " + _("Configuration") + "\n" +
                                    "    show_header_toggle: false\n" +
                                    "    entities:\n" +
                                    "      - entity: select.{}_allow_enter\n".format(CONFIG['MQTT_DEVICE_ID']) +
                                    "        name: " + _("Open entrance for...") + "\n" +
                                    "        icon: \"\"\n" +
                                    "        secondary_info: none\n" +
                                    "      - entity: select.{}_allow_exit\n".format(CONFIG['MQTT_DEVICE_ID']) +
                                    "        name: " + _("Allow cats to exit") + "\n" +
                                    "        icon: \"\"\n" +
                                    "        secondary_info: none\n" +
                                    "    state_color: false\n" +
                                    "  - type: entities\n" +
                                    "    entities:\n" +
                                    "      - entity: binary_sensor.{}_motion_outside\n".format(CONFIG['MQTT_DEVICE_ID']) +
                                    "        name: " + _("Motion outside") + "\n" +
                                    "        secondary_info: last-changed\n" +
                                    "      - entity: binary_sensor.{}_motion_inside\n".format(CONFIG['MQTT_DEVICE_ID']) +
                                    "        name: " + _("Motion inside") + "\n" +
                                    "        secondary_info: last-changed\n" +
                                    "      - entity: binary_sensor.{}_prey_detected\n".format(CONFIG['MQTT_DEVICE_ID']) +
                                    "        name: " + _("Prey detected") + "\n" +
                                    "        secondary_info: last-changed\n" +
                                    "    title: " + _("Status") + "\n" +
                                    "    state_color: false\n" +
                                    "    show_header_toggle: false\n" +
                                    "```"
                                ),
                            ),
                        ),
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),

                # --- Advanced settings ---
                collapsible_section(
                    "advanced_settings",
                    _("Advanced settings"),
                    _("Advanced configuration options for hostname, logging, and performance."),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(12, ui.input_text("txtHostname", label=_("Hostname"), placeholder="", value=hostname, width="100%")),
                            ui.column(
                                12,
                                ui.markdown(
                                    _("The hostname of the Kittyflap. You can change it to any unique name in your network for an easier access (mDNS / Avahi must be enabled in your router).") + "  \n" +
                                    _("Allowed characters: `a-z`, `A-Z`, `0-9` and `-`.") + "  \n" +
                                    "> " + _("NOTE: This setting requires a restart of the kittyflap to take effect.") + "\n\n" +
                                    _("You can access the Kittyflap via the hostname in your browser:")
                                ), 
                                ui.output_text_verbatim("hostname_preview"),
                                style_="color: grey;"
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(4, ui.input_select("txtLoglevel", "Loglevel", {"DEBUG": "DEBUG", "INFO": "INFO", "WARN": "WARN", "ERROR": "ERROR", "CRITICAL": "CRITICAL"}, selected=CONFIG['LOGLEVEL'])),
                            ui.column(8, ui.markdown(_("`INFO` is the default log level and should be used in normal operation. `DEBUG` should only be used if it is really necessary!")), style_="color: grey;"),
                        ),
                        (
                            ui.TagList(
                                ui.hr(),
                                ui.row(
                                    ui.column(12, ui.input_switch("btnUseAllCoresForImageProcessing", _("Use all CPU cores for image processing"), CONFIG['USE_ALL_CORES_FOR_IMAGE_PROCESSING'])),
                                    ui.column(
                                        12,
                                        ui.markdown(
                                            _("If this is enabled, all CPU cores will be used for image processing. This results in a faster analysis of the pictures, and therefore a maybe a bit faster prey detection.")
                                        ), style="color: grey;",
                                    ),
                                    ui.column(
                                        12,
                                        ui.markdown(
                                            f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                                            + _("**WARNING**: It is NOT recommended to enable this feature! Several users have reported that this option causes reboots or system freezes.") +
                                            _("If you encounter the same issue, it's strongly recommended to disable this setting.")
                                        ), style_="color: #e74a3b; padding: 10px; border: 1px solid #e74a3b; border-radius: 5px; margin: 20px; width: 90%;"
                                    ),
                                    ui.column( 12, ui.markdown("> " + _("NOTE: This setting requires a restart of the kittyflap to take effect.")), style_="color: grey;"),
                                ),
                                ui.hr(),
                            )
                            if not is_remote_mode()
                            else ui.hr()
                        ),
                        ui.row(
                            ui.column(
                                12,
                                (lambda _inp: (_inp if is_remote_mode() else _disable_numeric_input(_inp)))(
                                    ui.input_numeric(
                                        "numRemoteInferenceMaxFps",
                                        _("Remote inference FPS limit"),
                                        float(CONFIG.get('REMOTE_INFERENCE_MAX_FPS', 10.0) or 10.0),
                                        min=1,
                                        max=60,
                                        step=1,
                                        width="100%",
                                    )
                                ),
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    (_("Limits the model inference loop to reduce CPU load in remote-mode.")
                                     + "\n\n> "
                                     + (_("This setting is only configurable in remote-mode.") if not is_remote_mode() else _("Default: 10 FPS")))
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        ui.hr(),
                        (
                            ui.row(
                                ui.column(
                                    12,
                                    ui.input_numeric(
                                        "numRemoteWaitAfterRebootTimeout",
                                        _("Wait for remote after reboot (seconds)"),
                                        float(CONFIG.get("REMOTE_WAIT_AFTER_REBOOT_TIMEOUT", 30.0) or 30.0),
                                        min=5,
                                        max=600,
                                        step=1,
                                        width="100%",
                                    ),
                                ),
                                ui.column(
                                    12,
                                    ui.markdown(
                                        _("If the Kittyflap has been controlled remotely before, it will wait this long for a remote-control takeover after reboot before starting Kittyhack locally.")
                                    ),
                                    style_="color: grey;",
                                ),
                            )
                            if not is_remote_mode()
                            else ui.HTML(""),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_switch(
                                    "btnRestartIpCameraStreamOnFailure",
                                    _("IP camera watchdog"),
                                    CONFIG['RESTART_IP_CAMERA_STREAM_ON_FAILURE']
                                ),
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    _("If enabled, the stream of an external camera will be automatically restarted if corrupted frames are detected.")
                                ),
                                style_="color: grey;"
                            ),
                        ),
                        ui.hr(),
                        (
                            ui.row(
                                ui.column(12, ui.markdown(_("WLAN watchdog is not available in remote-mode.")), style_="color: grey;")
                            )
                            if is_remote_mode()
                            else ui.row(
                                ui.column(
                                    12,
                                    ui.input_switch(
                                        "btnWlanWatchdogEnabled",
                                        _("WLAN watchdog"),
                                        CONFIG['WLAN_WATCHDOG_ENABLED']
                                    ),
                                ),
                                ui.column(
                                    12,
                                    ui.markdown(
                                        _("If enabled, the WLAN connection will be monitored and automatically reconnected on failure. If this also fails, the Kittyflap will automatically restart."),
                                    ),
                                    style_="color: grey;"
                                ),
                            )
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_switch(
                                    "btnDisableRfidReader",
                                    _("Disable RFID reader"),
                                    CONFIG['DISABLE_RFID_READER']
                                ),
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    _("If this option is enabled, the RFID reader will NOT be powered when motion is detected. "
                                      "Use only for troubleshooting hardware defects or undervoltage reboots. "
                                      "You must rely on 'Open inside direction for: All cats' or camera-based cat detection instead.") +
                                    "\n\n> " + _("Default: Disabled")
                                ),
                                style_="color: grey;"
                            ),
                        ),
                        ui.br(),
                        full_screen=False,
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),

                ui.br(),
                ui.br(),
                ui.br(),
                ui.br(),
                ui.br(),
                ui.panel_absolute(
                    ui.panel_well(
                        ui.input_action_button(id="bSaveKittyhackConfig", label=_("Save all changes"), icon=icon_svg("floppy-disk")),
                        class_="sticky-action-well",
                    ),
                    draggable=False, width="100%", left="0px", right="0px", bottom="0px", fixed=True,
                ),

                id="config_tab_container"
            ),
        )
        return ui_config
    
    @render.text
    def hostname_preview():
        return "http://" + input.txtHostname() + ".local"
    
    @reactive.effect
    def update_mouse_threshold_limit():
        # You can update the value, min, max, and step.
        ui.update_slider(
            "sldMouseThreshold",
            value=max(input.sldMouseThreshold(), input.sldMinThreshold()),
            min=input.sldMinThreshold(),
        )
        ui.update_slider(
            "sldCatThreshold",
            value=max(input.sldCatThreshold(), input.sldMinThreshold()),
            min=input.sldMinThreshold(),
        )

    @reactive.Effect
    @reactive.event(input.bSaveKittyhackConfig)
    def on_save_kittyhack_config():
        global _
        global model_handler

        camera_settings_changed = (
            CONFIG.get('CAMERA_SOURCE') != input.camera_source() or
            CONFIG.get('IP_CAMERA_URL') != input.ip_camera_url() or
            CONFIG.get('ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE', False) != input.btnEnableIpCameraDecodeScalePipeline() or
            CONFIG.get('IP_CAMERA_TARGET_RESOLUTION', '640x360') != input.ip_camera_target_resolution() or
            int(CONFIG.get('IP_CAMERA_PIPELINE_FPS_LIMIT', 10) or 10) != int(input.ip_camera_pipeline_fps_limit())
        )

        # Check the time ranges for allowed to exit
        def validate_time_format(field_id):
            # Get the value from the input field
            value = input[field_id]()
            # Check if value exists and doesn't match the time format
            if value and not re.match(r"^\d{2}:\d{2}$", value):
                ui.notification_show(
                    _("Allowed to exit time ranges: ") +
                    _("Invalid time format. Please use HH:MM format.") + "\n" + 
                    _("Changes were not saved."), 
                    duration=10, type="error"
                )
                return False
            # Check if the time is in the valid range
            try:
                hour, minute = map(int, value.split(':'))
                if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                    ui.notification_show(
                        _("Allowed to exit time ranges: ") +
                        _("Time must be between 00:00 and 23:59.") + "\n" + 
                        _("Changes were not saved."), 
                        duration=10, type="error"
                    )
                    return False
            except ValueError:
                ui.notification_show(
                    _("Allowed to exit time ranges: ") +
                    _("Invalid time value.") + "\n" +
                    _("Changes were not saved."),
                    duration=10, type="error"
                )
                return False
            return True
        
        # Validate all time input fields
        time_fields = [
            "txtAllowedToExitRange1From", "txtAllowedToExitRange1To",
            "txtAllowedToExitRange2From", "txtAllowedToExitRange2To",
            "txtAllowedToExitRange3From", "txtAllowedToExitRange3To"
        ]
        
        for field_id in time_fields:
            valid = validate_time_format(field_id)
            if not valid:
                return
            
        # Check for a changed hostname
        hostname_changed = input.txtHostname() != get_hostname()
        if hostname_changed:
            # Check if the hostname is valid
            if not re.match(r"^[a-zA-Z0-9-]+$", input.txtHostname()):
                ui.notification_show(
                    _("Hostname: ") +
                    _("Invalid hostname. Only letters, numbers and hyphens are allowed.") + "\n" + 
                    _("Changes were not saved."), duration=10, type="error"
                )
                return
            # Hostname is valid, set it
            set_hostname(input.txtHostname())
        
        mqtt_settings_changed = (
            CONFIG['MQTT_ENABLED'] != input.btnMqttEnabled() or
            CONFIG['MQTT_BROKER_ADDRESS'] != input.txtMqttBrokerAddress() or
            CONFIG['MQTT_BROKER_PORT'] != int(input.numMqttBrokerPort()) or
            CONFIG['MQTT_USERNAME'] != input.txtMqttUsername() or
            CONFIG['MQTT_PASSWORD'] != input.txtMqttPassword() or
            CONFIG['MQTT_IMAGE_PUBLISH_INTERVAL'] != float(input.mqtt_image_publish_interval())
        )

        if input.camera_source() == "ip_camera":
            # Accept RTSP, HTTP(S), RTMP, UDP, TCP, and file URLs
            if not re.match(r"^(rtsp|http|https|rtmp|udp|tcp|file)://", input.ip_camera_url(), re.IGNORECASE):
                ui.notification_show(
                    _("IP Camera URL: ") +
                    _("Invalid stream URL format. Please use a valid URL starting with rtsp://, http://, https://, rtmp://, udp://, tcp://, or file://") + "\n" +
                    _("Changes were not saved."), duration=10, type="error"
                )
                return

        if input.btnEnableIpCameraDecodeScalePipeline():
            ffmpeg_ok = ensure_ffmpeg_installed()
            if not ffmpeg_ok:
                ui.notification_show(
                    _("FFmpeg is required for the decode+scale pipeline but could not be installed automatically.") + "\n" +
                    _("Please install `ffmpeg` manually and try again. Changes were not saved."),
                    duration=12,
                    type="error",
                )
                return

        # override the variable with the data from the configuration page
        language_changed = CONFIG['LANGUAGE'] != input.txtLanguage()
        rfid_state_changed = CONFIG['DISABLE_RFID_READER'] != input.btnDisableRfidReader()
        if (input.selectedModel().startswith("tflite::") and
            input.selectedModel() != f"tflite::{CONFIG['TFLITE_MODEL_VERSION']}"):
            selected_model_changed = True
        elif (input.selectedModel().startswith("yolo::") and
              input.selectedModel() != f"yolo::{CONFIG['YOLO_MODEL']}"):
            selected_model_changed = True
        else:
            selected_model_changed = False
        if is_remote_mode():
            img_processing_cores_changed = False
        else:
            img_processing_cores_changed = CONFIG['USE_ALL_CORES_FOR_IMAGE_PROCESSING'] != input.btnUseAllCoresForImageProcessing()

        # Update the configuration dictionary with the new values
        CONFIG['LANGUAGE'] = input.txtLanguage()
        CONFIG['TIMEZONE'] = input.txtConfigTimezone()
        CONFIG['DATE_FORMAT'] = input.txtConfigDateformat()
        CONFIG['MOUSE_THRESHOLD'] = float(input.sldMouseThreshold())
        CONFIG['MIN_THRESHOLD'] = float(input.sldMinThreshold())
        try:
            CONFIG['MIN_SECONDS_TO_ANALYZE'] = max(0.1, round(float(input.numMinSecondsToAnalyze()), 1))
        except Exception:
            CONFIG['MIN_SECONDS_TO_ANALYZE'] = float(DEFAULT_CONFIG['Settings']['min_seconds_to_analyze'])
        CONFIG['ELEMENTS_PER_PAGE'] = int(input.numElementsPerPage())
        CONFIG['MAX_PHOTOS_COUNT'] = int(input.numMaxPhotosCount())
        CONFIG['LOGLEVEL'] = input.txtLoglevel()
        CONFIG['MOUSE_CHECK_ENABLED'] = input.btnDetectPrey()

        # Always update the model configuration and ensure, that only one of the two is set
        # Check if the selected model is a TFLite model or a YOLO model
        if (input.selectedModel().startswith("yolo::")):
            CONFIG['YOLO_MODEL'] = input.selectedModel().replace("yolo::", "")
            CONFIG['TFLITE_MODEL_VERSION'] = ""
        elif (input.selectedModel().startswith("tflite::")):
            CONFIG['TFLITE_MODEL_VERSION'] = input.selectedModel().replace("tflite::", "")
            CONFIG['YOLO_MODEL'] = ""
        
        CONFIG['USE_CAMERA_FOR_CAT_DETECTION'] = input.btnUseCameraForCatDetection()
        CONFIG['CAT_THRESHOLD'] = float(input.sldCatThreshold())
        CONFIG['USE_CAMERA_FOR_MOTION_DETECTION'] = input.btnUseCameraForMotionDetection()
        CONFIG['ALLOWED_TO_ENTER'] = AllowedToEnter(input.txtAllowedToEnter())
        CONFIG['LIVE_VIEW_REFRESH_INTERVAL'] = float(input.numLiveViewUpdateInterval())
        from src.baseconfig import AllowedToExit as ATE
        CONFIG['ALLOWED_TO_EXIT'] = ATE(input.btnAllowedToExit())
        CONFIG['PERIODIC_VERSION_CHECK'] = input.btnPeriodicVersionCheck()
        # TODO: Outside PIR shall not yet be configurable. Need to redesign the camera control, otherwise we will have no cat pictures at high PIR thresholds.
        #CONFIG['PIR_OUTSIDE_THRESHOLD'] = 10-int(input.sldPirOutsideThreshold())
        CONFIG['PIR_INSIDE_THRESHOLD'] = float(input.sldPirInsideThreshold())
        if not is_remote_mode():
            CONFIG['WLAN_TX_POWER'] = int(input.sldWlanTxPower())
        CONFIG['LOCK_DURATION_AFTER_PREY_DETECTION'] = int(input.sldLockAfterPreyDetect())
        CONFIG['MAX_PICTURES_PER_EVENT_WITH_RFID'] = int(input.numMaxPicturesPerEventWithRfid())
        CONFIG['MAX_PICTURES_PER_EVENT_WITHOUT_RFID'] = int(input.numMaxPicturesPerEventWithoutRfid())
        if is_remote_mode():
            CONFIG['USE_ALL_CORES_FOR_IMAGE_PROCESSING'] = True
        else:
            CONFIG['USE_ALL_CORES_FOR_IMAGE_PROCESSING'] = input.btnUseAllCoresForImageProcessing()
        CONFIG['ALLOWED_TO_EXIT_RANGE1'] = input.btnAllowedToExitRange1()
        CONFIG['ALLOWED_TO_EXIT_RANGE1_FROM'] = input.txtAllowedToExitRange1From()
        CONFIG['ALLOWED_TO_EXIT_RANGE1_TO'] = input.txtAllowedToExitRange1To()
        CONFIG['ALLOWED_TO_EXIT_RANGE2'] = input.btnAllowedToExitRange2()
        CONFIG['ALLOWED_TO_EXIT_RANGE2_FROM'] = input.txtAllowedToExitRange2From()
        CONFIG['ALLOWED_TO_EXIT_RANGE2_TO'] = input.txtAllowedToExitRange2To()
        CONFIG['ALLOWED_TO_EXIT_RANGE3'] = input.btnAllowedToExitRange3()
        CONFIG['ALLOWED_TO_EXIT_RANGE3_FROM'] = input.txtAllowedToExitRange3From()
        CONFIG['ALLOWED_TO_EXIT_RANGE3_TO'] = input.txtAllowedToExitRange3To()
        CONFIG['CAMERA_SOURCE'] = input.camera_source()
        CONFIG['IP_CAMERA_URL'] = input.ip_camera_url()
        CONFIG['ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE'] = input.btnEnableIpCameraDecodeScalePipeline()
        CONFIG['IP_CAMERA_TARGET_RESOLUTION'] = input.ip_camera_target_resolution()
        try:
            CONFIG['IP_CAMERA_PIPELINE_FPS_LIMIT'] = int(input.ip_camera_pipeline_fps_limit())
        except Exception:
            CONFIG['IP_CAMERA_PIPELINE_FPS_LIMIT'] = 10

        if camera_settings_changed:
            # Ensure live view reacts immediately (do not keep showing an old frame).
            try:
                live_view_aspect.set((4, 3))
            except Exception:
                pass

            if getattr(live_view_image, "_is_running", False):
                # Defer forced refresh until the current render is done to avoid
                # overlapping recalculations (client progress-state errors).
                live_view_image._refresh_after_run = True
            else:
                live_view_refresh_nonce.set(live_view_refresh_nonce.get() + 1)
        CONFIG['MQTT_ENABLED'] = input.btnMqttEnabled()
        CONFIG['MQTT_BROKER_ADDRESS'] = input.txtMqttBrokerAddress()
        CONFIG['MQTT_BROKER_PORT'] = int(input.numMqttBrokerPort())
        CONFIG['MQTT_USERNAME'] = input.txtMqttUsername()
        CONFIG['MQTT_PASSWORD'] = input.txtMqttPassword()
        CONFIG['MQTT_IMAGE_PUBLISH_INTERVAL'] = float(input.mqtt_image_publish_interval())
        CONFIG['RESTART_IP_CAMERA_STREAM_ON_FAILURE'] = input.btnRestartIpCameraStreamOnFailure()
        if not is_remote_mode():
            CONFIG['WLAN_WATCHDOG_ENABLED'] = input.btnWlanWatchdogEnabled()
        else:
            CONFIG['WLAN_WATCHDOG_ENABLED'] = False
        CONFIG['DISABLE_RFID_READER'] = input.btnDisableRfidReader()

        if not is_remote_mode():
            try:
                CONFIG['REMOTE_WAIT_AFTER_REBOOT_TIMEOUT'] = float(input.numRemoteWaitAfterRebootTimeout())
            except Exception:
                CONFIG['REMOTE_WAIT_AFTER_REBOOT_TIMEOUT'] = float(
                    CONFIG.get('REMOTE_WAIT_AFTER_REBOOT_TIMEOUT', DEFAULT_CONFIG['Settings']['remote_wait_after_reboot_timeout'])
                    or DEFAULT_CONFIG['Settings']['remote_wait_after_reboot_timeout']
                )
            # Keep it within a sane operational range.
            CONFIG['REMOTE_WAIT_AFTER_REBOOT_TIMEOUT'] = max(5.0, min(600.0, float(CONFIG['REMOTE_WAIT_AFTER_REBOOT_TIMEOUT'])))

        if is_remote_mode():
            try:
                CONFIG['REMOTE_INFERENCE_MAX_FPS'] = float(input.numRemoteInferenceMaxFps())
            except Exception:
                CONFIG['REMOTE_INFERENCE_MAX_FPS'] = float(CONFIG.get('REMOTE_INFERENCE_MAX_FPS', 10.0) or 10.0)

        # Update the log level
        configure_logging(input.txtLoglevel())

        # Save the configuration to the config file
        _ = set_language(CONFIG['LANGUAGE'])

        # Check for invalid combinations of settings
        if input.selectedModel().startswith("tflite::") and input.btnUseCameraForMotionDetection():
            CONFIG['USE_CAMERA_FOR_MOTION_DETECTION'] = False
            ui.notification_show(
                _("Invalid configuration: ") +
                _("You cannot use the camera for motion detection in combination with an original Kittyflap model. You need a custom trained model for this.") + "\n" +
                _("The setting \"{}\" was disabled.").format(_("Use camera for motion detection")),
                duration=None, type="warning"
            )
        if input.selectedModel().startswith("tflite::") and input.btnUseCameraForCatDetection():
            CONFIG['USE_CAMERA_FOR_CAT_DETECTION'] = False
            ui.notification_show(
                _("Invalid configuration: ") +
                _("You cannot use the camera for cat detection in combination with an original Kittyflap model. You need a custom trained model for this.") + "\n" +
                _("The setting \"{}\" was disabled.").format(_("Use camera for cat detection")),
                duration=None, type="warning"
            )
        
        if save_config():
            ui.notification_show(_("Kittyhack configuration updated successfully."), duration=5, type="message")
            model_reload_failed = False

            if selected_model_changed:
                try:
                    reload_ok, active_model_handler = reload_model_handler_runtime()
                    if reload_ok:
                        model_handler = active_model_handler
                        ui.notification_show(
                            _("Model change applied."),
                            duration=6,
                            type="message",
                        )
                    else:
                        model_reload_failed = True
                        ui.notification_show(
                            _("Model change could not be applied live. A reboot is still required."),
                            duration=10,
                            type="warning",
                        )
                except Exception as e:
                    model_reload_failed = True
                    logging.error(f"Failed to apply model change live: {e}")

            if language_changed:
                ui.notification_show(_("Please restart the kittyflap in the [SYSTEM] section, to apply the new language."), duration=30, type="message")
                update_mqtt_language()

            if (
                (selected_model_changed and model_reload_failed) or
                hostname_changed or
                img_processing_cores_changed or
                rfid_state_changed
            ):
                ui.modal_remove()
                ui.modal_show(
                    ui.modal(
                        _("A restart is required to apply the changes. Do you want to reboot the kittyflap now?"),
                        title=_("Restart required"),
                        easy_close=True,
                        footer=ui.div(
                            ui.input_action_button("btn_modal_reboot_ok", _("Reboot")),
                            ui.input_action_button("btn_modal_cancel", _("Cancel")),
                        )
                    )
                )
            
            if mqtt_settings_changed:
                logging.info("MQTT settings changed. Restarting MQTT client...")
                success = restart_mqtt()
                if not success:
                    ui.notification_show(_("Failed to restart MQTT client. Check the logs for details."), duration=10, type="error")
            else:
                # Just update the door configuration in MQTT
                update_mqtt_config('ALLOWED_TO_ENTER')
                update_mqtt_config('ALLOWED_TO_EXIT')
            
            # Sync live view input
            ui.update_select("quick_allowed_to_enter", selected=str(CONFIG['ALLOWED_TO_ENTER'].value))
            ui.update_select("quick_allowed_to_exit", selected=str(CONFIG['ALLOWED_TO_EXIT'].value))
            # Trigger UI components that depend on these settings to re-render
            reload_trigger_config.set(reload_trigger_config.get() + 1)

        else:
            ui.notification_show(_("Failed to save the Kittyhack configuration."), duration=10, type="error")

    @output
    @render.ui
    def ui_wlan_configured_connections():
        return ui.layout_column_wrap(
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.p(_("Configured WLANs"))
                    ),
                    ui.HTML('<div id="pleasewait_wlan_configured" class="spinner-container"><div class="spinner"></div></div>'),
                    ui.output_ui("configured_wlans_table"),
                    ui.hr(),
                    ui.input_action_button(id="btn_wlan_add", label=_("Add new WLAN"), icon=icon_svg("plus")),
                    full_screen=False,
                    class_="generic-container",
                    min_height="150px"
                ),
                width="400px"
            )
        )

    @render.table
    @reactive.event(reload_trigger_wlan, ignore_none=True)
    def configured_wlans_table():
        # Properly handle SSIDs with special characters
        try:
            def _table_status_icon(icon_name: str, color_class: str, title: str):
                try:
                    icon = icon_svg(icon_name, margin_left="0", margin_right="0")
                except Exception:
                    return ui.span("•", class_=f"table-icon {color_class}", title=title)
                return ui.span(icon, class_=f"table-icon {color_class}", title=title)

            configured_wlans = get_wlan_connections()
            i = 0
            for wlan in configured_wlans:
                unique_id = hashlib.md5(os.urandom(16)).hexdigest()
                if wlan['connected']:
                    wlan['connected_icon'] = _table_status_icon("circle-check", "text-success", _("Connected"))
                else:
                    wlan['connected_icon'] = _table_status_icon("circle", "text-muted", _("Not connected"))
                wlan['actions'] = ui.div(
                    ui.tooltip(
                        btn_wlan_connect(f"btn_wlan_connect_{unique_id}"),
                        _("Enforce connection to this WLAN"),
                        id=f"tooltip_wlan_connect_{unique_id}",
                        options={"trigger": "hover"},
                    ),
                    ui.tooltip(
                        btn_wlan_modify(f"btn_wlan_modify_{unique_id}"),
                        _("Modify this WLAN"),
                        id=f"tooltip_wlan_modify_{unique_id}",
                        options={"trigger": "hover"},
                    ),
                )
                # Add new event listeners for the buttons
                wlan_modify_server(f"btn_wlan_modify_{unique_id}", wlan["ssid"])
                wlan_connect_server(f"btn_wlan_connect_{unique_id}", wlan["ssid"])
                i += 1

            # Create a pandas DataFrame from the available WLANs
            df = pd.DataFrame(configured_wlans)
            df = df[['ssid', 'priority', 'connected_icon', 'actions']]  # Select only the columns we want to display
            df.columns = ['SSID', _('Priority'), "", ""]  # Rename columns for display

            return (
                df.style.set_table_attributes('class="dataframe shiny-table table w-auto"')
                .hide(axis="index")
            )
        except Exception as e:
            logging.error(f"Failed to scan for available WLANs: {e}")
            # Return an empty DataFrame with an error message
            return pd.DataFrame({
                'ERROR': [_('Failed to scan for available WLANs')]
            })
        finally:
            ui.remove_ui("#pleasewait_wlan_configured")

    @reactive.effect
    @reactive.event(input.btn_wlan_add)
    def wlan_add():
        wlan_add_dialog()

    @reactive.effect
    @reactive.event(input.btn_wlan_save)
    def wlan_save():
        _set_wlan_action_in_progress(True)
        ssid = input.txtWlanSSID()
        password = input.txtWlanPassword()
        priority = input.numWlanPriority()
        password_changed = True if password else False
        logging.info(f"Updating WLAN configuration: SSID={ssid}, Priority={priority}, Password changed={password_changed}")
        ui.modal_remove()
        ui.modal_show(
            ui.modal(
                _("The WLAN connection will be interrupted now!"),
                ui.br(),
                _("Please wait a few seconds. If the page does not reload automatically within 30 seconds, please reload it manually."),
                title=_("Updating WLAN configuration..."),
                footer=None
            )
        )
        try:
            success = manage_and_switch_wlan(ssid, password, priority, password_changed)
            if success:
                ui.notification_show(_("WLAN configuration for {} updated successfully.").format(ssid), duration=5, type="message")
                reload_trigger_wlan.set(reload_trigger_wlan.get() + 1)
            else:
                ui.notification_show(_("Failed to update WLAN configuration for {}").format(ssid), duration=10, type="error")
        finally:
            _set_wlan_action_in_progress(False)
            ui.modal_remove()

    @output
    @render.ui
    def ui_wlan_available_networks():
        return ui.layout_column_wrap(
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.p(_("Available WLANs"))
                    ),
                    ui.HTML('<div id="pleasewait_wlan_scan" class="spinner-container"><div class="spinner"></div></div>'),
                    ui.output_ui("scan_wlan_results_table"),
                    full_screen=False,
                    class_="generic-container",
                    min_height="150px"
                ),
                width="400px"
            )
        )
    
    @render.table
    def scan_wlan_results_table():
        # Get the available WLANs
        reactive.invalidate_later(30.0)
        try:
            def _table_signal_icon(color_class: str, title: str):
                try:
                    icon = icon_svg("wifi", margin_left="0", margin_right="0")
                except Exception:
                    return ui.span("•", class_=f"table-icon {color_class}", title=title)
                return ui.span(icon, class_=f"table-icon {color_class}", title=title)

            available_wlans = scan_wlan_networks()
            for wlan in available_wlans:
                signal_strength = wlan['bars']
                signal_percent = int(wlan.get('signal') or 0)
                if signal_strength == 0:
                    color_class = "text-muted"
                    title = _("No signal")
                elif signal_strength == 1:
                    color_class = "text-danger"
                    title = _("Weak signal")
                elif 2 <= signal_strength <= 3:
                    color_class = "text-warning"
                    title = _("Medium signal")
                else:
                    color_class = "text-success"
                    title = _("Strong signal")

                wlan['signal_icon'] = ui.span(
                    f"{signal_percent}% ",
                    _table_signal_icon(color_class, title),
                )
                

            # Create a pandas DataFrame from the available WLANs
            df = pd.DataFrame(available_wlans)
            df = df[['ssid', 'channel', 'signal_icon']]  # Select only the columns we want to display
            df.columns = ['SSID', _('Channel'), _('Signal')]  # Rename columns for display
            return (
                df.style.set_table_attributes('class="dataframe shiny-table table w-auto"')
                .hide(axis="index")
            )
        except Exception as e:
            logging.error(f"Failed to scan for available WLANs: {e}")
            # Return an empty DataFrame with an error message
            return pd.DataFrame({
                'ERROR': [_('Failed to scan for available WLANs')]
            })
        finally:
            ui.remove_ui("#pleasewait_wlan_scan")
            
    @render.download(filename="kittyhack_logs.zip")
    def download_logfile():
        # Show a modal dialog to inform the user that the download is in progress
        ui.modal_show(
            ui.modal(
                ui.div(
                    ui.markdown(_("Creating log archive. Please wait...")),
                    ui.HTML('<div class="spinner-container"><div class="spinner"></div></div>'),
                ),
                title=_("Preparing Download"),
                easy_close=False,
                footer=None
            )
        )
        
        try:
            # Create a temporary directory for the logs
            temp_dir = os.path.join("/tmp", f"kittyhack_logs_{int(tm.time())}")
            os.makedirs(temp_dir, exist_ok=True)
            
            # Path to the zip file we'll create
            zip_file_path = os.path.join("/tmp", f"kittyhack_logs.zip")

            # If the zip file already exists, remove it
            if os.path.exists(zip_file_path):
                os.remove(zip_file_path)
            
            # Export journal to a file. Do NOT filter by unit — include all system logs.
            journal_file_path = os.path.join(temp_dir, "journalctl.log")
            try:
                result = subprocess.run(
                    ["/usr/bin/journalctl", "-n", "50000", "--no-pager", "--quiet", "--output=short-iso-precise"],
                    capture_output=True,
                    text=True,
                )
                # If returncode != 0 capture whatever output we got and stderr for diagnostics
                if result.returncode == 0:
                    journal_text = result.stdout
                else:
                    logging.error(f"journalctl exited with code {result.returncode}: {result.stderr.strip()}")
                    journal_text = (result.stdout or "") + "\n\n--- journalctl stderr ---\n\n" + (result.stderr or "")
            except FileNotFoundError:
                logging.error("journalctl not found on system")
                journal_text = "ERROR: journalctl not found on system. No systemd journal available."
            except Exception as e:
                logging.error(f"Failed to export journal: {e}")
                journal_text = f"ERROR: Failed to run journalctl: {e}"

            # Ensure we always write a file (even if empty or containing the error message)
            try:
                with open(journal_file_path, "w", encoding="utf-8") as jf:
                    jf.write(journal_text or "No journal output captured.")
            except Exception as e:
                logging.error(f"Failed to write journal file {journal_file_path}: {e}")
                # continue — zip will be created without the journal

            # Create a zip file with compression and include only the journal and sanitized config + setup logs
            with zipfile.ZipFile(zip_file_path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                # Add the journalctl export
                if os.path.exists(journal_file_path):
                    z.write(journal_file_path, arcname="journalctl.log")
                
                # Add a sanitized version of config.ini to the zip file
                sanitized_config_path = os.path.join(temp_dir, "config_sanitized.ini")
                
                # Create a sanitized version of the config using the CONFIG dictionary
                with open(sanitized_config_path, 'w') as sanitized_file:
                    sanitized_file.write("[Settings]\n")
                    for key, value in CONFIG.items():
                        # Use the existing function to mask sensitive values
                        loggable_value = get_loggable_config_value(key, value)
                        sanitized_file.write(f"{key.lower()} = {loggable_value}\n")
                
                # Add the sanitized config to the zip file
                z.write(sanitized_config_path, arcname="config_sanitized.ini")

                system_log_dir = "/var/log"
                if os.path.exists(system_log_dir):
                    # First, find all setup directories with timestamps
                    setup_dirs = []
                    for root, dirs, x in os.walk(system_log_dir):
                        for dir_name in dirs:
                            if dir_name.startswith("kittyhack-setup-"):
                                full_path = os.path.join(root, dir_name)
                                try:
                                    # Extract timestamp from directory name (format: kittyhack-setup-YYYYMMDD-HHMMSS)
                                    timestamp_str = dir_name.replace("kittyhack-setup-", "")
                                    timestamp = datetime.strptime(timestamp_str, "%Y%m%d-%H%M%S")
                                    setup_dirs.append((full_path, timestamp))
                                except ValueError:
                                    # If timestamp parsing fails, still include with minimum date
                                    setup_dirs.append((full_path, datetime.min))
                    
                    # Process setup directories (latest first) - include up to 3 most recent directories
                    if setup_dirs:
                        # Sort by timestamp (newest first)
                        setup_dirs.sort(key=lambda x: x[1], reverse=True)
                        # Include up to the latest 3 setup directories
                        for i, (setup_dir, timestamp) in enumerate(setup_dirs[:3]):
                            # Get a short name for the directory based on its timestamp
                            if timestamp != datetime.min:
                                dir_short_name = timestamp.strftime("%Y%m%d-%H%M%S")
                            else:
                                dir_short_name = f"unknown-{i+1}"
                                
                            for root, x, files in os.walk(setup_dir):
                                for file in files:
                                    file_path = os.path.join(root, file)
                                    rel_path = os.path.relpath(file_path, system_log_dir)
                                    z.write(file_path, arcname=f"system_logs/{dir_short_name}/{rel_path}")
            
            # Clean up the temp directory files
            for file in os.listdir(temp_dir):
                try:
                    os.remove(os.path.join(temp_dir, file))
                except:
                    pass
            try:
                os.rmdir(temp_dir)
            except:
                pass
            
            ui.modal_remove()
            return zip_file_path
        except Exception as e:
            logging.error(f"Failed to create logs zip file: {e}")
            ui.notification_show(_("Failed to create logs zip file: {}").format(e), duration=10, type="error")
            ui.modal_remove()
            return None
        
    @render.download(filename=lambda: (
        "kittyhack_database_unavailable.txt" if (ids_with_original_blob and len(ids_with_original_blob) > 0)
        else "kittyhack_database.db"
    ))
    def download_kittyhack_db():
        try:
            from io import BytesIO

            # Block snapshot download while legacy image migration is in progress
            global ids_with_original_blob
            if ids_with_original_blob and len(ids_with_original_blob) > 0:
                # Inform user and return a small text file to avoid server-side exceptions
                ui.notification_show(
                    _("Database snapshot is unavailable during image migration ({} pictures remaining). Please try again later.").format(len(ids_with_original_blob)),
                    duration=12,
                    type="warning"
                )
                msg = (
                    "Kittyhack database snapshot is currently unavailable.\n"
                    f"Legacy image migration in progress: {len(ids_with_original_blob)} pictures remaining.\n"
                    "Please try again later."
                )
                return BytesIO(msg.encode("utf-8"))

            dest_path = os.path.join("/tmp", "kittyhack_database.db")
            result = backup_database_sqlite(CONFIG['KITTYHACK_DATABASE_PATH'], dest_path)
            if not result.success:
                logging.error(f"[DOWNLOAD_DB] Backup snapshot failed: {result.message}")
                ui.notification_show(_("Failed to create a consistent snapshot: {}").format(result.message), duration=12, type="error")
                # Ensure we don't return None; provide a small error text payload
                err_msg = f"Failed to create database snapshot: {result.message}\nCheck server logs for details."
                try:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                except Exception:
                    pass
                return BytesIO(err_msg.encode("utf-8"))

            return dest_path
        except Exception as e:
            logging.error(f"[DOWNLOAD_DB] Unexpected error: {e}")
            ui.notification_show(_("Failed to prepare database download: {}").format(e), duration=12, type="error")
            # Return a minimal error payload to avoid NoneType iteration errors
            from io import BytesIO
            return BytesIO(f"Unexpected error while preparing download: {e}".encode("utf-8"))
    
    @render.download(filename="kittyflap.db")
    def download_kittyflap_db():
        if os.path.exists(CONFIG['DATABASE_PATH']):
            return CONFIG['DATABASE_PATH']
        else:
            ui.notification_show(_("The original kittyflap database file does not exist."), duration=10, type="error")
            return None
    
    @reactive.Effect
    @reactive.event(input.btn_retry_latest_version)
    def on_retry_latest_version():
        try:
            # Ensure network is up, then fetch latest version
            if wait_for_network(timeout=5):
                CONFIG['LATEST_VERSION'] = read_latest_kittyhack_version(timeout=5)
                if CONFIG['LATEST_VERSION'] == "unknown":
                    ui.notification_show(_("Latest version check failed."), duration=8, type="warning")
            else:
                ui.notification_show(_("Internet connection not available."), duration=8, type="warning")
        except Exception as e:
            logging.warning(f"[VERSION] Retry failed: {e}")
            ui.notification_show(_("Retry failed: {}").format(e), duration=10, type="error")
        finally:
            # Trigger re-render of INFO section
            reload_trigger_info.set(reload_trigger_info.get() + 1)

    @output
    @render.ui
    @reactive.event(reload_trigger_info, ignore_none=True)
    def ui_info():
        target_git_version = "unknown"
        remote_target_connected = False
        if is_remote_mode():
            try:
                from src.remote.control_client import RemoteControlClient

                client = RemoteControlClient.instance()
                client.ensure_started()
                remote_target_connected = bool(client.wait_until_ready(timeout=0))
                info = client.request_target_version(timeout=0.6) if remote_target_connected else None
                if not info:
                    info = client.get_target_version_info()
                if info and str(info.get("git_version") or "").strip():
                    target_git_version = str(info.get("git_version") or "unknown")
            except Exception:
                target_git_version = "unknown"

        versions_mismatch = bool(
            is_remote_mode()
            and remote_target_connected
            and target_git_version not in ("", "unknown")
            and git_version not in ("", "unknown")
            and str(target_git_version) != str(git_version)
        )

        # Check if the current version is different from the latest version
        latest_version = CONFIG['LATEST_VERSION']
        if latest_version == "unknown":
            ui_update_kittyhack = ui.div(
                ui.markdown(_("Unable to fetch the latest version from github. Please try it again later or check your internet connection.")),
                ui.br(),
                ui.div(
                    ui.input_task_button("btn_retry_latest_version", _("Retry latest version check"), icon=icon_svg("rotate")),
                    style_="text-align: center;"
                )
            )

            if versions_mismatch and git_repo_available:
                ui_update_kittyhack = ui_update_kittyhack, ui.hr(), ui.div(
                    ui.markdown(
                        _("Remote/target version mismatch detected.")
                        + " "
                        + _("You can update only one side to match versions before running a full update.")
                    ),
                    ui.div(
                        ui.input_task_button("update_target_kittyhack", _("Update target device only"), icon=icon_svg("download"), class_="btn-primary"),
                        ui.br(),
                        ui.input_task_button("update_remote_kittyhack", _("Update this device only"), icon=icon_svg("download"), class_="btn-default"),
                        style_="text-align: center;"
                    ),
                )
        elif git_version != latest_version:
            release_notes_block = None
            try:
                # Fetch the release notes of the latest version
                release_notes = fetch_github_release_notes(latest_version)
                release_notes = filter_release_notes_for_language(release_notes, CONFIG.get('LANGUAGE', 'en'))
                release_notes_block = ui.div(
                    ui.markdown("**" + _("Release Notes for") + " " + latest_version + ":**"),
                    ui.div(
                        ui.markdown(release_notes),
                        class_="release_notes"
                    ),
                    ui.br()
                )
            except Exception as e:
                logging.warning(f"[VERSION] Failed to fetch release notes: {e}")
                release_notes_block = ui.div(
                    ui.markdown(_("Release notes unavailable: {}").format(e)),
                    ui.br()
                )

            ui_update_kittyhack = release_notes_block if release_notes_block else ui.div()

            if git_repo_available:
                ui_update_kittyhack = ui_update_kittyhack, ui.div(
                    ui.markdown(_("Automatic update to **{}**:").format(latest_version)),
                    ui.input_task_button("update_kittyhack", _("Update Kittyhack"), icon=icon_svg("download"), class_="btn-primary"),
                    ui.br(),
                    ui.help_text(_("Important: A stable WLAN connection is required for the update process.")),
                    ui.br(),
                    ui.help_text(_("The update will end with a reboot of the Kittyflap.")),
                    ui.markdown(_("Check out the [Changelog](https://github.com/floppyFK/kittyhack/releases) to see what's new in the latest version.")),
                )

                if versions_mismatch:
                    ui_update_kittyhack = ui_update_kittyhack, ui.hr(), ui.div(
                        ui.markdown(
                            _("Remote/target version mismatch detected.")
                            + " "
                            + _("Use one of the buttons below if you want to update only one device.")
                        ),
                        ui.div(
                            ui.input_task_button("update_target_kittyhack", _("Update target device only"), icon=icon_svg("download"), class_="btn-primary"),
                            ui.br(),
                            ui.input_task_button("update_remote_kittyhack", _("Update this device only"), icon=icon_svg("download"), class_="btn-default"),
                            style_="text-align: center;"
                        ),
                    )
            else:
                ui_update_kittyhack = ui_update_kittyhack, ui.div(
                    ui.markdown(
                        _("This installation does not appear to be a git clone. Automatic updates require a git repository (git clone).")
                    )
                )

            if git_repo_available:
                try:
                    # Check for local changes in the git repository and warn the user
                    result = subprocess.run(["/bin/git", "status", "--porcelain"], capture_output=True, text=True, check=True)
                    if result.stdout.strip():
                        result = subprocess.run(["/bin/git", "status"], capture_output=True, text=True, check=True)
                        ui_update_kittyhack = ui_update_kittyhack, ui.div(
                            ui.hr(),
                            ui.markdown(
                                f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                                + _("WARNING: Local changes detected in the git repository in `{}`.").format(kittyhack_root()) + "\n\n" +
                                _("If you proceed with the update, these changes will be lost (the database and configuration will not be affected).") + "\n\n" +
                                _("Please commit or stash your changes manually before updating, if you want to keep them.")
                            ),
                            ui.h6(_("Local changes:")),
                            ui.div(
                                result.stdout,
                                class_="release_notes",
                                style_="font-family: monospace; white-space: pre-wrap;"
                            )
                        )
                except Exception as e:
                    ui_update_kittyhack = ui_update_kittyhack, ui.div(
                        ui.hr(),
                        ui.markdown(_("Unable to check local git changes: {}").format(e))
                    )
            else:
                # No local git repo: update button is hidden
                pass
        
        else:
            ui_update_kittyhack = ui.markdown(_("You are already using the latest version of Kittyhack."))
            if versions_mismatch and git_repo_available:
                ui_update_kittyhack = ui_update_kittyhack, ui.hr(), ui.div(
                    ui.markdown(
                        _("Remote/target version mismatch detected.")
                        + " "
                        + _("Use one of the buttons below to align the versions.")
                    ),
                    ui.div(
                        ui.input_task_button("update_target_kittyhack", _("Update target device only"), icon=icon_svg("download"), class_="btn-primary"),
                        ui.br(),
                        ui.input_task_button("update_remote_kittyhack", _("Update this device only"), icon=icon_svg("download"), class_="btn-default"),
                        style_="text-align: center;"
                    ),
                )

        # Check if the original kittyflap database file still exists
        kittyflap_db_file_exists = os.path.exists(CONFIG['DATABASE_PATH'])
        ui_kittyflap_section = None
        if kittyflap_db_file_exists and (get_file_size(CONFIG['DATABASE_PATH']) > 50):
            ui_kittyflap_db = ui.div(
                ui.markdown(
                    _("The original kittyflap database file consumes currently **{:.1f} MB** of disk space.").format(get_file_size(CONFIG['DATABASE_PATH'])) + "\n\n" +
                    _("The file contains a lot pictures which could not be uploaded to the original kittyflap servers anymore.") + "\n\n" +
                    _("You could delete the pictures from it to free up disk space.")
                ),
                ui.input_task_button("clear_kittyflap_db", _("Remove pictures from original Kittyflap Database"), icon=icon_svg("trash")),
                ui.download_button("download_kittyflap_db", _("Download Kittyflap Database"), icon=icon_svg("download")),
            )

            # Build the whole section (card) only if the file exists
            ui_kittyflap_section = ui.div(
                ui.card(
                    ui.card_header(ui.h4(_("Original Kittyflap Database"), style_="text-align: center;")),
                    ui.br(),
                    ui_kittyflap_db,
                    ui.br(),
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            )

        return ui.div(
            ui.div(
                ui.card(
                    ui.card_header(ui.h4(_("Information"), style_="text-align: center;")),
                    ui.br(),
                    ui.markdown(
                        _("Kittyhack is an open-source project that enables offline use of the Kittyflap cat door—completely without internet access.") + "\n\n" +
                        _("It was created after the manufacturer of Kittyflap filed for bankruptcy, rendering the associated app non-functional.")
                    ),
                    ui.hr(),
                    ui.markdown(
                        _("**Important Notes**") +  "\n\n" +
                        _("I have no connection to the manufacturer of Kittyflap. This project was developed on my own initiative to continue using my Kittyflap.") + "\n\n" +
                        _("If you find any bugs or have suggestions for improvement, please report them on the GitHub page.")
                    ),
                    ui.HTML("<center><p><a href='https://github.com/floppyFK/kittyhack' target='_blank'>" + str(icon_svg('square-github')) + " " + _("GitHub Repository") + "</a></p></center>"),
                    ui.hr(),
                    ui.markdown(
                        _("**License**") + "\n\n" +
                        _("This project is licensed under the MIT License.") + " " +
                        _("Copyright (c) 2025 Florian Kispert.")
                    ),
                    ui.HTML("<center><p><a href='https://github.com/floppyFK/kittyhack/blob/main/LICENSE' target='_blank'>" + str(icon_svg('file-lines')) + " " + _("MIT License (full text)") + "</a></p></center>"),
                    ui.HTML("<center><p style='margin-bottom: 0.3rem;'>" + _("If you like Kittyhack, you can support the project with a small donation:") + "</p></center>"),
                    ui.HTML("<center><p><a href='https://www.paypal.com/donate?hosted_button_id=QY57YUADYRVW2' target='_blank' class='btn btn-primary'>" + str(icon_svg('paypal')) + " " + _("Donate via PayPal") + "</a></p></center>"),
                    ui.br(),
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),
            ui.div(
                ui.card(
                    ui.card_header(ui.h4(_("Version Information"), style_="text-align: center;")),
                    ui.br(),
                    ui.markdown(
                        "**" + _("Current Version") + ":** `" + git_version + "`" + "  \n"
                        + ("**" + _("Target Device Version") + ":** `" + target_git_version + "`" + "  \n" if is_remote_mode() else "")
                        + "**" + _("Latest Version") + ":** `" + latest_version + "`"
                    ),
                    (
                        ui.div(
                            ui.markdown(
                                f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                                + _("Remote/target versions are different. This can cause confusing update behavior.")
                            ),
                            class_="generic-container warning-container",
                        )
                        if versions_mismatch
                        else ui.HTML("")
                    ),
                    ui_update_kittyhack,
                    ui.br(),
                    ui.h5("Changelogs"),
                    ui.div(ui.input_action_button("btn_changelogs", _("Show all Changelogs"), icon=icon_svg("info"))),
                    ui.br(),
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),
            ui.div(
                ui.card(
                    ui.card_header(ui.h4(_("System Information"), style_="text-align: center;")),
                    ui.br(),
                    ui.output_ui("ui_system_info"),
                    ui.hr(),
                    ui.h5(_("Kittyhack Database")),
                    ui.h6(_("Backup and Restore your Kittyhack database")),
                    ui.div(ui.download_button("download_kittyhack_db", _("Download Kittyhack Database"), icon=icon_svg("download"))),
                    ui.br(),
                    # Group: Restore Kittyhack DB
                    ui.div(
                        ui.div(
                            uix.input_file("upload_kittyhack_db", _("Restore Kittyhack Database (.db)"), accept=[".db"], multiple=False, width="90%"),
                            ui.div(
                                ui.input_task_button("restore_kittyhack_db", _("Restore Database"), icon=icon_svg("rotate"), class_="btn-outline-danger"),
                                style_="text-align: center; margin-top: 6px;"
                            ),
                            class_="generic-container",
                            style_=("border: 1px solid #ddd; border-radius: 6px; padding: 10px; margin-top: 8px;"
                                    "background: #fafafa;")
                        )
                    ),
                    ui.hr(),
                    ui.h5(_("Configuration File")),
                    ui.h6(_("Backup and Restore your Kittyhack configuration")),
                    ui.div(ui.download_button("download_config", _("Download Configuration File"), icon=icon_svg("download"))),
                    ui.br(),
                    # Group: Restore Configuration
                    ui.div(
                        ui.div(
                            uix.input_file("upload_config", _("Restore Configuration File (config.ini)"), accept=[".ini"], multiple=False, width="90%"),
                            ui.div(
                                ui.input_task_button("restore_config", _("Restore Configuration"), icon=icon_svg("rotate"), class_="btn-outline-danger"),
                                style_="text-align: center; margin-top: 6px;"
                            ),
                            class_="generic-container",
                            style_=("border: 1px solid #ddd; border-radius: 6px; padding: 10px; margin-top: 8px;"
                                    "background: #fafafa;")
                        )
                    ),
                    ui.br(),
                    ui.markdown(_("> Note: If the database or the configuration gets restored from a backup, then the Kittyflap will be rebooted afterwards to apply the new configuration.")),
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),
            # Include Original Kittyflap Database card only if present
            (ui_kittyflap_section if ui_kittyflap_section else ui.HTML("")),
            ui.div(
                ui.card(
                    ui.card_header(ui.h4(_("Progressive Web App (PWA)"), style_="text-align: center;")),
                    ui.br(),
                    ui.markdown(
                        _("You can install Kittyhack as a") +" [" + _("Progressive Web App") + "](https://web.dev/learn/pwa/progressive-web-apps) " + _("on your Smartphone or computer for easier access.") + "  \n" +
                        _("Such a PWA can be added to your home screen and launched like a native app, without needing to open a web browser.")
                    ),
                    ui.div(
                        # HTTPS Warning message
                        ui.div(
                            ui.markdown(
                                f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} " + _("**HTTPS Required**") + "\n\n" +
                                _("PWA installation requires a secure connection (HTTPS). You are currently accessing Kittyhack via HTTP.") + "  \n\n" +
                                _("You'll need to set up a reverse proxy with HTTPS.") + " " +
                                _("If you want to setup a reverse proxy in your home network, you can watch") +" [" + _("this guide") + "](https://schroederdennis.de/allgemein/nginx-proxy-manager-nginx-reverse-proxy-vorgestellt/)."
                            ),
                            id="pwa_https_warning",
                            style_="display: none; color: #e74a3b; padding: 10px; border: 1px solid #e74a3b; border-radius: 5px; margin: 10px 0;"
                        ),
                        # Already installed message
                        ui.div(
                            ui.markdown(f"{icon_svg('circle-check', margin_left='-0.1em')} " + _("**App is already installed on this device!**")),
                            id="pwa_already_installed", 
                            style_="display: none; color: #1cc88a; padding: 10px; text-align: center;"
                        ),
                        # Success message
                        ui.div(
                            ui.markdown(f"{icon_svg('circle-check', margin_left='-0.1em')} " + _("**Installation successful!**")),
                            id="pwa_installed_success", 
                            style_="display: none; color: #1cc88a; padding: 10px; text-align: center;"
                        ),
                        # Install button
                        ui.div(
                            ui.input_action_button(
                                id="pwa_install_button",
                                label=_("Install as App"),
                                icon=icon_svg("download"),
                                class_="btn-default"
                            ),
                            style_="text-align: center; margin-top: 10px;"
                        ),
                        id="pwa_install_container"
                    ),
                    ui.br(),
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),
            ui.div(
                ui.card(
                    ui.card_header(ui.h4(_("Logfiles"), style_="text-align: center;")),
                    ui.br(),
                    ui.div(ui.download_button("download_logfile", _("Download Kittyhack Logfile"), icon=icon_svg("download"))),
                    ui.br(),
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),
            ui.br(),
            ui.br(),
            ui.br(),
            ui.br(),
        )
    
    @render.table
    def ui_system_info():
        reactive.invalidate_later(10.0)

        database_size = get_database_size()
        free_disk_space = get_free_disk_space()
        total_disk_space = get_total_disk_space()
        used_ram_space = get_used_ram_space()
        total_ram_space = get_total_ram_space()
        ram_usage_percentage = (used_ram_space / total_ram_space) * 100

        def _wlan_status_icon(color_class: str, title: str):
            try:
                icon = icon_svg("wifi", margin_left="0", margin_right="0")
            except Exception:
                return ui.span("•", class_=f"table-icon {color_class}", title=title)
            return ui.span(icon, class_=f"table-icon {color_class}", title=title)

        # Get WLAN status information
        if is_remote_mode():
            wlan_info = _("Not available in remote-mode")
        else:
            try:
                wlan = subprocess.run(["/sbin/iwconfig", "wlan0"], capture_output=True, text=True, check=True)
                if "Link Quality=" in wlan.stdout and "Signal level=" in wlan.stdout:
                    quality = wlan.stdout.split("Link Quality=")[1].split(" ")[0]
                    signal = wlan.stdout.split("Signal level=")[1].split(" ")[0]
                    quality_value = float(quality.split('/')[0]) / float(quality.split('/')[1])

                    if quality_value >= 0.8:
                        color_class = "text-success"
                        title = _("Strong signal")
                    elif quality_value >= 0.4:
                        color_class = "text-warning"
                        title = _("Medium signal")
                    else:
                        color_class = "text-danger"
                        title = _("Weak signal")

                    wlan_info = ui.span(
                        _wlan_status_icon(color_class, title),
                        " ",
                        _("Quality: {}, Signal: {} dBm").format(quality, signal),
                    )
                else:
                    wlan_info = _("Not connected")
            except Exception:
                wlan_info = _("Unable to determine")

        # Create a DataFrame with the system information
        df = pd.DataFrame({
            'Property': [_('Kittyhack database size'), _('Free disk space'), _('Used RAM'), _('WLAN Status')],
            'Value': [
                f'{database_size:.1f} MB',
                f'{free_disk_space:.1f} MB / {total_disk_space:.1f} MB',
                f'{used_ram_space:.1f} MB / {total_ram_space:.1f} MB ({ram_usage_percentage:.1f}%)',
                wlan_info
            ]
        })
        return (
            df.style.set_table_attributes('class="dataframe shiny-table table w-auto"')
            .hide(axis="index")
        )
        
    @reactive.Effect
    @reactive.event(input.btn_changelogs)
    def show_changelogs():
        changelog_text = get_changelogs(after_version="v1.0.0", language=CONFIG['LANGUAGE'])
        ui.modal_show(
            ui.modal(
                ui.div(
                    ui.markdown(changelog_text),
                ),
                title=_("Changelogs"),
                easy_close=True,
                size="xl",
                footer=ui.div(
                    ui.input_action_button("btn_modal_cancel", _("Close")),
                )
            )
        )

    @reactive.Effect
    @reactive.event(input.clear_kittyflap_db)
    def clear_original_kittyflap_db():
        with ui.Progress(min=1, max=2) as p:
            p.set(1, message="Deleting the pictures", detail="This may take a while...")
            if os.path.exists(CONFIG['DATABASE_PATH']):
                try:
                    clear_original_kittyflap_database(CONFIG['DATABASE_PATH'])
                    ui.notification_show(_("The pictures from the original kittyflap database were removed successfully."), duration=5, type="message")
                    p.set(2)
                except Exception as e:
                    ui.notification_show(_("An error occurred while deleting the pictures from the original kittyflap database: {}").format(e), duration=10, type="error")
            else:
                ui.notification_show(_("The original kittyflap database file does not exist anymore."), duration=5, type="message")
        reload_trigger_info.set(reload_trigger_info.get() + 1)

    @render.download(filename="config.ini")
    def download_config():
        try:
            # Simply return the path to the actual config.ini file
            config_ini_path = os.path.join(os.getcwd(), "config.ini")
            
            if os.path.exists(config_ini_path):
                return config_ini_path
            else:
                logging.error("config.ini not found")
                ui.notification_show(_("config.ini not found"), duration=10, type="error")
                return None
        except Exception as e:
            logging.error(f"Failed to download config.ini file: {e}")
            ui.notification_show(_("Failed to download config.ini file: {}").format(e), duration=10, type="error")
            return None

    @reactive.Effect
    @reactive.event(input.upload_kittyhack_db)
    def on_upload_kittyhack_db():
        files = input.upload_kittyhack_db()
        if not files:
            ui.notification_show(_("No file selected."), duration=8, type="error")
            last_uploaded_db_path.set(None)
            return
        f = files[0]
        name = f.get('name', '')
        src_path = f.get('datapath', '')
        if not src_path or not os.path.exists(src_path):
            ui.notification_show(_("Uploaded file not found."), duration=8, type="error")
            last_uploaded_db_path.set(None)
            return
        if not name.lower().endswith(".db"):
            ui.notification_show(_("Invalid file type. Please upload a .db file."), duration=10, type="error")
            last_uploaded_db_path.set(None)
            return
        # Only store path and inform user; do not restore yet
        last_uploaded_db_path.set(src_path)
        ui.notification_show(_("Database file uploaded. Click 'Restore Database' to apply."), duration=8, type="message")

    @reactive.Effect
    @reactive.event(input.restore_kittyhack_db)
    def on_restore_kittyhack_db():
        src_path = last_uploaded_db_path.get()
        if not src_path or not os.path.exists(src_path):
            ui.notification_show(_("No uploaded database ready to restore."), duration=8, type="error")
            return
        try:
            # Stop backend to avoid DB locks
            sigterm_monitor.halt_backend()
            tm.sleep(1.0)
            # Backup current DB before overwrite
            backup_dir = os.path.dirname(CONFIG['KITTYHACK_DATABASE_PATH']) or "."
            backup_name = f"kittyhack_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            backup_dest = os.path.join(backup_dir, backup_name)
            try:
                shutil.copy2(CONFIG['KITTYHACK_DATABASE_PATH'], backup_dest)
                logging.info(f"[UPLOAD_DB] Backup created: {backup_dest}")
            except Exception as e:
                logging.warning(f"[UPLOAD_DB] Failed to create backup: {e}")
            # Overwrite DB with uploaded file
            shutil.copy2(src_path, CONFIG['KITTYHACK_DATABASE_PATH'])
            ui.notification_show(_("Database restored successfully."), duration=6, type="message")
            # Prompt reboot to apply
            logging.info("Database restore performed --> Restart pending.")
            m = ui.modal(
                ui.markdown(_("Please click the 'Reboot' button to restart the Kittyflap.")),
                title=_("Reboot required"),
                easy_close=False,
                footer=ui.div(
                    ui.input_action_button("btn_modal_reboot_ok", _("Reboot")),
                )
            )
            ui.modal_show(m)
        except Exception as e:
            logging.error(f"[RESTORE_DB] Failed to restore database: {e}")
            ui.notification_show(_("Failed to restore database: {}").format(e), duration=12, type="error")

    @reactive.Effect
    @reactive.event(input.upload_config)
    def on_upload_config():
        files = input.upload_config()
        if not files:
            ui.notification_show(_("No file selected."), duration=8, type="error")
            last_uploaded_cfg_path.set(None)
            return
        f = files[0]
        name = f.get('name', '')
        src_path = f.get('datapath', '')
        if not src_path or not os.path.exists(src_path):
            ui.notification_show(_("Uploaded file not found."), duration=8, type="error")
            last_uploaded_cfg_path.set(None)
            return
        if not name.lower().endswith(".ini"):
            ui.notification_show(_("Invalid file type. Please upload a config.ini file."), duration=10, type="error")
            last_uploaded_cfg_path.set(None)
            return
        # Optional quick validation
        try:
            with open(src_path, 'r', encoding='utf-8', errors='ignore') as fcfg:
                content = fcfg.read()
            if "[Settings]" not in content:
                ui.notification_show(_("Invalid configuration file: missing [Settings] section."), duration=12, type="error")
                last_uploaded_cfg_path.set(None)
                return
        except Exception as e:
            ui.notification_show(_("Failed to validate configuration: {}").format(e), duration=10, type="error")
            last_uploaded_cfg_path.set(None)
            return
        # Only store path and inform user; do not restore yet
        last_uploaded_cfg_path.set(src_path)
        ui.notification_show(_("Configuration file uploaded. Click 'Restore Configuration' to apply."), duration=8, type="message")

    @reactive.Effect
    @reactive.event(input.restore_config)
    def on_restore_config():
        src_path = last_uploaded_cfg_path.get()
        if not src_path or not os.path.exists(src_path):
            ui.notification_show(_("No uploaded configuration ready to restore."), duration=8, type="error")
            return
        try:
            config_path = os.path.join(os.getcwd(), "config.ini")
            shutil.copy2(src_path, config_path)
            ui.notification_show(_("Configuration restored successfully."), duration=6, type="message")
            logging.info("Configuration restore performed --> Restart pending.")
            m = ui.modal(
                ui.markdown(_("Please click the 'Reboot' button to restart the Kittyflap.")),
                title=_("Reboot required"),
                easy_close=False,
                footer=ui.div(
                    ui.input_action_button("btn_modal_reboot_ok", _("Reboot")),
                )
            )
            ui.modal_show(m)
        except Exception as e:
            logging.error(f"[RESTORE_CFG] Failed to restore configuration: {e}")
            ui.notification_show(_("Failed to restore configuration: {}").format(e), duration=12, type="error")

    def _start_update_process(update_target: bool, update_local: bool):
        latest_version = CONFIG['LATEST_VERSION']
        current_version = git_version

        if not git_repo_available:
            ui.notification_show(
                _("Automatic update requires a git repository. Please reinstall via git clone or the setup script."),
                duration=12,
                type="error"
            )
            return

        if is_remote_mode() and not (update_target or update_local):
            ui.notification_show(_("Nothing to update."), duration=8, type="warning")
            return

        if (not is_remote_mode()) and update_target:
            ui.notification_show(_("Target-only update is only available in remote-mode."), duration=8, type="error")
            return

        if is_remote_mode() and update_target and update_local:
            initial_max_steps = 9
        elif update_local:
            initial_max_steps = 8
        else:
            initial_max_steps = 1

        set_update_progress(
            in_progress=True,
            step=1,
            max_steps=initial_max_steps,
            message=_("Starting update..."),
            detail="",
            result=None,
            error_msg="",
        )

        # Start the update in a background thread so the UI can update immediately
        def run_update():
            remote_mode_active = bool(is_remote_mode())

            def _set_error_and_stop(message: str):
                logging.error(f"Kittyhack update failed: {message}")
                set_update_progress(result="error", in_progress=False, error_msg=message)

            if remote_mode_active and update_target:
                try:
                    from src.remote.control_client import RemoteControlClient

                    set_update_progress(
                        in_progress=True,
                        step=1,
                        max_steps=(9 if update_local else 1),
                        message=_("Updating connected target device..."),
                        detail=_("Preparing remote update..."),
                        result=None,
                        error_msg="",
                    )

                    client = RemoteControlClient.instance()
                    client.ensure_started()
                    if not client.wait_until_ready(timeout=20.0):
                        _set_error_and_stop(_("Remote target is not connected."))
                        return

                    client.start_target_update(latest_version=latest_version, current_version=current_version)
                    target_update_started_at = monotonic_time()
                    reconnect_grace_s = 120.0
                    had_in_progress_state = False

                    while True:
                        if (monotonic_time() - float(target_update_started_at or 0.0)) > 3600.0:
                            _set_error_and_stop(_("Timed out while waiting for target device update."))
                            return

                        status = client.get_target_update_status()
                        if bool(status.get("in_progress")):
                            had_in_progress_state = True

                        if status.get("ok") is False:
                            reason = str(status.get("reason") or _("Unknown target update error."))
                            _set_error_and_stop(_("Target device update failed: {}.").format(reason))
                            return
                        if status.get("ok") is True:
                            set_update_progress(
                                in_progress=True,
                                step=1,
                                max_steps=(9 if update_local else 1),
                                message=_("Connected target device updated."),
                                detail=(_("Starting update on this device...") if update_local else _("Update finished.")),
                                result=None,
                                error_msg="",
                            )
                            break

                        if not client.wait_until_ready(timeout=0):
                            elapsed = monotonic_time() - float(target_update_started_at or 0.0)
                            if had_in_progress_state and elapsed >= 5.0:
                                set_update_progress(
                                    in_progress=True,
                                    step=1,
                                    max_steps=(9 if update_local else 1),
                                    message=_("Updating connected target device..."),
                                    detail=_("Connection lost while target is restarting/reconnecting. Waiting..."),
                                    result=None,
                                    error_msg="",
                                )
                                if elapsed >= reconnect_grace_s:
                                    logging.warning("[UPDATE] Target update connection did not return within grace period; continuing without hard failure.")
                                    break
                                tm.sleep(1.0)
                                continue

                            _set_error_and_stop(_("Connection to target device was lost during update."))
                            return

                        if status.get("in_progress"):
                            detail = _("Target update is running...")
                        elif status.get("requested"):
                            detail = _("Waiting for target update to start...")
                        else:
                            detail = _("Waiting for remote target response...")

                        set_update_progress(
                            in_progress=True,
                            step=1,
                            max_steps=(9 if update_local else 1),
                            message=_("Updating connected target device..."),
                            detail=detail,
                            result=None,
                            error_msg="",
                        )
                        tm.sleep(1.0)
                except Exception as e:
                    _set_error_and_stop(_("Failed to run target update: {}.").format(e))
                    return

            if not update_local:
                set_update_progress(result="ok", in_progress=False)
                return

            def progress_callback(step, message, detail):
                mapped_step = int(step)
                mapped_max_steps = 8
                if remote_mode_active and update_target:
                    mapped_step = max(1, min(9, int(step) + 1))
                    mapped_max_steps = 9
                set_update_progress(
                    in_progress=True,
                    step=mapped_step,
                    max_steps=mapped_max_steps,
                    message=message,
                    detail=detail,
                    result=None,
                    error_msg="",
                )
            ok, msg = update_kittyhack(
                progress_callback=progress_callback,
                latest_version=latest_version,
                current_version=current_version
            )
            if ok:
                logging.info(f"Kittyhack updated successfully to version {latest_version}.")
                set_update_progress(result="ok")
            else:
                logging.error(f"Kittyhack update failed: {msg}")
                set_update_progress(result="error", error_msg=msg)

        threading.Thread(target=run_update, daemon=True).start()

        # Add some delay here to keep the action button active for a moment until the modal is shown
        tm.sleep(1.0)

    @reactive.Effect
    @reactive.event(input.update_kittyhack)
    def update_kittyhack_process():
        # In remote-mode: target first, then local device.
        # In target/local mode: local device only.
        _start_update_process(update_target=bool(is_remote_mode()), update_local=True)

    @reactive.Effect
    @reactive.event(input.update_target_kittyhack)
    def update_target_kittyhack_process():
        _start_update_process(update_target=True, update_local=False)

    @reactive.Effect
    @reactive.event(input.update_remote_kittyhack)
    def update_remote_kittyhack_process():
        _start_update_process(update_target=False, update_local=True)

    @output
    @render.text
    def update_progress_message():
        reactive.invalidate_later(0.5)
        state = get_update_progress()
        return state["message"]

    @output
    @render.text
    def update_progress_detail():
        reactive.invalidate_later(0.5)
        state = get_update_progress()
        return state["detail"]

    def show_update_progress_modal():
        state = get_update_progress()
        if not state["in_progress"] and state["result"] is None:
            ui.modal_remove()
            return

        ui.modal_show(
            ui.modal(
                ui.div(
                    ui.HTML("""
                        <div style="margin-bottom: 1em;">
                            <div style="display: flex; align-items: center; gap: 1em;">
                                <div class="spinner" style="display: inline-block; width: 16px; height: 16px; border: 2px solid #eee; border-top: 2px solid #007bff; border-radius: 50%; animation: spin 1s linear infinite;"></div>
                                <div style="font-weight: bold; display: flex;">
                    """),
                    ui.output_text("update_progress_message"),
                    ui.HTML("""
                                    <div id="in_progress_dots" style="margin-left: 0.2em; color: #888; font-size: 0.95em;"></div>
                                </div>
                            </div>
                            <div style="color: #888;">
                    """),
                    ui.output_text("update_progress_detail"),
                    ui.HTML("""
                            </div>
                            <div style="margin-top: 1em;">
                                <div style="background: #eee; border-radius: 4px; height: 24px; width: 100%; position: relative;">
                                    <div id="progress_bar" style="background: #007bff; height: 100%; border-radius: 4px; width: 0%; transition: width 0.3s;"></div>
                                    <div id="progress_percent_text" style="position: absolute; right: 8px; top: 0; height: 100%; display: flex; align-items: center; color: #555; font-size: 0.9em;">
                    """),
                    ui.output_text("update_progress_percent"),
                    ui.HTML("""
                                    </div>
                                </div>
                            </div>
                            <style>
                            @keyframes spin {
                                0% { transform: rotate(0deg);}
                                100% { transform: rotate(360deg);}
                            }
                            </style>
                        </div>
                        <br>
                    """),
                    ui.markdown(_("Do not close this page until the update is finished!")),
                    ui.markdown(_("*This step may take several minutes. Please be patient.*")),
                ),
                title=_("Updating Kittyhack..."),
                easy_close=False,
                footer=None,
                id="update_progress_modal",
            )
        )

    @output
    @render.text
    def update_progress_percent():
        reactive.invalidate_later(0.5)
        state = get_update_progress()
        max_steps = int(state.get("max_steps") or 0)
        step = int(state.get("step") or 0)
        if max_steps <= 0:
            percent = 0
        else:
            percent = int(round((step / max_steps) * 100))
            percent = max(0, min(100, percent))
        return f"{percent}%"

    # Reactive effect to show/update the modal in all sessions
    @reactive.Effect
    async def update_progress_watcher():
        reactive.invalidate_later(1)
        state = get_update_progress()

        # Track only the modal open/close state
        if not hasattr(update_progress_watcher, "modal_open"):
            update_progress_watcher.modal_open = False
        if not hasattr(update_progress_watcher, "reboot_dialog_shown"):
            update_progress_watcher.reboot_dialog_shown = False

        # Show update progress modal only when starting update
        if state["in_progress"] and not update_progress_watcher.modal_open:
            show_update_progress_modal()
            update_progress_watcher.modal_open = True
            update_progress_watcher.reboot_dialog_shown = False

        # Remove modal and show result only when update is finished
        elif update_progress_watcher.modal_open and not state["in_progress"] and state["result"] is None:
            ui.modal_remove()
            update_progress_watcher.modal_open = False
            update_progress_watcher.reboot_dialog_shown = False

        # Show reboot dialog if update finished and reboot required, but only once
        elif (
            (state["result"] == "ok" or state["result"] == "reboot_dialog")
            and not update_progress_watcher.reboot_dialog_shown
        ):
            # Always remove any open modal first
            ui.modal_remove()
            set_update_progress(result="reboot_dialog", in_progress=False)
            ui.modal_show(
                ui.modal(
                    _("A restart is required to apply the update. Please click the 'Reboot' button to restart the Kittyflap."),
                    title=_("Restart required"),
                    easy_close=False,
                    footer=ui.div(
                        ui.input_action_button("btn_modal_reboot_ok", _("Reboot")),
                    )
                )
            )
            update_progress_watcher.modal_open = True
            update_progress_watcher.reboot_dialog_shown = True

        # Show reboot dialog with error if update failed, but only once
        elif (
            state["result"] == "error"
            and not update_progress_watcher.reboot_dialog_shown
        ):
            ui.modal_remove()
            set_update_progress(result="reboot_dialog", in_progress=False)
            ui.modal_show(
                ui.modal(
                    ui.div(
                        ui.markdown(_("An error occurred during the update process:")),
                        ui.markdown(f"```\n{state['error_msg']}\n```"),
                        ui.br(),
                        ui.markdown(_("A restart is required to recover. Please click the 'Reboot' button to restart the Kittyflap.")),
                    ),
                    title=_("Restart required"),
                    easy_close=False,
                    footer=ui.div(
                        ui.input_action_button("btn_modal_reboot_ok", _("Reboot")),
                    )
                )
            )
            update_progress_watcher.modal_open = True
            update_progress_watcher.reboot_dialog_shown = True

        # If modal is open but result is not reboot or update, close it
        elif update_progress_watcher.modal_open and state["result"] not in ("ok", "reboot_dialog", "error") and not state["in_progress"]:
            ui.modal_remove()
            update_progress_watcher.modal_open = False
            update_progress_watcher.reboot_dialog_shown = False
