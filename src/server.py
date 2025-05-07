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
from typing import List
from src.baseconfig import (
    CONFIG,
    AllowedToEnter,
    set_language,
    save_config,
    configure_logging,
    DEFAULT_CONFIG,
    JOURNAL_LOG,
    LOGFILE,
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
    scan_wlan_networks
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
        message=_("The kittyflap was not shut down gracefully several times in a row. Please do not power off the device without shutting it down first!") + "\n\n" +
                _("If you did not unplug the Kittyflap from power without shutting it down, please report your log files on the GitHub issue tracker, thanks!") + "\n\n" +
                _("> **NOTE:** The option 'Use all CPU cores for image processing' has been disabled now automatically, since this could cause the issue on some devices.") + "\n" +
                _("Please check the settings and enable it again, if you want to use it."),
                type="warning",
                id="not_graceful_shutdown",
                skip_if_id_exists=True
        )
    
# Now proceed with the startup
from src.backend import backend_main, manual_door_override, model_hanlder
from src.magnets import Magnets
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

# END OF MIGRATION RULES ############################################################################################

logging.info(f"Current version: {git_version}")

# Log all configuration values from CONFIG dictionary
logging.info("Configuration values:")
for key, value in CONFIG.items():
    logging.info(f"{key}={value}")

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

# Frontend background task in a separate thread
def start_background_task():
    # Register task in the sigterm_monitor object
    sigterm_monitor.register_task()

    def run_periodically():
        while not sigterm_monitor.stop_now:
            global free_disk_space

            # Periodically check that the kwork and manager services are NOT running anymore
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

            # Use a shorter sleep interval and check for sigterm_monitor.stop_now to allow graceful shutdown
            for _ in range(int(CONFIG['PERIODIC_JOBS_INTERVAL'])):
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
                footer=ui.div(),
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
    @reactive.event(input.btn_modal_cancel)
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
            ui.notification_show(_("Failed to update WLAN configuration for {}").format(ssid), duration=5, type="error")
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
            ui.notification_show(_("Failed to delete WLAN connection {}").format(ssid), duration=5, type="error")

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
            ui.notification_show(_("Failed to update model configuration {}").format(model_name), duration=5, type="error")
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
            ui.notification_show(_("Failed to delete Model {}").format(input.txtModelName()), duration=5, type="error")
        ui.modal_remove()

#######################################################################
# The main server application
#######################################################################
def server(input, output, session):

    # Create reactive triggers
    reload_trigger_cats = reactive.Value(0)
    reload_trigger_info = reactive.Value(0)
    
    # Show a notification if a new version of Kittyhack is available
    if CONFIG['LATEST_VERSION'] != "unknown" and CONFIG['LATEST_VERSION'] != git_version and CONFIG['PERIODIC_VERSION_CHECK']:
        ui.notification_show(_("A new version of Kittyhack is available: {}. Go to the 'Info' section for update instructions.").format(CONFIG['LATEST_VERSION']), duration=10, type="message")

    # Show a warning if the remaining disk space is below the critical threshold
    kittyflap_db_file_exists = os.path.exists(CONFIG['DATABASE_PATH'])
    if free_disk_space < 500:
        if kittyflap_db_file_exists:
            additional_info = _(" or consider deleting pictures from the original kittyflap database file. For more details, see the 'Info' section.")
        else:
            additional_info = ""
        ui.notification_show(_("Remaining disk space is low: {:.1f} MB. Please free up some space (e.g. reduce the max amount of pictures in the database{}).").format(free_disk_space, additional_info), duration=20, type="warning")

    # Show user notification if available
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

    # Show changelogs, if the version was updated
    changelog_text = get_changelogs(after_version=CONFIG['LAST_READ_CHANGELOGS'], language=CONFIG['LANGUAGE'])
    if changelog_text:
        ui.modal_show(
            ui.modal(
                ui.div(
                    ui.markdown(changelog_text),
                    ui.markdown("\n\n---------\n\n" + _("**NOTE**: You can find the changelogs also in the 'Info' section.")),
                ),
                title=_("Changelog"),
                easy_close=True,
                size="xl",
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

    @reactive.effect
    def ext_trigger_reload_photos():
        reactive.invalidate_later(3)
        if last_imgblock_ts.get_timestamp() != sess_last_imgblock_ts[0]:
            sess_last_imgblock_ts[0] = last_imgblock_ts.get_timestamp()
            reload_trigger_photos.set(reload_trigger_photos.get() + 1)
            logging.info("Reloading photos due to external trigger.")

    @render.text
    def live_view_header():
        reactive.invalidate_later(0.25)
        try:
            inside_lock_state = Magnets.instance.get_inside_state()
            outside_lock_state = Magnets.instance.get_outside_state()

            outside_pir_state, inside_pir_state, motion_outside_raw, motion_inside_raw = Pir.instance.get_states()

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
            outside_pir_state_icon = "🟢" if outside_pir_state else "⚫"
            inside_pir_state_icon = "🟢" if inside_pir_state else "⚫"
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
        try:
            frame = model_hanlder.get_camera_frame()
            if frame is None:
                img_html = (
                    '<div class="placeholder-image">' +
                    '<div></div>' +
                    '<div><strong>' + _('Connection to the camera failed.') + '</strong></div>' +
                    '<div>' + _('If this message does not disappear within 10 seconds, please (re-)install the required camera drivers with the "Reinstall Camera Driver" button in the "System" section.') + '</div>' +
                    '<div></div>' +
                    '</div>'
                )
            else:
                frame_jpg = model_hanlder.encode_jpg_image(frame)
                if frame_jpg:
                    frame_b64 = base64.b64encode(frame_jpg).decode('utf-8')
                    img_html = f'<img src="data:image/jpeg;base64,{frame_b64}" />'
                else:
                    img_html = '<div class="placeholder-image"><strong>' + _('Could not read the picture from the camera.') + '</strong></div>'
        except Exception as e:
            logging.error(f"Failed to fetch the live view image: {e}")
            img_html = '<div class="placeholder-image"><strong>' + _('An error occured while fetching the live view image.') + '</strong></div>'
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
                ui.div(ui.input_switch("button_cat_only", _("Show detected cats only")), class_="col-auto btn-date-filter px-1"),
                ui.div(ui.input_switch("button_mouse_only", _("Show detected mice only")), class_="col-auto btn-date-filter px-1"),
                ui.div(ui.input_switch("button_detection_overlay", _("Show detection overlay"), CONFIG['SHOW_IMAGES_WITH_OVERLAY']), class_="col-auto btn-date-filter px-1"),
                ui.div(ui.input_switch("button_events_view", _("Group pictures to events"), CONFIG['GROUP_PICTURES_TO_EVENTS']), class_="col-auto btn-date-filter px-1"),
                class_="d-flex justify-content-center align-items-center"  # Centers elements horizontally
            ),
            class_="container"  # Adds centering within a smaller container
            )
        return uiDateBar

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
    @reactive.event(input.ui_photos_cards_tabs, input.button_detection_overlay, ignore_none=True)
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

        df_cats = db_get_cats(CONFIG['KITTYHACK_DATABASE_PATH'], ReturnDataCatDB.all_except_photos)

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
                try:
                    cat_name = df_cats.loc[df_cats["rfid"] == data_row["rfid"], "name"].values[0]
                except:
                    cat_name = _("Unknown RFID: {}".format(data_row["rfid"]))
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
                    ui.notification_show(_("An error occurred while deleting the photo: {}").format(result.message), duration=5, type="error")

        if deleted_photos:
            # Reload the dataset
            reload_trigger_photos.set(reload_trigger_photos.get() + 1)
        else:
            ui.notification_show(_("No photos selected for deletion."), duration=5, type="message")

    @output
    @render.ui
    def ui_live_view():
        return ui.div(
            ui.card(
                ui.card_header(
                    ui.output_ui("live_view_header"),
                ),
                ui.output_ui("live_view_main"),
                full_screen=False,
                class_="image-container"
            ),
        )
    
    @output
    @render.ui
    def ui_live_view_footer():
        return ui.div(
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
        ),
    
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
                    min_height="150px"
                ),
                width="400px"
            )
        )
    
    @render.table
    @reactive.event(reload_trigger_photos, ignore_none=True)
    def ui_last_events_table():
        return get_events_table(block_count=25)
    
    @output
    @render.ui
    def ui_events_by_date():
        return ui.layout_column_wrap(
            ui.div(
                ui.card(
                    ui.output_ui("ui_events_by_date_table"),
                    full_screen=False,
                    class_="generic-container",
                    min_height="150px"
                ),
                width="400px"
            )
        )
    
    @render.table
    @reactive.event(input.button_reload, input.date_selector, input.button_cat_only, input.button_mouse_only, reload_trigger_photos, ignore_none=True)
    def ui_events_by_date_table():
        date_start = format_date_minmax(input.date_selector(), True)
        date_end = format_date_minmax(input.date_selector(), False)
        timezone = ZoneInfo(CONFIG['TIMEZONE'])
        # Convert date_start and date_end to timezone-aware datetime strings in the UTC timezone
        date_start = datetime.strptime(date_start, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S%z')
        date_end = datetime.strptime(date_end, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S%z')
        return get_events_table(0, date_start, date_end, input.button_cat_only(), input.button_mouse_only(), CONFIG['MOUSE_THRESHOLD'])

    def get_events_table(block_count = 0, date_start="2020-01-01 00:00:00", date_end="2100-12-31 23:59:59", cats_only=False, mouse_only=False, mouse_probability=0.0):
        try:
            logging.info(f"Reading events from the database for block_count={block_count}, date_start={date_start}, date_end={date_end}, cats_only={cats_only}, mouse_only={mouse_only}, mouse_probability={mouse_probability}")
            df_events = db_get_motion_blocks(CONFIG['KITTYHACK_DATABASE_PATH'], block_count, date_start, date_end, cats_only, mouse_only, mouse_probability)

            if df_events.empty:
                return pd.DataFrame({
                    '': [_('No events found.')]
                })
            # Convert UTC timestamps to local timezone
            df_events['created_at'] = pd.to_datetime(df_events['created_at']).dt.tz_convert(CONFIG['TIMEZONE'])
            df_events = df_events.sort_values(by='created_at', ascending=False)
            df_events['date'] = df_events['created_at'].dt.date
            df_events['time'] = df_events['created_at'].dt.strftime('%H:%M:%S')

            # Replace dates with "Today" and "Yesterday"
            today = datetime.now(ZoneInfo(CONFIG['TIMEZONE'])).date()
            yesterday = today - timedelta(days=1)
            # Convert dates to 'Today', 'Yesterday', or the date string
            # Convert the config date format to Python's strftime format
            date_format = CONFIG['DATE_FORMAT'].lower().replace('yyyy', '%Y').replace('mm', '%m').replace('dd', '%d')
            df_events['date_display'] = df_events['date'].apply(
                lambda date: _("Today") if date == today else (_("Yesterday") if date == yesterday else date.strftime(date_format))
            )

            # Show the cat name instead of the RFID
            cat_name_dict = get_cat_name_rfid_dict(CONFIG['KITTYHACK_DATABASE_PATH'])
            df_events['cat_name'] = df_events['rfid'].apply(lambda rfid: cat_name_dict.get(rfid, _("Unknown RFID") + f": {rfid}" if rfid else _("No RFID found") ))

            # Add a column 'event_pretty' to the events table with icon(s) and a tooltip            
            df_events['event_pretty'] = df_events['event_type'].apply(lambda event_type: ui.tooltip(
                ui.HTML("<div>" + " ".join(str(icon) for icon in EventType.to_icons(event_type)) + "</div>"),
                EventType.to_pretty_string(event_type)
            ))

            # Add an 'inspect' column to the 'events' DataFrame
            inspect_buttons = []
            for index, row in df_events.iterrows():
                unique_id = hashlib.md5(os.urandom(16)).hexdigest()
                inspect_buttons.append(
                    ui.div(btn_show_event(f"btn_show_event_{unique_id}"))
                )
                show_event_server(f"btn_show_event_{unique_id}", row['block_id'])
            df_events['inspect'] = inspect_buttons

            # Create a new DataFrame to hold the formatted events
            formatted_events = pd.DataFrame(columns=['time', 'event', 'cat', 'action'])

            # Iterate through the events and add date rows when the date changes
            last_date = None
            for x, row in df_events.iterrows():
                if row['date_display'] != last_date:
                    # Create a date separator row that spans all columns
                    new_row = pd.DataFrame([{
                        'time': ui.div(
                            row["date_display"], 
                            class_="event-date-separator",
                        ),
                        'event': '',
                        'cat': '',
                        'action': ''
                    }])
                    formatted_events = pd.concat([formatted_events, new_row], ignore_index=True)
                    last_date = row['date_display']
                new_row = pd.DataFrame([{'time': row['time'], 'event': row['event_pretty'], 'cat': row['cat_name'], 'action': row['inspect']}])
                formatted_events = pd.concat([formatted_events, new_row], ignore_index=True)

            return (
                formatted_events.style.set_table_attributes('class="dataframe shiny-table table w-auto"')
                .hide(axis="index")
                .hide(axis="columns")
            )
        except Exception as e:
            logging.error(f"Failed to read events from the database: {e}")
            # Return an empty DataFrame with an error message
            return pd.DataFrame({
                'ERROR': [_('Failed to read events from the database.')]
            })

    @output
    @render.ui
    def ui_system():
            return ui.div(
                ui.column(12, ui.h3(_("Kittyflap System Actions"))),
                ui.column(12, ui.help_text(_("Start tasks/actions on the Kittyflap"))),
                ui.br(),
                ui.br(),
                ui.column(12, ui.input_action_button("bRestartKittyflap", _("Restart Kittyflap"))),
                ui.br(),
                ui.br(),
                ui.column(12, ui.input_action_button("bShutdownKittyflap", _("Shutdown Kittyflap"))),
                ui.column(12, ui.help_text(_("To avoid data loss, always shut down the Kittyflap properly before unplugging the power cable. After a shutdown, wait 30 seconds before unplugging the power cable. To start the Kittyflap again, just plug in the power again."))),
                ui.br(),
                ui.br(),
                ui.column(12, ui.input_task_button("reinstall_camera_driver", _("Reinstall Camera Driver"), icon=icon_svg("rotate-right"), class_="btn-primary")),
                ui.column(12, ui.help_text(_("Reinstall the camera driver if the live view does not work properly."))),
                ui.hr(),
                ui.br(),
                ui.br()
            )
    
    @reactive.Effect
    @reactive.event(input.reinstall_camera_driver)
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
                logging.error(f"An error occurred during the installation process: {e}")
                ui.notification_show(_("An error occurred during the installation process. Please check the logs for details."), duration=None, type="error")

            else:
                logging.info(f"Camera driver reinstallation successful.")
                # Show the restart dialog
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

    @reactive.effect
    @reactive.event(input.btn_modal_reboot_ok)
    def modal_reboot():
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
    @reactive.event(reload_trigger_cats, ignore_none=True)
    def ui_manage_cats():
        ui_cards = []
        df_cats = db_get_cats(CONFIG['KITTYHACK_DATABASE_PATH'], ReturnDataCatDB.all)
        if not df_cats.empty:
            for index, data_row in df_cats.iterrows():
                if data_row["cat_image"]:
                    try:
                        decoded_picture = base64.b64encode(data_row["cat_image"]).decode('utf-8')
                    except:
                        decoded_picture = None
                else:
                    decoded_picture = None

                if decoded_picture:
                    img_html = f'<img style="max-width: 400px !important;" src="data:image/jpeg;base64,{decoded_picture}" />'
                else:
                    img_html = '<div class="placeholder-image"><strong>' + _('No picture found!') + '</strong></div>'

                ui_cards.append(
                    ui.card(
                        ui.card_header(
                            ui.div(
                                ui.column(12, ui.input_text(id=f"mng_cat_name_{data_row['id']}", label=_("Name"), value=data_row['name'], width="100%")),
                                ui.br(),
                                ui.column(12, ui.input_text(id=f"mng_cat_rfid_{data_row['id']}", label=_("RFID"), value=data_row['rfid'], width="100%")),
                                ui.column(12, ui.help_text(_("NOTE: This is NOT the number which stands in the booklet of your vet! You must use the the ID, which is read by the Kittyflap. It is 16 characters long and consists of numbers (0-9) and letters (A-F)."))),
                                ui.column(12, ui.help_text(_("If you have entered the RFID correctly here, the name of the cat will be displayed in the 'Pictures' section."))),
                                ui.br(),
                                ui.column(12, uix.input_file(id=f"mng_cat_pic_{data_row['id']}", label=_("Change Picture"), accept=[".jpg", ".png"], width="100%")),
                            )
                        ),
                        ui.HTML(img_html),
                        ui.card_footer(
                            ui.div(
                                ui.column(12, ui.input_checkbox(id=f"mng_cat_del_{data_row['id']}", label=_("Delete {} from the database").format(data_row['name']), value=False), style_="padding-top: 20px;"),
                            )
                        ),
                        full_screen=False,
                        class_="image-container"
                    )
                )
            
            return ui.div(
                ui.layout_column_wrap(*ui_cards, width="400px"),
                ui.panel_absolute(
                    ui.panel_well(
                        ui.input_action_button(id="mng_cat_save_changes", label=_("Save all changes"), icon=icon_svg("floppy-disk")),
                        style_="background: rgba(240, 240, 240, 0.9); text-align: center;"
                    ),
                    draggable=False, width="100%", left="0px", right="0px", bottom="0px", fixed=True,
                ),
            )
        else:
            ui_cards.append(ui.help_text(_("No cats found in the database. Please go to the 'Add new cat' section to add a new cat.")))

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

                card_name = input[f"mng_cat_name_{db_id}"]()
                card_rfid = input[f"mng_cat_rfid_{db_id}"]()
                card_del = input[f"mng_cat_del_{db_id}"]()

                # Check if the cat should be deleted
                if card_del:
                    updated_cats.append(db_id)
                    result = db_delete_cat_by_id(CONFIG['KITTYHACK_DATABASE_PATH'], db_id)
                    if result.success:
                        ui.notification_show(_("{} deleted successfully from the database.").format(db_name), duration=5, type="message")
                    else:
                        ui.notification_show(_("Failed to delete {} from the database: {}").format(db_name, result.message), duration=5, type="error")
                else:                    
                    # Get image path, if a file was uploaded
                    card_pic: list[FileInfo] | None = input[f"mng_cat_pic_{db_id}"]()
                    if card_pic is not None:
                        card_pic_path = card_pic[0]['datapath']
                    else:
                        card_pic_path = None

                    # Only update the cat data if the values have changed
                    if (db_name != card_name) or (db_rfid != card_rfid) or (card_pic_path is not None):
                        # Add the ID to the list of updated cats
                        updated_cats.append(db_id)

                        result = db_update_cat_data_by_id(CONFIG['KITTYHACK_DATABASE_PATH'], db_id, card_name, card_rfid, card_pic_path)
                        if result.success:
                            ui.notification_show(_("Data for {} updated successfully.").format(card_name), duration=5, type="message")
                        else:
                            ui.notification_show(_("Failed to update cat details: {}").format(result.message), duration=5, type="error")
            
            if not updated_cats:
                ui.notification_show(_("No changes detected. Nothing to save."), duration=5, type="message")
            else:
                reload_trigger_cats.set(reload_trigger_cats.get() + 1)
        
    @output
    @render.ui
    @reactive.event(reload_trigger_cats, ignore_none=True)
    def ui_add_new_cat():
        ui_cards = []
        ui_cards.append(
            ui.card(
                ui.card_header(
                    ui.div(
                        ui.h5(_("Add new cat")),
                        ui.column(12, ui.input_text(id=f"add_new_cat_name", label=_("Name"), value="", width="100%")),
                        ui.br(),
                        ui.column(12, ui.input_text(id=f"add_new_cat_rfid", label=_("RFID"), value="", width="100%")),
                        ui.column(12, ui.help_text(_("You can find the RFID in the 'Pictures' section, if the chip of your cat was recognized by the Kittyflap. To read the RFID, just set the entrance mode to 'All Cats' and let pass your cat through the Kittyflap."))),
                        ui.column(12, ui.help_text(_("NOTE: This is NOT the number which stands in the booklet of your vet! You must use the the ID, which is read by the Kittyflap. It is 16 characters long and consists of numbers (0-9) and letters (A-F)."))),
                        ui.br(),
                        ui.column(12, uix.input_file(id=f"add_new_cat_pic", label=_("Upload Picture"), accept=".jpg", width="100%")),
                        ui.hr(),
                        ui.column(12, ui.input_action_button(id=f"add_new_cat_save", label=_("Save"), icon=icon_svg("floppy-disk"))),
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
        
        # Get image path, if a file was uploaded
        if cat_pic is not None:
            cat_pic_path = cat_pic[0]['datapath']
        else:
            cat_pic_path = None

        result = db_add_new_cat(CONFIG['KITTYHACK_DATABASE_PATH'], cat_name, cat_rfid, cat_pic_path)
        if result.success:
            ui.notification_show(_("New cat {} added successfully.").format(cat_name), duration=5, type="message")
            ui.update_text(id="add_new_cat_name", value="")
            ui.update_text(id="add_new_cat_rfid", value="")
            reload_trigger_cats.set(reload_trigger_cats.get() + 1)
        else:
            ui.notification_show(_("An error occurred while adding the new cat: {}").format(result.message), duration=5, type="error")

    @output
    @render.ui
    @reactive.event(reload_trigger_ai, ignore_none=True)
    def ui_ai_training():

        # Check labelstudio
        if CONFIG["LABELSTUDIO_VERSION"] is not None:
            # Check if labelstudio is running
            if get_labelstudio_status() == True:
                ui_labelstudio = ui.div(
                    ui.row(
                        ui.column(
                            12,
                            ui.input_task_button("btn_labelstudio_stop", _("Stop Label Studio"), icon=icon_svg("stop")),
                            ui.br(),
                            ui.help_text(_("Label Studio is running. Click the button to stop Label Studio.")),
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
                            ui.help_text(_("Latest version: ") + get_labelstudio_latest_version()),
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
                        ui.help_text(_("Label Studio is not running. Click the button to start Label Studio.")),
                        style_="text-align: center;"
                    ),
                )

            if get_labelstudio_latest_version() != CONFIG["LABELSTUDIO_VERSION"]:
                ui_labelstudio = ui_labelstudio, ui.row(
                    ui.column(
                        12,
                        ui.input_task_button("btn_labelstudio_update", _("Update Label Studio"), icon=icon_svg("circle-up"), class_="btn-primary"),
                        ui.br(),
                        ui.help_text(_("Click the button to update Label Studio to the latest version.")),
                        style_="text-align: center;"
                    ),
                    style_ ="padding-top: 50px;"
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
                    _("This is a bit harder, but you are more flexible and the performance is way better. Also, you don't need to worry about the limited disk space on the Kittyflap. See the [Label Studio](https://labelstud.io/) website for instructions.") + "  \n" +
                    _("> **Please note, if you want to install Label Studio on the Kittyflap:**") + "  \n" +
                    _("Some Kittyflaps have only 1GB of RAM. In this case, it is strongly recommended to always stop the Label Studio server after you are done with the labeling process, otherwise the Kittyflap may run out of memory.") + "  \n" +
                    _("You can check the available disk space and the RAM configuration in the 'Info' section.")
                ),
                ui.hr(),
                ui.column(
                    12,
                    ui.input_task_button("btn_labelstudio_install", _("Install Label Studio on the Kittyflap")),
                    ui.br(),
                    ui.help_text(_("Click the button to install Label Studio.")),
                    ui.br(),
                    ui.help_text(_("This will take several minutes, so please be patient.")),
                    ui.br(),
                    ui.help_text(_("The Kittyflap may not be reachable during the installation. This is normal.")),
                    style_="text-align: center;"
                ),
            )

        # Check if a model training is in progress
        training_status = RemoteModelTrainer.check_model_training_result(show_notification=True, show_in_progress=True, return_pretty_status=True)

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
                        _("You can watch this Youtube video for a introduction, how to use Label Studio and train your own model:") + "[YOUTUBE_VIDEO_LINK_TODO](https://www.youtube.com/watch?v=0x0x0x0x0x)"
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
                    ui.div(_("Here you can rename or remove your own models. To activate a model, go to the 'Configuration' section.")),
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
            ui.notification_show(_("Please upload a ZIP file with the Label Studio training data."), duration=5, type="error")
            return

        # Check if the file is a ZIP file
        if not input.model_training_data()[0]['name'].endswith('.zip'):
            ui.notification_show(_("The uploaded file is not a ZIP file. Please upload a valid ZIP file."), duration=5, type="error")
            return

        # Get the uploaded file path
        zip_file_path = input.model_training_data()[0]['datapath']
        model_name = input.model_name()
        email_notification = input.email_notification()
        user_name = input.user_name()

        if email_notification:
            # Validate the email address
            if not re.match(r"[^@]+@[^@]+\.[^@]+", email_notification):
                ui.notification_show(_("Please enter a valid email address."), duration=5, type="error")
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
            _("Do you really want to remove Label Studio?"),
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
        
        # Wait up to 45 seconds for the process to start
        max_wait_time = 45  # seconds
        start_time = tm.time()
        
        with ui.Progress(min=0, max=100) as p:
            p.set(message=_("Starting Label Studio..."), detail=_("Please wait..."))
            
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
        

        ui_config =  ui.div(
            # --- General settings ---
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(_("General settings"), style_="text-align: center;"),
                    ),
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
                        ui.column(4, ui.input_numeric("numElementsPerPage", _("Maximum pictures per page"), CONFIG['ELEMENTS_PER_PAGE'], min=1)),
                        ui.column(
                            8,
                            ui.markdown(
                                _("This setting applies only to the pictures section in the ungrouped view mode.") + "  \n" +
                                _("NOTE: Too many pictures per page could slow down the performance drastically!")
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
                                "> " + _("WARNING: You should keep the TX power as low as possible to avoid interference with the PIR Sensors! You should only increase this value, if you have problems with the WLAN connection.") + "  \n" +
                                "> " + _("NOTE: 0dBm = 1mW, 10dBm = 10mW, 20dBm = 100mW") + "\n\n" +
                                "*(" + _("Default value: {}").format(DEFAULT_CONFIG['Settings']['wlan_tx_power']) + ")*"
                            ), style_="color: grey;"
                        ),
                    ),
                    full_screen=False,
                    class_="generic-container align-left",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),

            # --- Door control settings ---
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(_("Door control settings"), style_="text-align: center;"),
                    ),
                    ui.br(),
                    ui.row(
                        ui.column(4, ui.input_slider("sldMouseThreshold", _("Mouse detection threshold"), min=0, max=100, value=CONFIG['MOUSE_THRESHOLD'])),
                        ui.column(
                            8,
                            ui.markdown(
                                _("Kittyhack decides based on this value, if a picture contains a mouse.") + "  \n" + 
                                _("If the detected mouse probability of a picture exceeds this value, the flap will remain closed.") + "  \n" + 
                                "*(" + _("Default value: {}").format(DEFAULT_CONFIG['Settings']['mouse_threshold']) + ")*"
                            ), style_="color: grey;"
                        ),
                    ),
                    ui.hr(),
                    ui.row(
                        ui.column(4, ui.input_slider("sldMinThreshold", _("Minimum detection threshold"), min=0, max=80, value=CONFIG['MIN_THRESHOLD'])),
                        ui.column(
                            8,
                            ui.markdown(
                                _("This threshold will only be used for the decision, if an motion event on the outside shall be logged or not:") + "\n\n" +
                                _("The detected probability for `Mouse` or `No Mouse` must exceed this threshold at least in one picture of a motion event. Otherwise the pictures will be discarded and the event will not be logged.") + "\n\n" +
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
                        ui.column(4, ui.input_slider("sldLockAfterPreyDetect", _("Lock duration after prey detection (in s)"), min=30, max=600, step=5, value=CONFIG['LOCK_DURATION_AFTER_PREY_DETECTION'])),
                        ui.column(8, ui.markdown(_("The flap will remain closed for this time after a prey detection.")), style_="color: grey;")
                    ),
                    ui.hr(),
                    ui.row(
                        ui.column(12, ui.input_switch("btnDetectPrey", _("Detect prey"), CONFIG['MOUSE_CHECK_ENABLED'])),
                        ui.column(
                            12,
                            ui.markdown(
                                _("If the prey detection is enabled and the mouse detection threshold is exceeded in a picture, the flap will remain closed.") + "  \n" +
                                _("> The zones and the probability of detected mice will be stored independently of this setting for every picture.")
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
                                "- **" + _("Custom Models:") + "** " + _("These are your own trained models, which you have created in the AI Training section.") + "\n\n" +
                                "> " + _("If you change this setting, the Kittyflap must be restarted to apply the new model version.")
                            ), style_="color: grey;"
                        ),
                    ),
                    ui.hr(),
                    ui.row(
                        ui.column(12, ui.input_select(
                            "txtAllowedToEnter",
                            _("Open inside direction for:"),
                            {
                                AllowedToEnter.ALL.value: _("All cats"), AllowedToEnter.ALL_RFIDS.value: _("All cats with a RFID chip"), AllowedToEnter.KNOWN.value: _("Only registered cats"), AllowedToEnter.NONE.value: _("No cats"),
                            },
                            selected=str(CONFIG['ALLOWED_TO_ENTER'].value),
                        )),
                    ),
                    ui.hr(),
                    ui.row(
                        ui.column(12, ui.input_switch("btnAllowedToExit", _("Allow cats to exit"), CONFIG['ALLOWED_TO_EXIT'])),
                        ui.column(12, ui.markdown( _("If this is disabled, the direction to the outside remains closed. Useful for e.g. new year's eve or an upcoming vet visit.")), style_="color: grey;"),
                    ),
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
                                f"""
                                {_("A low value means a fast reaction, but also a higher probability of false alarms. A high value means a slow reaction, but also a lower probability of false alarms.")}  
                                {_("The default setting should be a good value for most cases.")}  
                                *({_("Default value: {}").format(DEFAULT_CONFIG['Settings']['pir_inside_threshold'])})*
                                """
                                ), style_="color: grey;"
                        )
                    ),
                    full_screen=False,
                    class_="generic-container align-left",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),

            # --- Live view settings ---
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(_("Live view settings"), style_="text-align: center;"),
                    ),
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
                width="400px"
            ),

            # --- Pictures view settings ---
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(_("Pictures view settings"), style_="text-align: center;"),
                    ),
                    ui.br(),
                    ui.row(
                        ui.column(4, ui.input_numeric("numMaxPhotosCount", _("Maximum number of photos to retain in the database"), CONFIG['MAX_PHOTOS_COUNT'], min=100)),
                        ui.column(
                            8,
                            ui.markdown(
                                _("The oldest pictures will be deleted if the number of pictures exceeds this value.") + "  \n" +
                                _("The maximum number of pictures depends on the type of the Raspberry Pi, since some kittyflaps are equipped with 16GB and some with 32GB.") + "  \n" +
                                _("As a rule of thumb, you can calculate with 200MB per 1000 pictures. You can check the free disk space in the `Info` section.") + "  \n" +
                                "*(" + _("Default value: {}").format(DEFAULT_CONFIG['Settings']['max_photos_count']) + ")*"
                            ), style_="color: grey;"
                        ),
                    ),
                    ui.br(),
                    full_screen=False,
                    class_="generic-container align-left",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px"
            ),

            # --- Advanced settings ---
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(_("Advanced settings"), style_="text-align: center;"),
                    ),
                    ui.br(),
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
                                _("If this is disabled, only one CPU core will be used for image processing, which results in a slower analysis of the pictures, and therefore also a little bit slower prey detection.") + "  \n" +
                                _("Some users have reported that this option causes reboots or system freezes. If you encounter the same issue, it's recommended to disable this setting.") + "  \n" +
                                _("NOTE: This setting requires a restart of the kittyflap to take effect.")
                            ), style_="color: grey;"
                        ),
                    ),
                    ui.br(),
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
            ui.panel_absolute(
                ui.panel_well(
                    ui.input_action_button(id="bSaveKittyhackConfig", label=_("Save all changes"), icon=icon_svg("floppy-disk")),
                    style_="background: rgba(240, 240, 240, 0.9); text-align: center;"
                ),
                draggable=False, width="100%", left="0px", right="0px", bottom="0px", fixed=True,
            ),
        )
        return ui_config
    
    @reactive.effect
    def update_mouse_threshold_limit():
        # You can update the value, min, max, and step.
        ui.update_slider(
            "sldMouseThreshold",
            value=max(input.sldMouseThreshold(), input.sldMinThreshold()),
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
                    duration=5, type="error"
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
                        duration=5, type="error"
                    )
                    return False
            except ValueError:
                ui.notification_show(
                    _("Allowed to exit time ranges: ") +
                    _("Invalid time value.") + "\n" +
                    _("Changes were not saved."),
                    duration=5, type="error"
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

        # Check if the selected model is a TFLite model or a YOLO model
        if selected_model_changed:
            if (input.selectedModel().startswith("tflite::")):
                CONFIG['TFLITE_MODEL_VERSION'] = input.selectedModel().replace("tflite::", "")
                CONFIG['YOLO_MODEL'] = ""
            elif (input.selectedModel().startswith("yolo::")):
                CONFIG['YOLO_MODEL'] = input.selectedModel().replace("yolo::", "")
                CONFIG['TFLITE_MODEL_VERSION'] = ""
            else:
                CONFIG['YOLO_MODEL'] = ""
                CONFIG['TFLITE_MODEL_VERSION'] = ""
        
        CONFIG['ALLOWED_TO_ENTER'] = AllowedToEnter(input.txtAllowedToEnter())
        CONFIG['LIVE_VIEW_REFRESH_INTERVAL'] = float(input.numLiveViewUpdateInterval())
        CONFIG['ALLOWED_TO_EXIT'] = input.btnAllowedToExit()
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

        # Update the log level
        configure_logging(input.txtLoglevel())

        # Save the configuration to the config file
        _ = set_language(CONFIG['LANGUAGE'])
        
        if save_config():
            ui.notification_show(_("Kittyhack configuration updated successfully."), duration=5, type="message")
            if language_changed:
                ui.notification_show(_("Please restart the kittyflap in the 'System' section, to apply the new language."), duration=30, type="message")

            if selected_model_changed:
                ui.notification_show(_("Please restart the kittyflap in the 'System' section, to apply the new detection model."), duration=30, type="message")
            
            if img_processing_cores_changed:
                ui.notification_show(_("Please restart the kittyflap in the 'System' section, to apply the changed configuration."), duration=30, type="message")
        else:
            ui.notification_show(_("Failed to save the Kittyhack configuration."), duration=5, type="error")

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
            ui.notification_show(_("Failed to update WLAN configuration for {}").format(ssid), duration=5, type="error")
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
            
    
    @render.download()
    def download_logfile():
        return LOGFILE
    
    @render.download()
    def download_journal():
        try:
            with open(JOURNAL_LOG, 'w') as f:
                subprocess.run(["/usr/bin/journalctl", "-u", "kittyhack", "-n", "10000", "--quiet"], stdout=f, check=True)
            return JOURNAL_LOG
        except subprocess.CalledProcessError as e:
            ui.notification_show(_("Failed to create the journal file: {}").format(e), duration=5, type="error")
            return None
        
    @render.download(filename="kittyhack.db")
    def download_kittyhack_db():
        try:
            sigterm_monitor.halt_backend()
            tm.sleep(1.0)
            return CONFIG['KITTYHACK_DATABASE_PATH']
        except Exception as e:
            logging.error(f"Failed to halt the backend processes: {e}")
            ui.notification_show(_("Failed to halt the backend processes: {}").format(e), duration=5, type="error")
            return None
        finally:
            logging.info(f"A download of the kittyhack database was requested --> Restart pending.")
            # Show the restart dialog
            m = ui.modal(
                _("Please click the 'Reboot' button to restart the Kittyflap **after** the download has finished.\n\nNOTE: The download will start in the background, so check your browser's download section."),
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
            ui.notification_show(_("The original kittyflap database file does not exist."), duration=5, type="error")
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
                ui_update_kittyhack = ui.div(
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
                        ui.br(),
                        ui.markdown(
                            _("""
                            ⚠️ WARNING: Local changes detected in the git repository in `/root/kittyhack`.
                            If you proceed with the update, these changes will be lost (the database and configuration will not be affected).
                            Please commit or stash your changes manually before updating, if you want to keep them.
                            """)
                        ),
                        ui.h6(_("Local changes:")),
                        ui.tags.pre(result.stdout)
                    )
                    
            except Exception as e:
                ui_update_kittyhack = ui.markdown(_("An error occurred while checking for local changes in the git repository: {}\n\nNo automatic update possible.").format(e))
        else:
            ui_update_kittyhack = ui.markdown(_("You are already using the latest version of Kittyhack."))

        # Check if the original kittyflap database file still exists
        kittyflap_db_file_exists = os.path.exists(CONFIG['DATABASE_PATH'])
        if kittyflap_db_file_exists:
            if get_file_size(CONFIG['DATABASE_PATH']) > 100:
                ui_kittyflap_db = ui.div(
                    ui.markdown(
                        _("""
                        The original kittyflap database file consumes currently **{:.1f} MB** of disk space.  
                        The file contains a lot pictures which could not be uploaded to the original kittyflap servers anymore.
                        You could delete the pictures from it to free up disk space.  
                        """).format(get_file_size(CONFIG['DATABASE_PATH']))
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
                ui_kittyflap_db = ui.markdown(_("The original kittyflap database seems to be empty. **WARNING:** A downgrade to Kittyhack v1.1.0 will probably not work!"))
        else:
            ui_kittyflap_db = ui.markdown(_("The original kittyflap database file does not exist anymore.\n **WARNING:** A downgrade to Kittyhack v1.1.0 is not possible without the original database file!"))
        return ui.div(
            ui.div(
                ui.card(
                    ui.card_header(ui.h4(_("Information"), style_="text-align: center;")),
                    ui.br(),
                    ui.markdown(
                        _("Kittyhack is an open-source project that enables offline use of the Kittyflap cat door—completely without internet access. "
                        "It was created after the manufacturer of Kittyflap filed for bankruptcy, rendering the associated app non-functional.")
                    ),
                    ui.hr(),
                    ui.markdown(
                        _("**Important Notes**  \n"
                        "I have no connection to the manufacturer of Kittyflap. This project was developed on my own initiative to continue using my Kittyflap.  \n"
                        "Additionally, this project is in an early stage! The planned features are not fully implemented yet, and bugs are to be expected!  \n"
                        "Please report any bugs or feature requests on the GitHub repository.")
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
                    ui.div(ui.download_button("download_kittyhack_db", _("Download Kittyhack Database"), icon=icon_svg("download"))),
                    ui.markdown(
                        _("**WARNING:** To download the database, the Kittyhack software will be stopped and the flap will be locked. "
                        "You have to restart the Kittyflap afterwards to return to normal operation.")
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
                    ui.card_header(ui.h4(_("Logfiles"), style_="text-align: center;")),
                    ui.br(),
                    ui.div(ui.download_button("download_logfile", _("Download Kittyhack Logfile"), icon=icon_svg("download"))),
                    ui.div(ui.download_button("download_journal", _("Download Kittyhack Journal"), icon=icon_svg("download"))),
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
                    ui.notification_show(_("An error occurred while deleting the pictures from the original kittyflap database: {}").format(e), duration=5, type="error")
            else:
                ui.notification_show(_("The original kittyflap database file does not exist anymore."), duration=5, type="message")
        reload_trigger_info.set(reload_trigger_info.get() + 1)

    @reactive.Effect
    @reactive.event(input.update_kittyhack)
    def update_kittyhack_process():        
        with ui.Progress(min=1, max=7) as p:
            p.set(message="Update in progress", detail="This may take a while...")
            i = 0

            latest_version = CONFIG['LATEST_VERSION']
            try:
                # Step 1: Revert local changes, if there are any
                msg = "Reverting local changes"
                i += 1
                p.set(i, message=msg)
                logging.info(msg)
                if not execute_update_step("/bin/git restore .", msg):
                    raise subprocess.CalledProcessError(1, "git restore .")
                if not execute_update_step("/bin/git clean -fd", msg):
                    raise subprocess.CalledProcessError(1, "git clean -fd")

                # Step 2: Update the git repository to the latest tagged version
                msg = f"Updating Kittyhack to the latest version {latest_version}"
                i += 1
                p.set(i, message=msg)
                logging.info(msg)
                if not execute_update_step("/bin/git fetch --all --tags", msg):
                    raise subprocess.CalledProcessError(1, "git fetch")
                
                # Step 3: Check out the latest version
                msg = f"Checking out the latest version {latest_version}"
                i += 1
                p.set(i, message=msg)
                logging.info(msg)
                if not execute_update_step(f"/bin/git checkout {latest_version}", msg):
                    raise subprocess.CalledProcessError(1, f"git checkout {latest_version}")
                
                # Step 4: Update the python dependencies
                msg = "Updating the python dependencies"
                i += 1
                p.set(i, message=msg)
                logging.info(msg)
                if not execute_update_step("/bin/bash -c 'source /root/kittyhack/.venv/bin/activate && pip install --timeout 120 --retries 10 -r /root/kittyhack/requirements.txt'", msg):
                    raise subprocess.CalledProcessError(1, "pip install")
                
                # Step 5: Update the systemd service file
                msg = "Updating the systemd service file"
                i += 1
                p.set(i, message=msg)
                logging.info(msg)
                if not execute_update_step("/bin/cp /root/kittyhack/setup/kittyhack.service /etc/systemd/system/kittyhack.service", msg):
                    raise subprocess.CalledProcessError(1, "cp kittyhack.service")
                
                # Step 6: Reload the systemd daemon
                msg = "Reloading the systemd daemon"
                i += 1
                p.set(i, message=msg)
                logging.info(msg)
                if not execute_update_step("/bin/systemctl daemon-reload", msg):
                    raise subprocess.CalledProcessError(1, "systemctl daemon-reload")

            except subprocess.CalledProcessError as e:
                msg = f"An error occurred during the update process: {e}"
                logging.error(msg)
                ui.notification_show(msg, duration=None, type="error")

                # Rollback Step 1: Go back to the previous version
                msg = f"Rolling back to the previous version {git_version}"
                i = max(i - 1, 1)
                p.set(i, message=msg)
                logging.info(msg)
                execute_update_step(f"/bin/git checkout {git_version}", msg)

                # Rollback Step 2: Update the python dependencies
                msg = "Rolling back the python dependencies"
                i = max(i - 1, 1)
                p.set(i, message=msg)
                logging.info(msg)
                execute_update_step("/bin/bash -c 'source /root/kittyhack/.venv/bin/activate && pip install --timeout 120 --retries 10 -r /root/kittyhack/requirements.txt'", msg)

                # Rollback Step 3: Update the systemd service file
                msg = "Rolling back the systemd service file"
                i = max(i - 1, 1)
                p.set(i, message=msg)
                logging.info(msg)
                execute_update_step("/bin/cp /root/kittyhack/setup/kittyhack.service /etc/systemd/system/kittyhack.service", msg)

                # Rollback Step 4: Reload the systemd daemon
                msg = "Rolling back the systemd daemon"
                i = max(i - 1, 1)
                p.set(i, message=msg)
                logging.info(msg)
                execute_update_step("/bin/systemctl daemon-reload", msg)

                # Notify the user about the error
                ui.notification_show(f"Rollback to {git_version} complete. Please check the logs for details.", duration=None, type="warning")

            else:
                logging.info(f"Kittyhack updated successfully to version {latest_version}.")
                # Show the restart dialog
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


