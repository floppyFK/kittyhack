import os
import pandas as pd
from datetime import datetime, timedelta
import time as tm
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
import io
import zipfile
import uuid
from typing import List
from src.baseconfig import (
    CONFIG,
    AllowedToEnter,
    set_language,
    save_config,
    configure_logging,
    get_loggable_config_value,
    DEFAULT_CONFIG,
    UserNotifications
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
    is_gateway_reachable
)

# Prepare gettext for translations based on the configured language
_ = set_language(CONFIG['LANGUAGE'])

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
    logging.error("Not graceful shutdown detected 3 times in a row! We will disable the 'use all cores' setting, if it was enabled.")
    CONFIG['USE_ALL_CORES_FOR_IMAGE_PROCESSING'] = False
    CONFIG['NOT_GRACEFUL_SHUTDOWNS'] = 0
    update_single_config_parameter("USE_ALL_CORES_FOR_IMAGE_PROCESSING")
    update_single_config_parameter("NOT_GRACEFUL_SHUTDOWNS")

    # Add a entry to the user notifications, which will be shown at the next login in the frontend
    UserNotifications.add(
        header=_("⚠️ Several crashes detected!"),
        message=_("The kittyflap was not shut down gracefully several times in a row. Please do not power off the device without shutting it down first, otherwise the database may be corrupted!") + "\n\n" +
                _("If you have shut it down gracefully and see this message, please report it in the") + " " +
                "[GitHub issue tracker](https://github.com/floppyFK/kittyhack/issues), " + 
                _("thanks!") + "\n\n" +
                _("> **NOTE:** The option `Use all CPU cores for image processing` has been disabled now automatically, since this could cause the issue on some devices.") + "\n" +
                _("Please check the settings and enable it again, if you want to use it."),
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
from src.backend import backend_main, restart_mqtt, update_mqtt_config, update_mqtt_language, manual_door_override, model_handler
from src.magnets_rfid import Magnets
from src.pir import Pir
from src.model import RemoteModelTrainer, YoloModel
from src.shiny_wrappers import uix

# Read the GIT version
git_version = get_git_version()

# MIGRATION RULES ##################################################################################################

last_booted_version = CONFIG['LAST_BOOTED_VERSION']
# Check if we need to update the USE_ALL_CORES_FOR_IMAGE_PROCESSING setting
# This is only needed once after updating to version 1.5.2 or higher
if normalize_version(last_booted_version) < '1.5.2' and normalize_version(git_version) >= '1.5.2':
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
check_and_stop_kittyflap_services(CONFIG['SIMULATE_KITTYFLAP'])

# Cleanup old temp files
if os.path.exists("/tmp/kittyhack.db"):
    try:
        os.remove("/tmp/kittyhack.db")
    except:
        logging.error("Failed to delete the temporary kittyhack.db file.")

# Initial database integrity check
if os.path.exists(CONFIG['KITTYHACK_DATABASE_PATH']):
    db_check = check_database_integrity(CONFIG['KITTYHACK_DATABASE_PATH'])
    if db_check.success:
        logging.info("Initial Database integrity check successful.")
    else:
        logging.error(f"Initial Database integrity check failed: {db_check.message}")
        if os.path.exists(CONFIG['KITTYHACK_DATABASE_BACKUP_PATH']):
            restore_database_backup(database=CONFIG['KITTYHACK_DATABASE_PATH'], backup_path=CONFIG['KITTYHACK_DATABASE_BACKUP_PATH'])
        else:
            logging.error("No backup found to restore the database from. Deleting the database file...")
            try:
                os.remove(CONFIG['KITTYHACK_DATABASE_PATH'])
            except:
                logging.error("Failed to delete the kittyhack database file.")
            else:
                logging.info("Kittyhack Database file deleted.")
else:
    logging.warning(f"Database '{CONFIG['KITTYHACK_DATABASE_PATH']}' not found. This is probably the first start of the application.")
    CONFIG['LAST_READ_CHANGELOGS'] = git_version
    update_single_config_parameter("LAST_READ_CHANGELOGS")

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
if not check_if_column_exists(CONFIG['KITTYHACK_DATABASE_BACKUP_PATH'], "events", "thumbnail"):
    logging.warning(f"Column 'thumbnail' not found in the 'events' table of the backup database. Adding it...")
    add_column_to_table(CONFIG['KITTYHACK_DATABASE_BACKUP_PATH'], "events", "thumbnail", "BLOB")

# v2.0.0: Check if the "own_cat_probability" column exists in the "events" table. If not, add it
if not check_if_column_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "own_cat_probability"):
    logging.warning(f"Column 'own_cat_probability' not found in the 'events' table. Adding it...")
    add_column_to_table(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "own_cat_probability", "REAL")
if not check_if_column_exists(CONFIG['KITTYHACK_DATABASE_BACKUP_PATH'], "events", "own_cat_probability"):
    logging.warning(f"Column 'own_cat_probability' not found in the 'events' table of the backup database. Adding it...")
    add_column_to_table(CONFIG['KITTYHACK_DATABASE_BACKUP_PATH'], "events", "own_cat_probability", "REAL")

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
for db in [CONFIG['KITTYHACK_DATABASE_PATH'], CONFIG['KITTYHACK_DATABASE_BACKUP_PATH']]:
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
    logging.info("Starting backend...")
else:
    logging.warning("Timeout for network connectivity reached. Proceeding without network connection.")
    logging.info("Starting backend...")
backend_thread = threading.Thread(target=backend_main, args=(CONFIG['SIMULATE_KITTYFLAP'],), daemon=True)
backend_thread.start()

# Log the relevant installed deb packages
log_relevant_deb_packages()

# Set the WLAN TX Power level
logging.info(f"Setting WLAN TX Power to {CONFIG['WLAN_TX_POWER']} dBm...")
systemcmd(["iwconfig", "wlan0", "txpower", f"{CONFIG['WLAN_TX_POWER']}"], CONFIG['SIMULATE_KITTYFLAP'])
logging.info("Disabling WiFi power saving mode...")
systemcmd(["iw", "dev", "wlan0", "set", "power_save", "off"], CONFIG['SIMULATE_KITTYFLAP'])

logging.info("Starting frontend...")

# Check if the label-studio version is installed
CONFIG["LABELSTUDIO_VERSION"] = get_labelstudio_installed_version()

# Global for the free disk space:
free_disk_space = get_free_disk_space()

# Global flag to indicate if a user WLAN action is in progress
user_wlan_action_in_progress = False

# Frontend background task in a separate thread
def start_background_task():
    # Register task in the sigterm_monitor object
    sigterm_monitor.register_task()

    def run_periodically():
        wlan_disconnect_counter = 0
        wlan_reconnect_attempted = False
        last_periodic_jobs_run = tm.time()

        while not sigterm_monitor.stop_now:
            if user_wlan_action_in_progress:
                # Skip automatic reconnect/reboot while user action is in progress
                logging.info("[WLAN CHECK] User WLAN action in progress, skipping automatic reconnect/reboot checks.")
                wlan_disconnect_counter = 0
                wlan_reconnect_attempted = False
                for __ in range(5):
                    if sigterm_monitor.stop_now:
                        break
                    tm.sleep(1.0)
                continue

            # --- WLAN connection check every 5 seconds ---
            # Check WLAN connection state
            try:
                wlan_connections = get_wlan_connections()
                wlan_connected = any(wlan['connected'] for wlan in wlan_connections)
                gateway_reachable = is_gateway_reachable()
            except Exception as e:
                logging.error(f"[WLAN CHECK] Failed to get WLAN connections or gateway state: {e}")
                wlan_connected = False
                gateway_reachable = False

            # Only perform reconnect/reboot if WLAN watchdog is enabled
            if CONFIG['WLAN_WATCHDOG_ENABLED']:
                # Consider WLAN as not connected if either the interface or gateway is not reachable
                if not wlan_connected or not gateway_reachable:
                    wlan_disconnect_counter += 1
                    if wlan_disconnect_counter <= 5:
                        logging.warning(
                            f"[WLAN CHECK] WLAN not fully connected (attempt {wlan_disconnect_counter}/5): "
                            f"Interface connected: {wlan_connected}, Gateway reachable: {gateway_reachable}"
                        )
                    elif wlan_disconnect_counter <= 8:
                        logging.error(
                            f"[WLAN CHECK] WLAN still not connected after proactive reconnect attempt (attempt {wlan_disconnect_counter}/8)! "
                            f"LAST CHANCES! REBOOTING SYSTEM IF NOT RECONNECTED WITHIN 8 ATTEMPTS!"
                            f"Interface connected: {wlan_connected}, Gateway reachable: {gateway_reachable}"
                        )
                else:
                    wlan_disconnect_counter = 0
                    wlan_reconnect_attempted = False

                # If disconnected for 5 consecutive checks (25 seconds), try to reconnect
                if wlan_disconnect_counter == 5 and not wlan_reconnect_attempted:
                    logging.warning("[WLAN CHECK] Attempting to reconnect WLAN after 5 failed checks...")
                    # Sort configured WLANs by priority (highest first) and limit to max 6
                    sorted_wlans = sorted(wlan_connections, key=lambda w: w.get('priority', 0), reverse=True)[:6]
                    for wlan in sorted_wlans:
                        ssid = wlan['ssid']
                        systemctl("stop", f"NetworkManager")
                        tm.sleep(2)
                        systemctl("start", f"NetworkManager")
                        tm.sleep(2)
                        switch_wlan_connection(ssid)
                        # Wait up to 10 seconds for connection
                        for __ in range(10):
                            tm.sleep(1)
                            wlan_connections = get_wlan_connections()
                            if any(w['connected'] for w in wlan_connections) and is_gateway_reachable():
                                logging.info(f"[WLAN CHECK] Successfully reconnected to SSID: {ssid}")
                                break
                    wlan_reconnect_attempted = True

                # If still disconnected after 3 more checks (total 8, 40 seconds), reboot
                if wlan_disconnect_counter >= 8:
                    logging.error("[WLAN CHECK] WLAN still not connected after reconnect attempts. Rebooting system...")
                    systemcmd(["/sbin/reboot"], CONFIG['SIMULATE_KITTYFLAP'])
                    break
            else:
                # If watchdog is disabled, just reset counters if connected, but do nothing on disconnect
                if wlan_connected and gateway_reachable:
                    wlan_disconnect_counter = 0
                    wlan_reconnect_attempted = False

            # --- Main periodic jobs (every PERIODIC_JOBS_INTERVAL seconds) ---
            # Periodically check that the kwork and manager services are NOT running anymore
            now = tm.time()
            if now - last_periodic_jobs_run >= CONFIG['PERIODIC_JOBS_INTERVAL']:
                last_periodic_jobs_run = now
                check_and_stop_kittyflap_services(CONFIG['SIMULATE_KITTYFLAP'])
                immediate_bg_task("background task")

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
                    logging.info(f"[TRIGGER: background task] It is {current_time.hour}:{current_time.minute}:{current_time.second}. Start backup of the kittyhack database...")
                    db_backup = backup_database(database=CONFIG['KITTYHACK_DATABASE_PATH'], backup_path=CONFIG['KITTYHACK_DATABASE_BACKUP_PATH'])
                    if db_backup and db_backup.message == "kittyhack_db_corrupted":
                        restore_database_backup(database=CONFIG['KITTYHACK_DATABASE_PATH'], backup_path=CONFIG['KITTYHACK_DATABASE_BACKUP_PATH'])

                # Perform Scheduled VACUUM only if the last scheduled vacuum date is older than 24 hours
                if (datetime.now() - last_vacuum_date) > timedelta(days=1):
                    logging.info("[TRIGGER: background task] Start VACUUM of the kittyhack database...")
                    vacuum_database(CONFIG['KITTYHACK_DATABASE_PATH'])
                    CONFIG['LAST_VACUUM_DATE'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    update_single_config_parameter("LAST_VACUUM_DATE")

                # Log system information
                log_system_information()

            # Sleep 5 seconds before next WLAN check
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
start_background_task()

# Global reactive triggers
reload_trigger_wlan = reactive.Value(0)
reload_trigger_photos = reactive.Value(0)
reload_trigger_ai = reactive.Value(0)
reload_trigger_config = reactive.Value(0)

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
    return ui.input_action_button(id=f"btn_show_event" , label="", icon=icon_svg("magnifying-glass", margin_left="-1", margin_right="auto"), class_="btn-narrow btn-vertical-margin", style_="width: 42px;")

@module.ui
def btn_yolo_modify():
    return ui.input_action_button(id=f"btn_yolo_modify" , label="", icon=icon_svg("pencil", margin_left="-0.1em", margin_right="auto"), class_="btn-narrow btn-vertical-margin", style_="width: 42px;")

@module.server
def show_event_server(input, output, session, block_id: int):

    # Use standard Python list to store pictures and current frame index
    pictures = []
    orig_pictures = []
    timestamps = []
    # Store lists of DetectedObjects, one list per event
    event_datas: List[List[DetectedObject]] = []
    frame_index = [0]  # Use list to allow modification in nested functions
    fallback_mode = [False]
    slideshow_running = [True]

    @render.ui
    @reactive.effect
    @reactive.event(input.btn_show_event)
    async def show_event():
        logging.info(f"Show event with block_id {block_id}")
        picture_type = ReturnDataPhotosDB.all_original_image
        blob_picture = "original_image"

        # FALLBACK: The event_text column was added in version 1.4.0. If it is not present, show the "modified_image" with baked-in event data
        event = db_get_photos_by_block_id(CONFIG['KITTYHACK_DATABASE_PATH'], block_id, ReturnDataPhotosDB.all_except_photos)
        if not event.iloc[0]["event_text"]:
            fallback_mode[0] = True
            if CONFIG['SHOW_IMAGES_WITH_OVERLAY']:
                blob_picture = "modified_image"
                picture_type = ReturnDataPhotosDB.all_modified_image


        event = db_get_photos_by_block_id(CONFIG['KITTYHACK_DATABASE_PATH'], block_id, picture_type)
        
        # Clear the pictures list
        pictures.clear()
        orig_pictures.clear()
        timestamps.clear()
        event_datas.clear()

        # Iterate over the rows and encode the pictures
        async def process_event_row(row):
            try:
                event_text = row['event_text']
                
                # Handle thumbnail data correctly
                if row['thumbnail'] is not None:
                    # If thumbnail already exists in the row, use it directly
                    thumbnail_bytes = row['thumbnail']
                else:
                    # Generate thumbnail and store it in the database
                    thumbnail_bytes = get_thubmnail_by_id(database=CONFIG['KITTYHACK_DATABASE_PATH'], photo_id=row['id'])
                
                if thumbnail_bytes:
                    # Ensure we're encoding bytes, not a string
                    if isinstance(thumbnail_bytes, str):
                        try:
                            # Convert string to bytes if needed (in case it's a base64 string)
                            thumbnail_bytes = base64.b64decode(thumbnail_bytes)
                        except:
                            logging.error(f"Failed to decode thumbnail string for photo ID {row['id']}")
                            thumbnail_bytes = None
                    
                    if thumbnail_bytes:
                        # Encode the bytes to base64 and then to string for HTML
                        pictures.append(base64.b64encode(thumbnail_bytes).decode('utf-8'))
                    orig_pictures.append(row[blob_picture])
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

        # Sort the pictures, timestamps, event_datas, and orig_pictures lists by the timestamps
        sorted_data = sorted(zip(timestamps, pictures, event_datas, orig_pictures), key=lambda x: x[0])
        timestamps[:], pictures[:], event_datas[:], orig_pictures[:] = zip(*sorted_data)

        if int(CONFIG['SHOW_IMAGES_WITH_OVERLAY']):
            overlay_icon = icon_svg_local('prey-frame-on', margin_left="-0.1em")
        else:
            overlay_icon = icon_svg_local('prey-frame-off', margin_left="-0.1em")

        ui.modal_show(
            ui.modal(
                ui.card(
                    ui.output_ui("show_event_picture"),
                    ui.card_footer(
                        ui.div(
                            # left button group
                            ui.div(
                                ui.tooltip(
                                    ui.input_action_button(id="btn_delete_event", label="", icon=icon_svg("trash"), 
                                                        class_="btn-vertical-margin btn-narrow btn-danger", 
                                                        style_="width: 42px;"),
                                    _("Delete all pictures of this event"),
                                    id="tooltip_delete_event",
                                ),
                                style_="padding-top: 6px;",
                                class_="d-flex justify-content-start flex-shrink-0"
                            ),

                            # center button group with right alignment
                            ui.div(
                                ui.div(
                                    ui.input_action_button(id="btn_prev", label="", icon=icon_svg("chevron-left"), 
                                                        class_="btn-narrow", style_="width: 42px;"),
                                    ui.input_action_button(id="btn_play_pause", label="", icon=icon_svg("pause"), 
                                                        class_="btn-narrow", style_="width: 42px;"),
                                    ui.input_action_button(id="btn_next", label="", icon=icon_svg("chevron-right"), 
                                                        class_="btn-narrow", style_="width: 42px;"),
                                    style_="padding-left: 12px; padding-top: 6px;",
                                    class_="d-flex gap-1"
                                ),
                                class_="flex-grow-1 d-flex justify-content-end"
                            ),

                            # right button group
                            ui.div(
                                ui.download_button(id="btn_download", label="", icon=icon_svg("download", margin_left="-0.1em"),
                                                    class_="btn-vertical-margin btn-narrow", style_="width: 42px;"),
                                ui.input_action_button(id="btn_toggle_overlay", label="", icon=overlay_icon, 
                                                    class_="btn-vertical-margin btn-narrow", 
                                                    style_=f"width: 42px; opacity: 0.5;" if fallback_mode[0] else "width: 42px;", 
                                                    disabled=fallback_mode[0]),
                                ui.input_action_button(id="btn_modal_cancel", label="", icon=icon_svg("xmark"), 
                                                    class_="btn-vertical-margin btn-narrow", style_="width: 42px;"),
                                style_="padding-left: 12px; padding-top: 6px;",
                                class_="d-flex gap-1 justify-content-end flex-shrink-0 ms-auto"
                            ),

                            class_="d-flex flex-wrap align-items-center justify-content-center w-100 position-relative"
                        ),
                    ),
                    full_screen=False,
                    class_="image-container"
                ),
                footer=ui.div(
                    ui.input_action_button("modal_pulse", "", style_="visibility:hidden; width:1px; height:1px;"),
                    ui.HTML("""
                        <script>
                        (function() {
                            function setupPulseObserver() {
                                var modal = document.querySelector('.modal');
                                var btn = document.querySelector('button[id$="modal_pulse"]');
                                if (!modal || !btn) {
                                    // Try again in 100ms
                                    setTimeout(setupPulseObserver, 100);
                                    return;
                                }
                                var observer = new MutationObserver(function(mutations) {
                                    mutations.forEach(function(mutation) {
                                        if (
                                            mutation.type === "attributes" &&
                                            mutation.attributeName === "class" &&
                                            mutation.target.classList.contains("modal-static") &&
                                            (!mutation.oldValue || !mutation.oldValue.includes("modal-static"))
                                        ) {
                                            btn.click();
                                            console.log('Shiny modal_pulse button clicked');
                                        }
                                    });
                                });
                                observer.observe(modal, { attributes: true, attributeOldValue: true });
                            }
                            setupPulseObserver();
                        })();
                        </script>
                    """)
                ),
                size='l',
                easy_close=False,
                class_="transparent-modal-content"
            )
        )
    
    @render.text
    def show_event_picture():
        reactive.invalidate_later(0.2)
        try:
            if len(pictures) > 0:
                frame = pictures[frame_index[0]]
                # Get the detection areas from the current frame's EventSchema object
                detected_objects = event_datas[frame_index[0]]
                if frame is not None:
                    # Start the HTML for the container and image
                    img_html = f'''
                    <div style="position: relative; display: inline-block;">
                        <img src="data:image/jpeg;base64,{frame}" style="min-width: 250px;" />'''

                    # Iterate over the detected objects and draw bounding boxes
                    if input.btn_toggle_overlay() % 2 == (1 - int(CONFIG['SHOW_IMAGES_WITH_OVERLAY'])):
                        for detected_object in detected_objects:
                            if detected_object.object_name != "false-accept":
                                img_html += f'''
                                <div style="position: absolute; 
                                            left: {detected_object.x}%; 
                                            top: {detected_object.y}%; 
                                            width: {detected_object.width}%; 
                                            height: {detected_object.height}%; 
                                            border: 2px solid #ff0000; 
                                            background-color: rgba(255, 0, 0, 0.05);
                                            pointer-events: none;">
                                    <div style="position: absolute; 
                                                {f'bottom: -26px' if detected_object.y < 16 else 'top: -26px'}; 
                                                left: 0px; 
                                                background-color: rgba(255, 0, 0, 0.7); 
                                                color: white; 
                                                padding: 2px 5px;
                                                border-radius: 5px;
                                                text-wrap-mode: nowrap;
                                                font-size: 12px;">
                                        {detected_object.object_name} ({detected_object.probability:.0f}%)
                                    </div>
                                </div>'''

                    # Add the timestamp and frame counter overlays
                    # Remove the date part and the milliseconds from the timestamp
                    timestamp_display = timestamps[frame_index[0]][11:-4]
                    img_html += f'''
                        <div style="position: absolute; top: 12px; left: 50%; transform: translateX(-50%); background-color: rgba(0, 0, 0, 0.5); color: white; padding: 2px 5px; border-radius: 3px;">
                            {timestamp_display}
                        </div>
                        <div style="position: absolute; bottom: 12px; right: 8px; background-color: rgba(0, 0, 0, 0.5); color: white; padding: 2px 5px; border-radius: 3px;">
                            {frame_index[0] + 1}/{len(pictures)}
                        </div>
                    </div>
                    '''
                else:
                    img_html = '<div class="placeholder-image"><strong>' + _('No picture found!') + '</strong></div>'
            else:
                img_html = '<div class="placeholder-image"><strong>' + _('No pictures found for this event.') + '</strong></div>'
        except Exception as e:
            logging.error(f"Failed to show the picture for event: {e}")
            img_html = '<div class="placeholder-image"><strong>' + _('An error occured while reading the image.') + '</strong></div>'
        
        # Update frame index using standard list, if the play/pause button is in play mode
        if slideshow_running[0] and len(pictures) > 0:
            frame_index[0] = (frame_index[0] + 1) % max(len(pictures), 1)
        return ui.HTML(img_html)

    @reactive.effect
    @reactive.event(input.btn_modal_cancel, input.modal_pulse)
    def modal_cancel():
        ui.modal_remove()
        # Clear pictures and timestamps lists and reset frame index
        pictures.clear()
        orig_pictures.clear()
        timestamps.clear()
        event_datas.clear()
        frame_index[0] = 0
    
    @reactive.effect
    @reactive.event(input.btn_delete_event)
    def delete_event():
        logging.info(f"Delete all pictures of event with block_id {block_id}")
        delete_photos_by_block_id(CONFIG['KITTYHACK_DATABASE_PATH'], block_id)
        reload_trigger_photos.set(reload_trigger_photos.get() + 1)
        ui.modal_remove()
        # Clear pictures and timestamps lists and reset frame index
        pictures.clear()
        orig_pictures.clear()
        timestamps.clear()
        frame_index[0] = 0

    @render.download(filename=f"kittyhack_event_{block_id}.zip")
    def btn_download():
        if slideshow_running[0]:
            slideshow_running[0] = False
        
        # Create a BytesIO object to store the zip file
        zip_buffer = io.BytesIO()
        try:
            # Create a ZipFile object
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                # Add each picture to the zip file
                for i, (timestamp, picture, _, _) in enumerate(zip(timestamps, orig_pictures, event_datas, pictures)):
                    # Create filename from timestamp
                    filename = f"{timestamp.replace(':', '-')}.jpg"
                    zip_file.writestr(filename, picture)
            
            # Reset buffer position to the beginning
            zip_buffer.seek(0)
            return zip_buffer
        except Exception as e:
            logging.error(f"Failed to create the zip file for event with block_id {block_id}: {e}")
            return None
    
    @reactive.effect
    @reactive.event(input.btn_play_pause)
    def play_pause():
        # Toggle play/pause based on the click count
        if slideshow_running[0] == False:
            slideshow_running[0] = True
            ui.update_action_button("btn_play_pause", label="", icon=icon_svg("pause", margin_right="auto"))
        else:
            slideshow_running[0] = False
            ui.update_action_button("btn_play_pause", label="", icon=icon_svg("play", margin_right="auto"))

    @reactive.effect
    @reactive.event(input.btn_toggle_overlay)
    def toggle_overlay():
        # Toggle the overlay visibility based on the click count
        if input.btn_toggle_overlay() % 2 == int(CONFIG['SHOW_IMAGES_WITH_OVERLAY']):
            ui.update_action_button("btn_toggle_overlay", label="", icon=icon_svg_local('prey-frame-off', margin_left="-0.1em"))
        else:
            ui.update_action_button("btn_toggle_overlay", label="", icon=icon_svg_local('prey-frame-on', margin_left="-0.1em"))

    @reactive.effect
    @reactive.event(input.btn_prev)
    def prev_picture():
        frame_index[0] = (frame_index[0] - 1) % max(len(pictures), 1)

    @reactive.effect
    @reactive.event(input.btn_next)
    def next_picture():
        frame_index[0] = (frame_index[0] + 1) % max(len(pictures), 1)

@module.server
def wlan_connect_server(input, output, session, ssid: str):
    @reactive.effect
    @reactive.event(input.btn_wlan_connect)
    def wlan_connect():
        global user_wlan_action_in_progress
        user_wlan_action_in_progress = True
        ui.modal_show(
            ui.modal(
                _("The WLAN connection will be interrupted now!"),
                ui.br(),
                _("Please wait a few seconds. If the page does not reload automatically within 30 seconds, please reload it manually."),
                title=_("Updating WLAN configuration..."),
                footer=None
            )
        )
        switch_wlan_connection(ssid)
        user_wlan_action_in_progress = False
        reload_trigger_wlan.set(reload_trigger_wlan.get() + 1)
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
        success = manage_and_switch_wlan(ssid, password, priority, password_changed)
        if success:
            ui.notification_show(_("WLAN configuration for {} updated successfully.").format(ssid), duration=5, type="message")
            reload_trigger_wlan.set(reload_trigger_wlan.get() + 1)
        else:
            ui.notification_show(_("Failed to update WLAN configuration for {}").format(ssid), duration=10, type="error")
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
                    class_=f"btn-vertical-margin btn-narrow btn-danger {'disabled-wrapper' if model_in_use else ''}"
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

#######################################################################
# The main server application
#######################################################################
def server(input, output, session):

    # Create reactive triggers
    reload_trigger_cats = reactive.Value(0)
    reload_trigger_info = reactive.Value(0)

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

    # Show changelogs, if the version was updated
    state = get_update_progress()
    if not (state.get("result") == "reboot_dialog" or state.get("in_progress") is True):
        changelog_text = get_changelogs(after_version=CONFIG['LAST_READ_CHANGELOGS'], language=CONFIG['LANGUAGE'])
        if changelog_text:
            ui.modal_show(
                ui.modal(
                    ui.div(
                        ui.markdown(
                            _("✅ Update to version `{}` was successful!").format(git_version)
                            + "\n\n---------\n\n"
                            + _("**NOTE**: You can find the changelogs in the `INFO` section.")
                            ),
                    ),
                    title=_("Update"),
                    easy_close=True,
                    size="m",
                    footer=ui.div(
                        ui.input_action_button("btn_modal_cancel", _("Close")),
                    )
                )
            )
            CONFIG['LAST_READ_CHANGELOGS'] = git_version
            update_single_config_parameter("LAST_READ_CHANGELOGS")
    
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

    # When a new WebGUI session is opened, check if a model training is in progress:
    RemoteModelTrainer.check_model_training_result(show_notification=True)

    # Show user notifications if there are any
    show_user_notifications()

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

    @render.text
    def live_view_header():
        reactive.invalidate_later(0.25)
        try:
            inside_lock_state = Magnets.instance.get_inside_state()
            outside_lock_state = Magnets.instance.get_outside_state()

            from src.backend import motion_state, motion_state_lock

            with motion_state_lock:
                outside_motion_state = motion_state["outside"]
                inside_motion_state = motion_state["inside"]

            if hasattr(backend_main, "prey_detection_tm"):
                delta_to_last_prey_detection = tm.time() - float(backend_main.prey_detection_tm)
                time_until_release = CONFIG['LOCK_DURATION_AFTER_PREY_DETECTION'] - delta_to_last_prey_detection
                forced_lock_due_prey = time_until_release > 0
            else:
                delta_to_last_prey_detection = tm.time()
                time_until_release = 0
                forced_lock_due_prey = False

            if inside_lock_state:
                ui.update_action_button("bManualOverride", label=_("Close inside now"), icon=icon_svg("lock"), disabled=False)
            else:
                ui.update_action_button("bManualOverride", label=_("Open inside now"), icon=icon_svg("lock-open"), disabled=False)

            inside_lock_icon = icon_svg('lock-open') if inside_lock_state else icon_svg('lock')
            outside_lock_icon = icon_svg('lock-open') if outside_lock_state else icon_svg('lock')
            outside_pir_state_icon = "🟢" if outside_motion_state else "⚫"
            inside_pir_state_icon = "🟢" if inside_motion_state else "⚫"
            ui_html = ui.HTML(_("<b>Locks:</b> Inside {} | Outside {}<br><b>Motion:</b> Inside {} | Outside {}").format(inside_lock_icon, outside_lock_icon, inside_pir_state_icon, outside_pir_state_icon))
            if forced_lock_due_prey:
                ui.update_action_button("bResetPreyCooldown", disabled=False)
                ui_html += ui.HTML(_("<br>Prey detected {0:.0f}s ago. Inside lock remains closed for {1:.0f}s.").format(delta_to_last_prey_detection, time_until_release))
            else:
                ui.update_action_button("bResetPreyCooldown", disabled=True)
        except:
            ui_html = ui.markdown(_("Failed to fetch the current status of the locks and motion sensors."))

        return ui.div(
            ui.HTML(f"{datetime.now(ZoneInfo(CONFIG['TIMEZONE'])).strftime('%H:%M:%S')}"),
            ui.br(),
            ui.br(),
            ui_html
        )
    
    @render.text
    def live_view_main():
        reactive.invalidate_later(CONFIG['LIVE_VIEW_REFRESH_INTERVAL'])
        # Static variables to track last frame and time
        if not hasattr(live_view_main, "last_frame_hash"):
            live_view_main.last_frame_hash = None
            live_view_main.last_change_time = tm.time()
            live_view_main.last_frame_jpg = None

        warning_html = ""
        # Check for IP camera resolution warning (moved here)
        if CONFIG.get("CAMERA_SOURCE") == "ip_camera":
            try:
                res = model_handler.get_camera_resolution()
                if res and isinstance(res, (tuple, list)) and len(res) == 2:
                    width, height = res
                    if width * height > 1280 * 720:
                        warning_html = ui.div(
                            ui.div(
                                "⚠️ " + _("Warning") + ": " +
                                _("Your IP camera resolution is higher than recommended (max. 1280x720).") + " " +
                                _("Current: {width}x{height}. This may have negative effects on performance.").format(width=width, height=height),
                                class_="generic-container warning-container",
                            ),
                            style_="text-align: center;"
                        )
            except Exception as e:
                logging.error(f"Failed to check IP camera resolution: {e}")

        try:
            frame = model_handler.get_camera_frame()
            if frame is None:
                if CONFIG.get("CAMERA_SOURCE") == "ip_camera":
                    img_html = (
                        '<div class="placeholder-image" style="padding-top: 20px; padding-bottom: 20px;">' +
                        '<div></div>' +
                        '<div><strong>' + _('Connection to the IP camera failed.') + '</strong></div>' +
                        '<div>' + _("Please check the stream URL and the network connection of your IP camera.") + '</div>' +
                        '<div class="spinner-container"><div class="spinner"></div></div>' +
                        '<div>' + _('If you have just changed the camera settings, please wait a few seconds for the camera to reconnect.') + '</div>' +
                        '<div>' + _('Current status: ') + (str(model_handler.get_camera_state()) if model_handler.get_camera_state() is not None else _('Unknown')) + '</div>' +                        '<div></div>' +
                        '</div>'
                    )
                else:
                    img_html = (
                        '<div class="placeholder-image" style="padding-top: 20px; padding-bottom: 20px;">' +
                        '<div></div>' +
                        '<div><strong>' + _('Connection to the camera failed.') + '</strong></div>' +
                        '<div>' + _("Please wait...") + '</div>' +
                        '<div class="spinner-container"><div class="spinner"></div></div>' +
                        '<div>' + _('If this message does not disappear within 30 seconds, please (re-)install the required camera drivers with the "Reinstall Camera Driver" button in the "System" section.') + '</div>' +
                        '<div></div>' +
                        '</div>'
                    )
            else:
                frame_jpg = model_handler.encode_jpg_image(frame)
                # Compute a hash of the frame to detect changes
                frame_hash = hashlib.md5(frame_jpg).hexdigest() if frame_jpg else None

                if frame_jpg:
                    # If frame changed, update last_change_time and last_frame_jpg
                    if frame_hash != live_view_main.last_frame_hash:
                        live_view_main.last_change_time = tm.time()
                        live_view_main.last_frame_hash = frame_hash
                        live_view_main.last_frame_jpg = frame_jpg

                    # If frame hasn't changed for >5s and using external IP camera, show spinner and warning
                    if (
                        tm.time() - live_view_main.last_change_time > 5
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
                        frame_b64 = base64.b64encode(live_view_main.last_frame_jpg).decode('utf-8')
                        img_html = f'<img src="data:image/jpeg;base64,{frame_b64}" />'
                else:
                    img_html = '<div class="placeholder-image"><strong>' + _('Could not read the picture from the camera.') + '</strong></div>'
        except Exception as e:
            logging.error(f"Failed to fetch the live view image: {e}")
            img_html = '<div class="placeholder-image"><strong>' + _('An error occured while fetching the live view image.') + '</strong></div>'
        
        if warning_html:
            return ui.HTML(f'''
                <div style="display: inline-block; text-align: center;">
                    {img_html}
                    {warning_html}
                </div>
            ''')
        else:
            return ui.HTML(img_html)

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
        ui_tabs = []
        date_start = format_date_minmax(input.date_selector(), True)
        date_end = format_date_minmax(input.date_selector(), False)
        timezone = ZoneInfo(CONFIG['TIMEZONE'])
        # Convert date_start and date_end to timezone-aware datetime strings in the UTC timezone
        date_start = datetime.strptime(date_start, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S%z')
        date_end = datetime.strptime(date_end, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S%z')
        df_photo_ids = db_get_photos(
            CONFIG['KITTYHACK_DATABASE_PATH'],
            ReturnDataPhotosDB.only_ids,
            date_start,
            date_end,
            input.button_cat_only(),
            input.button_mouse_only(),
            CONFIG['MOUSE_THRESHOLD']
        )

        try:
            data_elements_count = df_photo_ids.shape[0]
        except:
            data_elements_count = 0
        tabs_count = int(math.ceil(data_elements_count / CONFIG['ELEMENTS_PER_PAGE']))
        if tabs_count > 0:
            for i in range(tabs_count, 0, -1):
                ui_tabs.append(ui.nav_panel(f"{i}", "", value=f"{date_start}_{date_end}_{i}"))
        else:
            ui_tabs.append(ui.nav_panel("1", "", value="empty"))
        logging.debug(f"[WEBGUI] Pictures-Nav: Recalculating tabs: {tabs_count} tabs for {data_elements_count} elements")
        return ui.navset_tab(*ui_tabs, id="ui_photos_cards_tabs")

    @output
    @render.ui
    @reactive.event(input.button_events_view, ignore_none=True)
    def ui_photos_cards():
        ui_cards = []

        if input.ui_photos_cards_tabs() == "empty":
            logging.info("No pictures for the selected filter criteria found.")
            return ui.help_text(_("No pictures for the selected filter criteria found."), class_="no-images-found")
        
        selected_page = input.ui_photos_cards_tabs().split('_')
        logging.debug(f"[WEBGUI] Pictures: Fetching images for {selected_page}")

        date_start = selected_page[0]
        date_end = selected_page[1]
        page_index = int(selected_page[2])-1

        df_photos = db_get_photos(
            CONFIG['KITTYHACK_DATABASE_PATH'],
            ReturnDataPhotosDB.all,
            date_start,
            date_end,
            input.button_cat_only(),
            input.button_mouse_only(),
            CONFIG['MOUSE_THRESHOLD'],
            page_index,
            CONFIG['ELEMENTS_PER_PAGE']
        )

        # Get a dictionary mapping RFIDs to cat names
        cat_name_dict = get_cat_name_rfid_dict(CONFIG['KITTYHACK_DATABASE_PATH'])

        for index, data_row in df_photos.iterrows():
            # FALLBACK: The event_text column was added in version 1.4.0. If it is not present, show the "modified_image" with baked-in event data
            if not data_row["event_text"] and CONFIG['SHOW_IMAGES_WITH_OVERLAY']:
                blob_picture = "modified_image"
            else:
                blob_picture = "original_image"
            
            try:
                decoded_picture = base64.b64encode(data_row[blob_picture]).decode('utf-8')
            except:
                decoded_picture = None
            
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
            

            if decoded_picture:
                img_html = f'''
                <div style="position: relative; display: inline-block;">
                    <img src="data:image/jpeg;base64,{decoded_picture}" style="min-width: 250px;" />'''
                
                if input.button_detection_overlay():
                    for detected_object in detected_objects:
                        img_html += f'''
                        <div style="position: absolute; 
                                    left: {detected_object.x}%; 
                                    top: {detected_object.y}%; 
                                    width: {detected_object.width}%; 
                                    height: {detected_object.height}%; 
                                    border: 2px solid #ff0000; 
                                    background-color: rgba(255, 0, 0, 0.05);
                                    pointer-events: none;">
                            <div style="position: absolute; 
                                        {f'bottom: -26px' if detected_object.y < 16 else 'top: -26px'}; 
                                        left: 0px; 
                                        background-color: rgba(255, 0, 0, 0.7); 
                                        color: white; 
                                        padding: 2px 5px;
                                        border-radius: 5px;
                                        text-wrap-mode: nowrap;
                                        font-size: 12px;">
                                {detected_object.object_name} ({detected_object.probability:.0f}%)
                            </div>
                        </div>'''
                img_html += "</div>"
            else:
                img_html = '<div class="placeholder-image"><strong>' + _('No picture found!') + '</strong></div>'
                logging.warning(f"No blob_picture found for entry {photo_timestamp}")
            
            ui_cards.append(
                        ui.card(
                        ui.card_header(
                            ui.div(
                                ui.HTML(f"{photo_timestamp} | {data_row['id']}"),
                                ui.div(ui.input_checkbox(id=f"delete_photo_{data_row['id']}", label="", value=False), style_="float: right; width: 15px;"),
                            ),
                        ),
                        ui.HTML(img_html),
                        ui.card_footer(
                            ui.div(
                                ui.tooltip(ui.HTML(card_footer_mouse), _("Mouse probability")),
                                ui.HTML(card_footer_cat),
                            )
                        ),
                        full_screen=True,
                        class_="image-container" + (" image-container-alert" if mouse_probability >= CONFIG['MOUSE_THRESHOLD'] else "")
                    )
            )

        return ui.div(
            ui.layout_column_wrap(*ui_cards, width="400px"),
            ui.panel_absolute(
                ui.panel_well(
                    ui.input_action_button(id="delete_selected_photos", label=_("Delete selected photos"), icon=icon_svg("trash")),
                    style_="background: rgba(240, 240, 240, 0.9); text-align: center;"
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

        df_photos = db_get_photos(CONFIG['KITTYHACK_DATABASE_PATH'], ReturnDataPhotosDB.only_ids)

        for id in df_photos['id']:
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
            ui.card_header(
                ui.output_ui("live_view_header"),
            ),
            ui.output_ui("live_view_main"),
            full_screen=False,
            class_="image-container"
        )
        return ui.div(
            live_view,
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
                        id="tooltip_configure_per_cat_quick"
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
        inside_state = Magnets.instance.get_inside_state()
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
    @reactive.event(reload_trigger_photos, ignore_none=True)
    def ui_last_events_table():
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
            html = '<table class="dataframe shiny-table table w-auto">'
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
                html += f'<td><div>{btn_show_event(btn_id)}'
                html += f'<i class="fa fa-search" style="margin-left: -1px; margin-right: auto;"></i></button></div></td>'
                show_event_server(btn_id, row['block_id'])
                html += '</tr>'

            html += '</tbody></table>'
            html += '''
            <script>
            $(document).ready(function() {
            $('.tooltip-wrapper').tooltip();
            });
            </script>
            '''
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
                    ui.hr(),
                    ui.div(
                        ui.input_task_button("reinstall_camera_driver", _("Reinstall Camera Driver"), icon=icon_svg("rotate-right"), class_="btn-default"),
                        style_="text-align: center;"
                    ),
                    ui.help_text(_("Reinstall the camera driver if the live view does not work properly.")),
                    ui.br(),
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
        ui.modal_remove()
        ui.modal_show(ui.modal(_("Kittyflap is rebooting now... This will take 1 or 2 minutes. Please reload the page after the restart."), title=_("Restart Kittyflap"), footer=None))
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
        # Add JavaScript/CSS to highlight unsaved changes in Manage Cats section
        manage_cats_unsaved_js = ui.HTML("""
        <script>
        $(document).ready(function() {
          let hasUnsavedCats = false;
          let isInitializingCats = true;
          $('#mng_cat_save_changes').removeClass('save-button-highlight');
          $('#manage_cats_container .unsaved-input').removeClass('unsaved-input');
          function highlightCatInput(el) {
            if (!isInitializingCats) {
              $(el).addClass('unsaved-input');
              $('#mng_cat_save_changes').addClass('save-button-highlight');
              hasUnsavedCats = true;
            }
          }
          setTimeout(function() { isInitializingCats = false; }, 800);
            $('#manage_cats_container').on('change input', 'input, select, textarea', function() {
              highlightCatInput(this);
            });
          $(document).on('click', '#mng_cat_save_changes', function() {
            $('#manage_cats_container .unsaved-input').removeClass('unsaved-input');
            $('#mng_cat_save_changes').removeClass('save-button-highlight');
            hasUnsavedCats = false;
          });
        });
        </script>
        <style>
        .unsaved-input { border: 2px solid #ffc107 !important; box-shadow: 0 0 0 0.2rem rgba(255, 193, 7, 0.25); }
        .save-button-highlight { background-color: #ffc107 !important; border-color: #e0a800 !important; color: #000 !important; box-shadow: 0 0 0 0.2rem rgba(255, 193, 7, 0.5); animation: pulse-save 2s infinite; }
        @keyframes pulse-save { 0% { box-shadow: 0 0 0 0 rgba(255, 193, 7, 0.7);} 70% { box-shadow: 0 0 0 8px rgba(255, 193, 7, 0);} }
        </style>
        """)
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
                        ),
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
                                    _("**Disabled:** switch `Open inside direction for` to `Individual configuration per cat` in the `CONFIGURATION` section to enable this feature.")
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
                if not exit_mode_per_cat:
                    settings_rows.append(
                        ui.row(
                            ui.column(
                                12,
                                ui.markdown(
                                    _("**Disabled:** switch `Outside direction` to `Individual configuration per cat` in the `CONFIGURATION` section to enable this feature.")
                                ), style_="color: grey;"
                            ),
                            style_="padding-bottom: 20px;"
                        )
                    )

                settings_section = ui.div(
                    *settings_rows,
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
                manage_cats_unsaved_js,
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
                        style_="background: rgba(240, 240, 240, 0.9); text-align: center;"
                    ),
                    draggable=False, width="100%", left="0px", right="0px", bottom="0px", fixed=True,
                ),
            )
        else:
            ui_cards.append(
                ui.help_text(_("No cats found in the database. Please go to the [ADD NEW CAT] section to add a new cat."))
            )
            return ui.div(
                ui.layout_column_wrap(*ui_cards, width="400px"),
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
                card_rfid = input[f"mng_cat_rfid_{db_id}"]()
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
                            _("**Disabled:** switch `Open inside direction for` to `Individual configuration per cat` in the `CONFIGURATION` section to enable this feature.")
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
                            _("**Disabled:** switch `Outside direction` to `Individual configuration per cat` in the `CONFIGURATION` section to enable this feature.")
                        ), style_="color: grey;"
                    ),
                    style_="padding-bottom: 20px;"
                )
            )

        settings_section_new_cat = ui.div(
            *settings_rows_new,
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
    
    @reactive.Effect
    @reactive.event(input.add_new_cat_save)
    def add_new_cat_save():
        cat_name = input.add_new_cat_name()
        cat_rfid = input.add_new_cat_rfid()
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
                            ui.HTML('<a href="http://{}:8080" target="_blank" class="btn btn-default">{}</a>'.format(get_current_ip(), _("Open Label Studio"))),
                            style_="text-align: center;"
                        ),
                        ui.column(
                            12,
                            ui.help_text(_("Installed Version: ") + CONFIG["LABELSTUDIO_VERSION"]),
                            style_="text-align: center; padding-top: 20px;"
                        ),
                        ui.column(
                            12,
                            ui.help_text(_("Latest version: ") + labelstudio_latest_version),
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

            if labelstudio_latest_version != CONFIG["LABELSTUDIO_VERSION"]:
                ui_labelstudio = ui_labelstudio, ui.row(
                    ui.column(
                        12,
                        ui.input_task_button("btn_labelstudio_update", _("Update Label Studio"), icon=icon_svg("circle-up"), class_="btn-primary"),
                        ui.br(),
                        ui.help_text(_("Click the button to update Label Studio to the latest version.")),
                        ui.br(),
                        ui.help_text(_('Current Version') + ": " + CONFIG['LABELSTUDIO_VERSION']),
                        ui.br(),
                        ui.help_text(_('Latest Version') + ": " + labelstudio_latest_version),
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
        training_status = RemoteModelTrainer.check_model_training_result(show_notification=True, show_in_progress=True, return_pretty_status=True)

        # Show user notifications, if they are any
        show_user_notifications()

        # URLs for different languages
        wiki_url = {
            "de": "https://github.com/floppyFK/kittyhack/wiki/%5BDE%5D-Kittyhack-v2.0-%E2%80%90-Eigene-KI%E2%80%90Modelle-trainieren",
            "en": "https://github.com/floppyFK/kittyhack/wiki/%5BEN%5D-Kittyhack-v2.0-%E2%80%90-Train-own-AI%E2%80%90Models"
        }.get(CONFIG["LANGUAGE"], "https://github.com/floppyFK/kittyhack/wiki/%5BEN%5D-Kittyhack-v2.0-%E2%80%90-Train-own-AI%E2%80%90Models")
        
        ui_ai_training =  ui.div(
            # --- Description ---
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
                        _("The training process can take several hours, depending on the number of images.") + "  \n\n" +
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

            # --- Label Studio ---
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

            # --- Model Training ---
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(_("Model Training"), style_="text-align: center;"),
                    ),
                    ui.br(),
                    ui.div(
                        ui.div(
                            ui.div(
                                uix.input_file("model_training_data", _("Upload Label-Studio Training Data (ZIP file)"), accept=".zip", multiple=False, width="90%"),
                                ui.input_text("model_name", _("Model Name (optional)"), placeholder=_("Enter a name for your model"), width="90%"),
                                ui.input_text("user_name", _("Username (optional)"), value=CONFIG['USER_NAME'],placeholder=_("Enter your name"), width="90%"),
                                ui.input_text("email_notification", _("Email for Notification (optional)"), value=CONFIG['EMAIL'],placeholder=_("Enter your email address"), width="90%"),
                                ui.help_text(_("If you provide an email address, you will be notified when the model training is finished.")),
                                ui.br(),
                                ui.input_task_button("submit_model_training", _("Submit Model for Training"), class_="btn-primary"),
                                id="model_training_form",
                                style_="display: flex; flex-direction: column; align-items: center; justify-content: center;"
                            ),
                            style_="text-align: center;"
                        ) if not is_valid_uuid4(CONFIG["MODEL_TRAINING"]) else ui.div(
                            ui.markdown(_("A model training is currently in progress. This can take several hours.") + _("You will be notified by email when the training is finished.") if CONFIG['EMAIL'] else ""),
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
                        ),
                    ),
                    ui.br(),
                    full_screen=False,
                    class_="generic-container align-left",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),

            # --- Model Management ---
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
    
    @render.table
    @reactive.event(reload_trigger_ai, ignore_none=True)
    def manage_yolo_models_table():
        try:
            available_models = YoloModel.get_model_list()
            for model in available_models:
                unique_btn_id = hashlib.md5(os.urandom(16)).hexdigest()
                unique_model_id = model['unique_id']

                model['actions'] = ui.div(
                    ui.tooltip(
                        btn_yolo_modify(f"btn_yolo_modify_{unique_btn_id}"),
                        _("Modify or delete this model"),
                        id=f"tooltip_yolo_modify_{unique_btn_id}"
                    ),
                )
                # Add new event listeners for the buttons
                manage_yolo_model_server(f"btn_yolo_modify_{unique_btn_id}", unique_model_id)

            # Create a pandas DataFrame from the available models
            df = pd.DataFrame(available_models)
            df = df[['display_name', 'creation_date', 'actions']]  # Select only the columns we want to display
            df.columns = [_('Name'), _('Creation date'), ""]  # Rename columns for display

            return (
                df.style.set_table_attributes('class="dataframe shiny-table table w-auto table_models_overview"')
                .hide(axis="index")
            )
        except Exception as e:
            # Return an empty DataFrame with an error message
            return pd.DataFrame({
                'INFO': [_('Nothing here yet. Please train a model first.')]
            })
    
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
            
        logging.info(f"Enqueued model training: Model Name: '{model_name}', Email: '{email_notification}', ZIP file: '{zip_file_path}'")
        
        # Start the model training process
        result = RemoteModelTrainer.enqueue_model_training(zip_file_path, model_name, user_name, email_notification)
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
                CONFIG["LABELSTUDIO_VERSION"] = get_labelstudio_latest_version()
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
        start_time = tm.time()
        
        with ui.Progress(min=0, max=100) as p:
            p.set(message=_("Stopping Label Studio..."), detail=_("Please wait..."))
            
            while tm.time() - start_time < max_wait_time:
                if not get_labelstudio_status():
                    ui.notification_show(_("Label Studio stopped successfully."), duration=5, type="message")
                    reload_trigger_ai.set(reload_trigger_ai.get() + 1)
                    return
                
                # Update progress
                progress_percent = int(((tm.time() - start_time) / max_wait_time) * 100)
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
        start_time = tm.time()
        
        with ui.Progress(min=0, max=100) as p:
            p.set(message=_("Starting Label Studio..."), detail=_("Please wait...") + " " + _("(The first start of Label Studio may take several minutes!)"))
            
            while tm.time() - start_time < max_wait_time:
                # Check if service is running and the web server is responding on port 8080
                if get_labelstudio_status():
                    if is_port_open(8080):
                        ui.notification_show(_("Label Studio started successfully."), duration=5, type="message")
                        reload_trigger_ai.set(reload_trigger_ai.get() + 1)
                        return
                
                # Update progress
                progress_percent = int(((tm.time() - start_time) / max_wait_time) * 100)
                p.set(progress_percent)
                tm.sleep(0.5)
        
        # If we get here, the process didn't start within the timeout period
        ui.notification_show(_("Label Studio may not have started completely. Please check the logs."), duration=5, type="warning")
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    def collapsible_section(section_id, title, intro, content):
        return ui.div(
            # Custom CSS for the collapsible section
            ui.HTML("""
            <script>
            $(document).on('shown.bs.collapse hidden.bs.collapse', function(e) {
                // Update aria-expanded attribute for accessibility and chevron rotation
                var btn = $("[data-bs-target='#" + e.target.id + "']");
                btn.attr("aria-expanded", $("#" + e.target.id).hasClass("show"));
            });
            </script>
            """),
            ui.div(
                ui.HTML(f"""
                    <button class="collapsible-header-btn btn" type="button" data-bs-toggle="collapse" data-bs-target="#{section_id}_body" aria-expanded="false" aria-controls="{section_id}_body">
                        <span class="collapsible-chevron">&#9654;</span>
                        {title}
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

        # Add JavaScript for tracking unsaved changes
        unsaved_changes_js = ui.HTML("""
        <script>
        $(document).ready(function() {
          let hasUnsavedChanges = false;
          let isInitializing = true;

          // Remove highlights on page load
          $('#bSaveKittyhackConfig').removeClass('save-button-highlight');
          $('#config_tab_container .unsaved-input').removeClass('unsaved-input');

          // Function to highlight the changed input and save button
          function highlightInput(element) {
            if (!isInitializing) {
              $(element).addClass('unsaved-input');
              $('#bSaveKittyhackConfig').addClass('save-button-highlight');
              hasUnsavedChanges = true;
            }
          }

          // Function to highlight slider specifically
          function highlightSlider(sliderId) {
            if (!isInitializing) {
              const sliderContainer = $(`#${sliderId}`).closest('.form-group');
              sliderContainer.find('.irs-single').addClass('unsaved-input');
              sliderContainer.find('.irs-bar').addClass('unsaved-input');
              sliderContainer.find('.irs-handle').addClass('unsaved-input');
              $('#bSaveKittyhackConfig').addClass('save-button-highlight');
              hasUnsavedChanges = true;
            }
          }

          // Initialization period: ignore changes for 1 second
          setTimeout(function() {
            isInitializing = false;
          }, 1000);

          // Only monitor inputs inside the configuration tab
          $('#config_tab_container').on('change', 'input, select', function() {
            highlightInput(this);
          });

          // Special handling for range sliders with direct observation of the hidden input
          $('#config_tab_container').on('change input', 'input.js-range-slider', function() {
            const sliderId = $(this).attr('id');
            highlightSlider(sliderId);
          });

          // Also observe the slider handle being dragged using mousedown/mouseup events
          $('#config_tab_container').on('mousedown touchstart', '.irs-handle', function() {
            const slider = $(this).closest('.form-group').find('input.js-range-slider');
            const sliderId = slider.attr('id');
            $(this).data('dragging', true);
            $(document).one('mouseup touchend', function() {
              if ($(this).data('dragging')) {
                highlightSlider(sliderId);
                $(this).data('dragging', false);
              }
            });
          });

          // Monitor text inputs and textareas as they're typed in
          $('#config_tab_container').on('input', 'input[type="text"], input[type="password"], textarea', function() {
            highlightInput(this);
          });

          // Reset when save button is clicked
          $(document).on('click', '#bSaveKittyhackConfig', function() {
            $('#config_tab_container .unsaved-input').removeClass('unsaved-input');
            $('#bSaveKittyhackConfig').removeClass('save-button-highlight');
            hasUnsavedChanges = false;
          });
        });
        </script>
        """)

        hide_unhide_ip_camera_url = ui.HTML("""
        <script>
        $(document).ready(function() {
            function toggleIpCameraUrlField() {
                if ($('#camera_source').val() === 'ip_camera') {
                    $('#ip_camera_url_container').show();
                    $('#ip_camera_warning').show();
                } else {
                    $('#ip_camera_url_container').hide();
                    $('#ip_camera_warning').hide();
                }
            }
            toggleIpCameraUrlField();
            $('#camera_source').on('change', toggleIpCameraUrlField);
        });
        </script>
        """)

        ui_config =  ui.div(
            unsaved_changes_js,
            hide_unhide_ip_camera_url,
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
                        ui.row(
                            ui.column(4, ui.input_slider("sldWlanTxPower", _("WLAN TX power (in dBm)"), min=0, max=20, value=CONFIG['WLAN_TX_POWER'], step=1)),
                            ui.column(
                                8,
                                ui.markdown(
                                    _("WARNING: You should keep the TX power as low as possible to avoid interference with the PIR Sensors! You should only increase this value, if you have problems with the WLAN connection.") + "\n\n" +
                                    "*(" + _("Default value: {}").format(DEFAULT_CONFIG['Settings']['wlan_tx_power']) + ")*"
                                ), style_="color: grey;"
                            ),
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
                        ui.br(),
                        ui.column(
                            12,
                            ui.markdown(
                                _(
                                    "You can use an **external IP camera** instead of the internal camera to achieve better viewing angles for prey detection. "
                                    "This can be especially useful if the internal camera's position is not ideal for your setup. "
                                    "You can also achieve much better night vision capabilities with an external camera. "
                                    "\n\n"
                                    "**Important notes:**\n"
                                    "- Both the Kittyflap and the IP camera must have a stable and strong WLAN connection for reliable operation. "
                                    "A **wired (Ethernet) connection** for the IP camera is recommended, as it provides much higher reliability and performance compared to WLAN.\n"
                                    "- The IP camera must be configured with a **fixed (static) IP address**.\n"
                                    "- Make sure your IP camera supports a compatible video stream (e.g., RTSP, HTTP MJPEG, or similar formats).\n"
                                    "- For best performance, use a resolution of up to 1280x720 and a frame rate of up to 15fps (higher resolutions will decrease the performance of the kittyflap drastically).\n"
                                    "- The resolution can often be configured in the settings of your IP camera. Some cameras also provide different URLs for different resolutions.\n"
                                    "\n"
                                    "If you experience connection issues or interruptions, please check the WLAN signal strength and the network configuration of both devices."
                                )
                            ),
                            style_="color: grey;"
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
                            ui.markdown(
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
                            style_="color: grey;"
                        ),
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
                            ui.column(4, ui.input_slider("sldMouseThreshold", _("Mouse detection threshold"), min=0, max=100, value=CONFIG['MOUSE_THRESHOLD'])),
                            ui.column(
                                8,
                                ui.markdown(
                                    _("Kittyhack decides based on this value, if a picture contains a mouse.") + "  \n" +
                                    _("If the detected mouse probability of a picture exceeds this value, the flap will remain closed.") + "  \n" +
                                    "*(" + _("Default value: {}").format(DEFAULT_CONFIG['Settings']['mouse_threshold']) + ")*" + "  \n\n" +
                                    "**" + _("Note: The minimum of this value is always coupled to the configured value of 'Minimum detection threshold' below.") + "**"
                                ), style_="color: grey;"
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(4, ui.input_slider("sldMinThreshold", _("Minimum detection threshold"), min=0, max=80, value=CONFIG['MIN_THRESHOLD'])),
                            ui.column(
                                8,
                                ui.markdown(
                                    _("This threshold will be used for the decision, if an motion event on the outside shall be logged or not:") + "\n\n" +
                                    _("The detected probability of objects must exceed this threshold at least in one picture of a motion event. Otherwise the pictures will be discarded and the event will not be logged.") + "\n\n" +
                                    "*(" + _("Default value: {}").format(DEFAULT_CONFIG['Settings']['min_threshold']) + ")*"
                                ), style_="color: grey;"
                            )
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(4, ui.input_numeric("numMinPicturesToAnalyze", _("Minimum pictures before unlock decision"), CONFIG['MIN_PICTURES_TO_ANALYZE'], min=1)),
                            ui.column(
                                8,
                                ui.markdown(
                                    _("Number of pictures that must be analyzed before deciding to unlock the flap. If a picture exceeds the mouse threshold, the flap will remain closed.") + "  \n" +
                                    _("If a picture after this minimum number of pictures exceeds the mouse threshold, the flap will be closed again.") + "  \n" +
                                    "*(" + _("Default value: {}").format(DEFAULT_CONFIG['Settings']['min_pictures_to_analyze']) + ")*"
                                ), style_="color: grey;"
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(4, ui.input_slider("sldLockAfterPreyDetect", _("Lock duration after prey detection (in s)"), min=30, max=1800, step=5, value=CONFIG['LOCK_DURATION_AFTER_PREY_DETECTION'])),
                            ui.column(8, ui.markdown(_("The flap will remain closed for this time after a prey detection.")), style_="color: grey;")
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(12, ui.input_switch("btnDetectPrey", _("Detect prey"), CONFIG['MOUSE_CHECK_ENABLED'])),
                            ui.column(
                                12,
                                ui.markdown(
                                    _("If the prey detection is enabled and the mouse detection threshold is exceeded in a picture, the flap will remain closed.") + "\n\n" +
                                    _("**NOTE:** This is the global setting. You can also configure prey detection individually per cat in the `MANAGE CATS` section.")
                                ), style_="color: grey;"
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(4, ui.input_select(
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
                                )
                            )),
                            ui.column(
                                8,
                                ui.markdown(
                                    "- **" + _("Original Kittyflap Model v1:") + "** " + _("Always tries to detect objects `Mouse` and `No Mouse`, even if there is no such object in the picture (this was the default in Kittyhack v1.4.0 and lower)") + "\n\n" +
                                    "- **" + _("Original Kittyflap Model v2:") + "** " + _("Only tries to detect objects `Mouse` and `No Mouse` if there is a cat in the picture.") + "\n\n" +
                                    "- **" + _("Custom Models:") + "** " + _("These are your own trained models, which you have created in the `AI TRAINING` section.") + "\n\n" +
                                    "> " + _("If you change this setting, the Kittyflap must be restarted to apply the new model version.")
                                ), style_="color: grey;"
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(12, ui.input_switch("btnUseCameraForCatDetection", _("Use camera for cat detection"), CONFIG['USE_CAMERA_FOR_CAT_DETECTION'])),
                            ui.column(
                                12,
                                ui.markdown(
                                    _("If this setting is enabled, the camera will also be used for cat detection (in addition to the RFID reader).") + "  \n\n" +
                                    _("You can configure the required threshold for the cat detection with the slider `Cat detection threshold` below.") + " " +
                                    _("If the detection is successful, the inside direction will be opened.") + "\n\n" +
                                    _("**NOTE:** This feature requires a custom trained model for your cat(s). It does not work with the default kittyflap models.") + "\n\n" +
                                    _("This feature depends heavily on the quality of your model and sufficient lighting conditions.") + " " +
                                    _("If one or both are not good, the detection may either fail or other - similiar looking cats may be detected as your cat.")
                                ), style_="color: grey;"
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(12, ui.input_switch("btnUseCameraForMotionDetection", _("Use camera for motion detection"), CONFIG['USE_CAMERA_FOR_MOTION_DETECTION'])),
                            ui.column(
                                12,
                                ui.markdown(
                                    _("If this setting is enabled, instead of the outside PIR sensor, the camera will be used for motion detection.") + "  \n\n" +
                                    _("**How it works:**") + "  \n" +
                                    _("- In regular operation, Kittyhack waits for a trigger from the outside PIR sensor before starting camera analysis") + "  \n" + 
                                    _("- With this feature enabled, the PIR sensor is disabled and the camera continuously analyzes images") + "  \n" + 
                                    _("- When a cat is detected in the camera feed, it's treated as equivalent to a motion detection outside") + "  \n\n" +
                                    _("You can configure the required threshold for the cat detection with the slider `Cat detection threshold` below.") + "  \n\n" +
                                    _("This may be very helpful in areas where environmental factors (moving trees, people passing by) permanently cause false PIR triggers.") + "  \n\n" +
                                    _("**NOTE:** This feature requires a custom trained model for your cat(s). It does not work with the default kittyflap models.") + "\n\n" +
                                    _("This feature depends heavily on the quality of your model and sufficient lighting conditions.") + " " +
                                    _("If one or both are not good, you may experience false triggers or your cat may not be detected correctly.")
                                ), style_="color: grey;"
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(4, ui.input_slider("sldCatThreshold", _("Cat detection threshold"), min=0, max=100, value=CONFIG['CAT_THRESHOLD'])),
                            ui.column(
                                8,
                                ui.markdown(
                                    _("Kittyhack decides based on this value, if a picture contains your cat.") + "  \n" + 
                                    _("If the detected probability of your cat exceeds this value in a picture, and the setting `Use camera for cat detection` is enabled, the flap will be opened.") + "  \n" +
                                    "*(" + _("Default value: {}").format(DEFAULT_CONFIG['Settings']['cat_threshold']) + ")*" + "  \n\n" +
                                    "**" + _("Note: The minimum of this value is always coupled to the configured value of 'Minimum detection threshold' above.") + "**"
                                ), style_="color: grey;"
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(4, ui.input_select(
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
                            )),
                            ui.column(
                                8,
                                ui.div(
                                    ui.markdown(
                                        _("This setting defines which cats are allowed to enter the house.") + "  \n\n" +
                                        _("- **All cats:** *Every* detected motion on the outside will unlock the flap.") + "  \n" +
                                        _("- **All cats with a RFID chip:** Every successful RFID detection will unlock the flap.") + "  \n" +
                                        _("- **Only registered cats:** Only the cats that are registered in the database will unlock the flap (either by RFID or by camera detection, if enabled).") + "  \n" +
                                        _("- **Individual configuration per cat:** Configure per cat if it is allowed to enter (in the `MANAGE CATS` section).") + "  \n" +
                                        _("- **No cats:** The inside direction will never be opened.")
                                    ),
                                    style_="color: grey;"
                                )
                            ),
                        ),
                        ui.row(
                            ui.column(
                                12,
                                ui.div(
                                    # Precompute translations to avoid _() inside f-strings
                                    ui.HTML(('''
                                        <button id="btn_toggle_entry_logic"
                                                class="btn btn-default"
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
                            ui.column(4, ui.input_select(
                                "btnAllowedToExit",
                                _("Outside direction:"),
                                {
                                    'allow': _("Allow exit"),
                                    'deny': _("Do not allow exit"),
                                    'configure_per_cat': _("Individual configuration per cat (Beta)"),
                                },
                                selected=str(CONFIG['ALLOWED_TO_EXIT'].value)
                            )),
                            ui.column(
                                8,
                                ui.div(
                                    ui.markdown(
                                        _("This setting defines the behavior of the outside direction.") + "  \n\n" +
                                        _("- **Allow exit:** The outside direction is always possible. You can also configure time ranges below to restrict the exit times.") + "  \n" +
                                        _("- **Do not allow exit:** The outside direction is always closed.") + "  \n" +
                                        _("- **Individual configuration per cat:** Configure per cat if it is allowed to exit (in the `MANAGE CATS` section). The time ranges below are applied in addition.")+ "  \n  " +
                                        _("**NOTE:** All your cats must be registered **with a RFID chip** to use this mode. Cats without RFID can not go outside in this mode!")
                                    ),
                                    style_="color: grey;"
                                )
                            ),
                        ),
                        ui.row(
                            ui.column(
                                12,
                                ui.div(
                                    # Precompute translations to avoid _() inside f-strings
                                    ui.HTML(('''
                                        <button id="btn_toggle_exit_logic"
                                                class="btn btn-default"
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
                        # Shared JS/CSS for logic toggles
                        ui.HTML(f"""
                        <script>
                        (function() {{
                            function toggleSection(btnId, sectionId, hintId) {{
                                var btn = document.getElementById(btnId);
                                var section = document.getElementById(sectionId);
                                var hint = document.getElementById(hintId);
                                if (!btn || !section) return;
                                var span = btn.querySelector('span');
                                var showLbl = btn.getAttribute('data-show-label');
                                var hideLbl = btn.getAttribute('data-hide-label');
                                if (section.style.display === 'none' || section.style.display === '') {{
                                    section.style.display = 'block';
                                    if (hint) hint.style.display = 'block';
                                    if (span) span.textContent = hideLbl;
                                }} else {{
                                    section.style.display = 'none';
                                    if (hint) hint.style.display = 'none';
                                    if (span) span.textContent = showLbl;
                                }}
                            }}
                            function showEntryLogic(mode) {{
                                var container = document.getElementById('entry_logic_images');
                                if (!container) return;
                                container.querySelectorAll('.logic-img-wrapper').forEach(function(el){{
                                    el.style.display = (el.getAttribute('data-mode') === mode) ? 'block' : 'none';
                                }});
                            }}
                            function showExitLogic(mode) {{
                                var container = document.getElementById('exit_logic_images');
                                if (!container) return;
                                container.querySelectorAll('.logic-img-wrapper').forEach(function(el){{
                                    el.style.display = (el.getAttribute('data-mode') === mode) ? 'block' : 'none';
                                }});
                            }}
                            document.addEventListener('click', function(e){{
                                if (e.target.id === 'btn_toggle_entry_logic' || e.target.closest('#btn_toggle_entry_logic')) {{
                                    toggleSection('btn_toggle_entry_logic','entry_logic_expand','entry_logic_hint');
                                    var sel = document.getElementById('txtAllowedToEnter');
                                    if (sel) showEntryLogic(sel.value);
                                }}
                                if (e.target.id === 'btn_toggle_exit_logic' || e.target.closest('#btn_toggle_exit_logic')) {{
                                    toggleSection('btn_toggle_exit_logic','exit_logic_expand','exit_logic_hint');
                                    var sel2 = document.getElementById('btnAllowedToExit');
                                    if (sel2) showExitLogic(sel2.value);
                                }}
                            }});
                            document.addEventListener('change', function(e){{
                                if (e.target.id === 'txtAllowedToEnter') {{
                                    showEntryLogic(e.target.value);
                                }}
                                if (e.target.id === 'btnAllowedToExit') {{
                                    showExitLogic(e.target.value);
                                }}
                            }});
                        }})();
                        </script>
                        """),
                        ui.br(),
                        ui.br(),
                        ui.div(
                            ui.row(
                                ui.column(
                                    12, 
                                    ui.markdown(
                                        _("You can specify here up to 3 time ranges, in which your cats are **allowed to exit**.") + "  \n" +
                                        _("If no time range is activated, the cats are allowed to exit at any time.") + "  \n" +
                                        _("Please set the time in 24h format with `:` (e.g. `13:00`)")
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
                            ui.column(4, ui.input_slider("sldPirInsideThreshold", _("Reaction speed (in s) of the motion sensor on the inside"), min=0.1, max=6, step=0.1, value=CONFIG['PIR_INSIDE_THRESHOLD'])),
                            ui.column(
                                8,
                                ui.markdown(
                                    _("A low value means a fast reaction, but also a higher probability of false alarms. A high value means a slow reaction, but also a lower probability of false alarms.") + "  \n" +
                                    _("The default setting should be a good value for most cases.") + "  \n" +
                                    "*(" + _("Default value: {}").format(DEFAULT_CONFIG['Settings']['pir_inside_threshold']) + ")*"
                                ), style_="color: grey;"
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
                                    "      - entity: switch.{}_allow_exit\n".format(CONFIG['MQTT_DEVICE_ID']) +
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
                                    _("⚠️ **WARNING**: It is NOT recommended to enable this feature! Several users have reported that this option causes reboots or system freezes.") +
                                    _("If you encounter the same issue, it's strongly recommended to disable this setting.")
                                ), style_="color: #e74a3b; padding: 10px; border: 1px solid #e74a3b; border-radius: 5px; margin: 20px; width: 90%;"
                            ),
                            ui.column( 12, ui.markdown("> " + _("NOTE: This setting requires a restart of the kittyflap to take effect.")), style_="color: grey;"),
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
                        ui.row(
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
                        style_="background: rgba(240, 240, 240, 0.9); text-align: center;"
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

        # override the variable with the data from the configuration page
        language_changed = CONFIG['LANGUAGE'] != input.txtLanguage()
        if (input.selectedModel().startswith("tflite::") and
            input.selectedModel() != f"tflite::{CONFIG['TFLITE_MODEL_VERSION']}"):
            selected_model_changed = True
        elif (input.selectedModel().startswith("yolo::") and
              input.selectedModel() != f"yolo::{CONFIG['YOLO_MODEL']}"):
            selected_model_changed = True
        else:
            selected_model_changed = False
        img_processing_cores_changed = CONFIG['USE_ALL_CORES_FOR_IMAGE_PROCESSING'] != input.btnUseAllCoresForImageProcessing()

        # Update the configuration dictionary with the new values
        CONFIG['LANGUAGE'] = input.txtLanguage()
        CONFIG['TIMEZONE'] = input.txtConfigTimezone()
        CONFIG['DATE_FORMAT'] = input.txtConfigDateformat()
        CONFIG['MOUSE_THRESHOLD'] = float(input.sldMouseThreshold())
        CONFIG['MIN_THRESHOLD'] = float(input.sldMinThreshold())
        CONFIG['MIN_PICTURES_TO_ANALYZE'] = int(input.numMinPicturesToAnalyze())
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
        CONFIG['WLAN_TX_POWER'] = int(input.sldWlanTxPower())
        CONFIG['LOCK_DURATION_AFTER_PREY_DETECTION'] = int(input.sldLockAfterPreyDetect())
        CONFIG['MAX_PICTURES_PER_EVENT_WITH_RFID'] = int(input.numMaxPicturesPerEventWithRfid())
        CONFIG['MAX_PICTURES_PER_EVENT_WITHOUT_RFID'] = int(input.numMaxPicturesPerEventWithoutRfid())
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
        CONFIG['MQTT_ENABLED'] = input.btnMqttEnabled()
        CONFIG['MQTT_BROKER_ADDRESS'] = input.txtMqttBrokerAddress()
        CONFIG['MQTT_BROKER_PORT'] = int(input.numMqttBrokerPort())
        CONFIG['MQTT_USERNAME'] = input.txtMqttUsername()
        CONFIG['MQTT_PASSWORD'] = input.txtMqttPassword()
        CONFIG['MQTT_IMAGE_PUBLISH_INTERVAL'] = float(input.mqtt_image_publish_interval())
        CONFIG['RESTART_IP_CAMERA_STREAM_ON_FAILURE'] = input.btnRestartIpCameraStreamOnFailure()
        CONFIG['WLAN_WATCHDOG_ENABLED'] = input.btnWlanWatchdogEnabled()

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
            if language_changed:
                ui.notification_show(_("Please restart the kittyflap in the [SYSTEM] section, to apply the new language."), duration=30, type="message")
                update_mqtt_language()

            if selected_model_changed:
                ui.notification_show(_("Please restart the kittyflap in the [SYSTEM] section, to apply the new detection model."), duration=30, type="message")
            
            if img_processing_cores_changed or hostname_changed:
                ui.notification_show(_("Please restart the kittyflap in the [SYSTEM] section, to apply the changed configuration."), duration=30, type="message")

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
            configured_wlans = get_wlan_connections()
            i = 0
            for wlan in configured_wlans:
                unique_id = hashlib.md5(os.urandom(16)).hexdigest()
                if wlan['connected']:
                    wlan['connected_icon'] = "🟢"
                else:
                    wlan['connected_icon'] = "⚫"
                wlan['actions'] = ui.div(
                    ui.tooltip(
                        btn_wlan_connect(f"btn_wlan_connect_{unique_id}"),
                        _("Enforce connection to this WLAN"),
                        id=f"tooltip_wlan_connect_{unique_id}"
                    ),
                    ui.tooltip(
                        btn_wlan_modify(f"btn_wlan_modify_{unique_id}"),
                        _("Modify this WLAN"),
                        id=f"tooltip_wlan_modify_{unique_id}"
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
        global user_wlan_action_in_progress
        user_wlan_action_in_progress = True
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
        success = manage_and_switch_wlan(ssid, password, priority, password_changed)
        user_wlan_action_in_progress = False
        if success:
            ui.notification_show(_("WLAN configuration for {} updated successfully.").format(ssid), duration=5, type="message")
            reload_trigger_wlan.set(reload_trigger_wlan.get() + 1)
        else:
            ui.notification_show(_("Failed to update WLAN configuration for {}").format(ssid), duration=10, type="error")
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
            available_wlans = scan_wlan_networks()
            for wlan in available_wlans:
                signal_strength = wlan['bars']
                wlan['signal_icon'] = f"{wlan['signal']}% "
                if signal_strength == 0:
                    wlan['signal_icon'] += "⚫"
                elif signal_strength == 1:
                    wlan['signal_icon'] += "🔴"
                elif 2 <= signal_strength <= 3:
                    wlan['signal_icon'] += "🟡"
                else:
                    wlan['signal_icon'] += "🟢"
                

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
        
    @render.download(filename="kittyhack.db")
    def download_kittyhack_db():
        try:
            # Show a modal dialog to prevent a double click of the download button
            ui.modal_show(
                ui.modal(
                    ui.div(
                        ui.markdown(_("Please wait...")),
                        ui.HTML('<div class="spinner-container"><div class="spinner"></div></div>'),
                    ),
                    title=_("Preparing Download"),
                    easy_close=False,
                    footer=None
                )
            )
            sigterm_monitor.halt_backend()
            tm.sleep(1.0)
            ui.modal_remove()
            return CONFIG['KITTYHACK_DATABASE_PATH']
        except Exception as e:
            logging.error(f"Failed to halt the backend processes: {e}")
            ui.notification_show(_("Failed to halt the backend processes: {}").format(e), duration=10, type="error")
            ui.modal_remove()
            return None
        finally:
            logging.info(f"A download of the kittyhack database was requested --> Restart pending.")
            # Show the restart dialog
            m = ui.modal(
                ui.markdown(
                    _("Please click the 'Reboot' button to restart the Kittyflap **after** the download has finished.") + "\n\n" +
                    _("NOTE: The download will start in the background, so check your browser's download section.")
                ),
                title=_("Download started..."),
                easy_close=False,
                footer=ui.div(
                    ui.input_action_button("btn_modal_reboot_ok", _("Reboot")),
                )
            )
            ui.modal_show(m)
    
    @render.download(filename="kittyflap.db")
    def download_kittyflap_db():
        if os.path.exists(CONFIG['DATABASE_PATH']):
            return CONFIG['DATABASE_PATH']
        else:
            ui.notification_show(_("The original kittyflap database file does not exist."), duration=10, type="error")
            return None
    
    @output
    @render.ui
    @reactive.event(reload_trigger_info, ignore_none=True)
    def ui_info():
        # Check if the current version is different from the latest version
        latest_version = CONFIG['LATEST_VERSION']
        if latest_version == "unknown":
            ui_update_kittyhack = ui.markdown(_("Unable to fetch the latest version from github. Please try it again later or check your internet connection."))
        elif git_version != latest_version:
            # Check for local changes in the git repository
            try:
                # Fetch the release notes of the latest version
                release_notes = fetch_github_release_notes(latest_version)
                ui_update_kittyhack = ui.div(
                    ui.markdown("**" + _("Release Notes for") + " " + latest_version + ":**"),
                    ui.div(
                        ui.markdown(release_notes),
                        class_="release_notes"
                    ),
                    ui.br()
                )
                
                ui_update_kittyhack = ui_update_kittyhack, ui.div(
                    ui.markdown(_("Automatic update to **{}**:").format(latest_version)),
                    ui.input_task_button("update_kittyhack", _("Update Kittyhack"), icon=icon_svg("download"), class_="btn-primary"),
                    ui.br(),
                    ui.help_text(_("Important: A stable WLAN connection is required for the update process.")),
                    ui.br(),
                    ui.help_text(_("The update will end with a reboot of the Kittyflap.")),
                    ui.markdown(_("Check out the [Changelog](https://github.com/floppyFK/kittyhack/releases) to see what's new in the latest version.")),
                )

                # Check for local changes in the git repository and warn the user
                result = subprocess.run(["/bin/git", "status", "--porcelain"], capture_output=True, text=True, check=True)
                if result.stdout.strip():
                    # Local changes detected
                    result = subprocess.run(["/bin/git", "status"], capture_output=True, text=True, check=True)
                    ui_update_kittyhack = ui_update_kittyhack, ui.div(
                        ui.hr(),
                        ui.markdown(
                            _("⚠️ WARNING: Local changes detected in the git repository in `/root/kittyhack`.") + "\n\n" +
                            _("If you proceed with the update, these changes will be lost (the database and configuration will not be affected).") + "\n\n" +
                            _("Please commit or stash your changes manually before updating, if you want to keep them.")
                        ),
                        ui.h6(_("Local changes:")),
                        ui.div(
                            result.stdout,
                            class_ = "release_notes",
                            style_ = "font-family: monospace; white-space: pre-wrap;"
                        )
                    )
                    
            except Exception as e:
                ui_update_kittyhack = ui.markdown(
                    _("An error occurred while checking for local changes in the git repository: {}").format(e) + "\n\n" +
                    _("No automatic update possible.")
                )
        
        else:
            ui_update_kittyhack = ui.markdown(_("You are already using the latest version of Kittyhack."))

        # Check if the original kittyflap database file still exists
        kittyflap_db_file_exists = os.path.exists(CONFIG['DATABASE_PATH'])
        if kittyflap_db_file_exists:
            if get_file_size(CONFIG['DATABASE_PATH']) > 100:
                ui_kittyflap_db = ui.div(
                    ui.markdown(
                        _("The original kittyflap database file consumes currently **{:.1f} MB** of disk space.").format(get_file_size(CONFIG['DATABASE_PATH'])) + "\n\n" +
                        _("The file contains a lot pictures which could not be uploaded to the original kittyflap servers anymore.") + "\n\n" +
                        _("You could delete the pictures from it to free up disk space.")
                    ),
                    ui.input_task_button("clear_kittyflap_db", _("Remove pictures from original Kittyflap Database"), icon=icon_svg("trash")),
                    ui.download_button("download_kittyflap_db", _("Download Kittyflap Database"), icon=icon_svg("download")),
                )
            elif get_file_size(CONFIG['DATABASE_PATH']) > 0:
                    ui_kittyflap_db = ui.div(
                        ui.markdown(_("The original kittyflap database file exists and has a regular size of **{:.1f} MB**. Nothing to do here.").format(get_file_size(CONFIG['DATABASE_PATH']))),
                        ui.download_button("download_kittyflap_db", _("Download Kittyflap Database"), icon=icon_svg("download")),
                    )
            else:
                ui_kittyflap_db = ui.markdown(_("The original kittyflap database seems to be empty.") + "\n\n" +
                                              _("**WARNING:** A downgrade to Kittyhack v1.1.0 will probably not work!"))
        else:
            ui_kittyflap_db = ui.markdown(_("The original kittyflap database file does not exist anymore.") + "\n\n" +
                                          _("**WARNING:** A downgrade to Kittyhack v1.1.0 is not possible without the original database file!"))
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
                    ui.HTML(f"<center><p><a href='https://github.com/floppyFK/kittyhack' target='_blank'>{icon_svg('square-github')} {_('GitHub Repository')}</a></p></center>"),
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
                    ui.markdown("**" + _("Current Version") + ":** `" + git_version + "`" + "  \n" \
                                "**" + _("Latest Version") + ":** `" + latest_version + "`"),
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
                    ui.div(ui.download_button("download_kittyhack_db", _("Download Kittyhack Database"), icon=icon_svg("download"))),
                    ui.markdown(
                        _("**WARNING:** To download the database, the Kittyhack software will be stopped and the flap will be locked. ") + "\n\n" +
                        _("You have to restart the Kittyflap afterwards to return to normal operation.")
                    ),
                    ui.hr(),
                    ui.div(ui.download_button("download_config", _("Download Configuration File"), icon=icon_svg("download"))),
                    ui.br(),
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),
            ui.div(
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
            ),
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
                                "⚠️ " + _("**HTTPS Required**") + "\n\n" +
                                _("PWA installation requires a secure connection (HTTPS). You are currently accessing Kittyhack via HTTP.") + "  \n\n" +
                                _("You'll need to set up a reverse proxy with HTTPS.") + " " +
                                _("If you want to setup a reverse proxy in your home network, you can watch") +" [" + _("this guide") + "](https://schroederdennis.de/allgemein/nginx-proxy-manager-nginx-reverse-proxy-vorgestellt/)."
                            ),
                            id="pwa_https_warning",
                            style_="display: none; color: #e74a3b; padding: 10px; border: 1px solid #e74a3b; border-radius: 5px; margin: 10px 0;"
                        ),
                        # Already installed message
                        ui.div(
                            ui.markdown("✅ " + _("**App is already installed on this device!**")),
                            id="pwa_already_installed", 
                            style_="display: none; color: #1cc88a; padding: 10px; text-align: center;"
                        ),
                        # Success message
                        ui.div(
                            ui.markdown("✅ " + _("**Installation successful!**")),
                            id="pwa_installed_success", 
                            style_="display: none; color: #1cc88a; padding: 10px; text-align: center;"
                        ),
                        # Install button
                        ui.div(
                            ui.input_action_button(
                                id="pwa_install_button",
                                label=_("Install as App"),
                                icon=icon_svg("download"),
                                class_="btn-primary"
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

        # Get WLAN status information
        try:
            wlan = subprocess.run(["/sbin/iwconfig", "wlan0"], capture_output=True, text=True, check=True)
            if "Link Quality=" in wlan.stdout:
                quality = wlan.stdout.split("Link Quality=")[1].split(" ")[0]
                signal = wlan.stdout.split("Signal level=")[1].split(" ")[0]
                quality_value = float(quality.split('/')[0]) / float(quality.split('/')[1])
                
                # Choose appropriate icon based on signal quality
                if quality_value >= 0.8:
                    wlan_icon = "🟢"
                elif quality_value >= 0.4:
                    wlan_icon = "🟡"
                else:
                    wlan_icon = "🔴"
                wlan_info = f"{wlan_icon} " + _("Quality: {}, Signal: {} dBm").format(quality, signal)
            else:
                wlan_info = _("Not connected")
        except:
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
    @reactive.event(input.update_kittyhack)
    def update_kittyhack_process():
        latest_version = CONFIG['LATEST_VERSION']
        current_version = git_version

        set_update_progress(
            in_progress=True,
            step=1,
            max_steps=8,
            message=_("Starting update..."),
            detail="",
            result=None,
            error_msg="",
        )

        # Start the update in a background thread so the UI can update immediately
        def run_update():
            def progress_callback(step, message, detail):
                set_update_progress(
                    in_progress=True,
                    step=step,
                    max_steps=8,
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

    @output
    @render.text
    def update_progress_percent():
        reactive.invalidate_later(0.5)
        state = get_update_progress()
        if state["max_steps"]:
            return f"{int(100 * state['step'] / state['max_steps'])}%"
        return "0%"

    # Update progress modal for all sessions
    def show_update_progress_modal():
        reactive.invalidate_later(0.5)
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
                            <script>
                            (function() {
                                var percentTextEl = document.getElementById('progress_percent_text');
                                var barEl = document.getElementById('progress_bar');
                                function updateBar() {
                                    if (percentTextEl && barEl) {
                                        var percent = percentTextEl.textContent.trim();
                                        if(percent.endsWith('%')) percent = percent.slice(0, -1);
                                        var val = parseInt(percent);
                                        if (!isNaN(val)) {
                                            barEl.style.width = val + '%';
                                        }
                                    }
                                }
                                if (window.MutationObserver && percentTextEl) {
                                    var observer = new MutationObserver(updateBar);
                                    observer.observe(percentTextEl, { childList: true, subtree: true });
                                    updateBar();
                                }
                                // Animate dots
                                var msgEl = document.getElementById('in_progress_dots');
                                var dots = 0;
                                setInterval(function() {
                                    if (msgEl) {
                                        dots = (dots + 1) % 4;
                                        msgEl.textContent = '.'.repeat(dots);
                                    }
                                }, 700);
                            })();
                            </script>
                        </div>
                        <br>
                    """),
                    ui.markdown(_("Do not close this page until the update is finished!")),
                    ui.markdown(_("*This step may take several minutes. Please be patient.*")),
                ),
                title=_("Updating Kittyhack..."),
                easy_close=False,
                footer=None,
                id="update_progress_modal"
            )
        )

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
