import pandas as pd
from datetime import datetime, timedelta
import time as tm
from shiny import render, ui, reactive
from shiny.types import FileInfo
import logging
from logging.handlers import RotatingFileHandler
import base64
from zoneinfo import ZoneInfo
from faicons import icon_svg
import math
import threading
import subprocess
import shutil
from src.helper import *
from src.database import *
from src.system import *
from src.backend import *
from src.magnets import Magnets

# LOGFILE SETUP
# Convert the log level string from the configuration to the corresponding logging level constant
loglevel = logging._nameToLevel.get(CONFIG['LOGLEVEL'], logging.INFO)

# Create a rotating file handler for logging
# This handler will create log files with a maximum size of 10 MB each and keep up to 5 backup files
handler = RotatingFileHandler(LOGFILE, maxBytes=10*1024*1024, backupCount=5)

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

# Define the format for log messages
formatter = TimeZoneFormatter('%(asctime)s [%(levelname)s] %(message)s')
handler.setFormatter(formatter)

# Get the root logger and set its level and handler
logger = logging.getLogger()
logger.setLevel(loglevel)
logger.addHandler(handler)

# Prepare gettext for translations based on the configured language
set_language(CONFIG['LANGUAGE'])

logging.info("----- Startup -----------------------------------------------------------------------------------------")

logging.info(f"Current version: {get_git_version()}")

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

# Check, if the kittyhack database file exists. If not, create it.
if not os.path.exists(CONFIG['KITTYHACK_DATABASE_PATH']):
    logging.info(f"Database '{CONFIG['KITTYHACK_DATABASE_PATH']}' not found. Creating it...")
    create_kittyhack_events_table(CONFIG['KITTYHACK_DATABASE_PATH'])

if not check_if_table_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events"):
    logging.warning(f"Table 'events' not found in the kittyhack database. Creating it...")
    create_kittyhack_events_table(CONFIG['KITTYHACK_DATABASE_PATH'])

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

# Wait for internet connectivity and NTP sync
logging.info("Waiting for network connectivity...")
if wait_for_network(timeout=120):
    logging.info("Starting backend...")
else:
    logging.warning("Timeout for network connectivity reached. Proceeding without network connection.")
    logging.info("Starting backend...")
backend_thread = threading.Thread(target=backend_main, args=(CONFIG['SIMULATE_KITTYFLAP'],), daemon=True)
backend_thread.start()

logging.info("Starting frontend...")

# Read the GIT version
git_version = get_git_version()

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
                if not db_backup.success and db_backup.message == "kittyhack_db_corrupted":
                    restore_database_backup(database=CONFIG['KITTYHACK_DATABASE_PATH'], backup_path=CONFIG['KITTYHACK_DATABASE_BACKUP_PATH'])

            # Perform Scheduled VACUUM only if the last scheduled vacuum date is older than 24 hours
            if (datetime.now() - last_vacuum_date) > timedelta(days=1):
                logging.info("[TRIGGER: background task] Start VACUUM of the kittyhack database...")
                write_stmt_to_database(CONFIG['KITTYHACK_DATABASE_PATH'], "VACUUM")
                logging.info("[TRIGGER: background task] VACUUM done")
                CONFIG['LAST_VACUUM_DATE'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                update_single_config_parameter("LAST_VACUUM_DATE")

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

# The main server application
def server(input, output, session):

    # Create reactive triggers
    reload_trigger_photos = reactive.Value(0)
    reload_trigger_cats = reactive.Value(0)
    reload_trigger_info = reactive.Value(0)

    # Show a notification if a new version of Kittyhack is available
    if CONFIG['LATEST_VERSION'] != "unknown" and CONFIG['LATEST_VERSION'] != git_version and CONFIG['PERIODIC_VERSION_CHECK']:
        ui.notification_show(_("A new version of Kittyhack is available: {}. Go to the 'Info' section for update instructions.").format(CONFIG['LATEST_VERSION']), duration=10, type="message")

    # Show a nag screen if the kittyflap database file still exists
    kittyflap_db_file_exists = os.path.exists(CONFIG['DATABASE_PATH'])
    #if kittyflap_db_file_exists and CONFIG['KITTYFLAP_DB_NAGSCREEN']:
    #    ui.notification_show(_("The original kittyflap database file still exists. Please consider deleting the photos in it to free up disk space. For more details, see the 'Info' section (NOTE: You can disable this message in the 'Configuration' section.)"), duration=10, type="warning")

    # Show a warning if the remaining disk space is below the critical threshold
    if free_disk_space < 500:
        if kittyflap_db_file_exists:
            additional_info = _(" or consider deleting pictures from the original kittyflap database file. For more details, see the 'Info' section.")
        else:
            additional_info = ""
        ui.notification_show(_("Remaining disk space is low: {:.1f} MB. Please free up some space (e.g. reduce the max amount of pictures in the database{}).").format(free_disk_space, additional_info), duration=20, type="warning")

    @render.text
    def live_view_header():
        reactive.invalidate_later(0.25)
        try:
            inside_lock_state = Magnets.instance.get_inside_state()
            outside_lock_state = Magnets.instance.get_outside_state()

            outside_pir_state, inside_pir_state = Pir.instance.get_states()

            if inside_lock_state:
                ui.update_action_button("bManualOverride", label=_("Close inside now"), icon=icon_svg("lock"), disabled=False)
            else:
                ui.update_action_button("bManualOverride", label=_("Open inside now"), icon=icon_svg("lock-open"), disabled=False)

            inside_lock_icon = icon_svg('lock-open') if inside_lock_state else icon_svg('lock')
            outside_lock_icon = icon_svg('lock-open') if outside_lock_state else icon_svg('lock')
            outside_pir_state_icon = "🟢" if outside_pir_state else "⚫"
            inside_pir_state_icon = "🟢" if inside_pir_state else "⚫"
            ui_html = ui.HTML(_("<b>Locks:</b> Inside {} | Outside {}<br><b>Motion:</b> Inside {} | Outside {}").format(inside_lock_icon, outside_lock_icon, inside_pir_state_icon, outside_pir_state_icon))
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
            frame = tflite.get_camera_frame()
            if frame is None:
                img_html = '<div class="placeholder-image"><strong>' + _('Connection to the camera failed.') + '</strong></div>'
            else:
                frame_jpg = tflite.encode_jpg_image(frame)
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
                    ui.div(button_decrement := ui.input_action_button("button_decrement", "", icon=icon_svg("angle-left"), class_="btn-date-control"), class_="col-auto px-1"),
                    ui.div(date := ui.input_date("date_selector", "", format=CONFIG['DATE_FORMAT']), class_="col-auto px-1"),
                    ui.div(button_increment := ui.input_action_button("button_increment", "", icon=icon_svg("angle-right"), class_="btn-date-control"), class_="col-auto px-1"),
                    class_="d-flex justify-content-center align-items-center flex-nowrap"
                ),
                ui.div(button_today := ui.input_action_button("button_today", _("Today"), icon=icon_svg("calendar-day"), class_="btn-date-filter"), class_="col-auto px-1"),
                ui.div(button_reload := ui.input_action_button("button_reload", "", icon=icon_svg("rotate"), class_="btn-date-filter"), class_="col-auto px-1"),
                class_="d-flex justify-content-center align-items-center"  # Centers elements horizontally and prevents wrapping
            ),
            ui.br(),
            ui.row(
                ui.div(button_cat_only := ui.input_switch("button_cat_only", _("Show detected cats only")), class_="col-auto btn-date-filter px-1"),
                ui.div(button_mouse_only := ui.input_switch("button_mouse_only", _("Show detected mice only")), class_="col-auto btn-date-filter px-1"),
                ui.div(button_detection_overlay := ui.input_switch("button_detection_overlay", _("Show detection overlay"), CONFIG['SHOW_IMAGES_WITH_OVERLAY']), class_="col-auto btn-date-filter px-1"),
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

        if input.button_detection_overlay():
            picture_type = ReturnDataPhotosDB.all_modified_image
            blob_picture = "modified_image"
        else:
            picture_type = ReturnDataPhotosDB.all_original_image
            blob_picture = "original_image"

        df_photos = db_get_photos(
            CONFIG['KITTYHACK_DATABASE_PATH'],
            picture_type,
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
            try:
                decoded_picture = base64.b64encode(data_row[blob_picture]).decode('utf-8')
            except:
                decoded_picture = None
            
            mouse_probability = data_row["mouse_probability"]

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
                img_html = f'<img src="data:image/jpeg;base64,{decoded_picture}" style="min-width: 250px;" />'
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
                ui.hr(),
                ui.br(),
                ui.br()
            )
    
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
                                ui.column(12, ui.input_file(id=f"mng_cat_pic_{data_row['id']}", label=_("Change Picture"), accept=[".jpg", ".png"], width="100%")),
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
                        ui.column(12, ui.input_file(id=f"add_new_cat_pic", label=_("Upload Picture"), accept=".jpg", width="100%")),
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
    def ui_configuration():
        ui_config =  ui.div(
            ui.column(12, ui.h3(_("Kittyhack configuration"))),
            ui.column(12, ui.help_text(_("In this section you can change the behaviour of the Kittyhack user interface"))),
            ui.br(),

            ui.column(12, ui.h5(_("General settings"))),
            ui.column(12, ui.input_select("txtLanguage", _("Language"), {"en":"English", "de":"Deutsch"}, selected=CONFIG['LANGUAGE'])),
            ui.column(12, ui.input_text("txtConfigTimezone", _("Timezone"), CONFIG['TIMEZONE'])),
            ui.column(12, ui.HTML('<span class="help-block">' + _('See') +  ' <a href="https://en.wikipedia.org/wiki/List_of_tz_database_time_zones" target="_blank">Wikipedia</a> ' + _('for valid timezone strings') + '</span>')),
            ui.br(),
            ui.column(12, ui.input_text("txtConfigDateformat", _("Date format"), CONFIG['DATE_FORMAT'])),
            ui.br(),
            ui.column(12, ui.input_numeric("numElementsPerPage", _("Maximum pictures per page"), CONFIG['ELEMENTS_PER_PAGE'], min=1)),
            ui.column(12, ui.help_text(_("NOTE: Too many pictures per page could slow down the performance drastically!"))),
            ui.br(),
            ui.column(12, ui.input_switch("btnPeriodicVersionCheck", _("Periodic version check"), CONFIG['PERIODIC_VERSION_CHECK'])),
            ui.column(12, ui.help_text(_("Automatically check for new versions of Kittyhack."))),
            ui.br(),
            #ui.column(12, ui.input_switch("btnShowKittyflapDbNagscreen", _("Show nag screen, if the original kittyflap database file exists and has a very large size"), CONFIG['KITTYFLAP_DB_NAGSCREEN'])),
            #ui.hr(),

            ui.column(12, ui.h5(_("Door control settings"))),
            ui.column(12, ui.input_slider("sldMouseThreshold", _("Mouse detection threshold"), min=0, max=100, value=CONFIG['MOUSE_THRESHOLD'])),
            ui.column(12, ui.help_text(_("Kittyhack decides based on this value, if a picture contains a mouse."))),
            ui.br(),
            ui.column(12, ui.input_slider("sldMinThreshold", _("Minimum detection threshold"), min=0, max=80, value=CONFIG['MIN_THRESHOLD'])),
            ui.column(12, ui.help_text(_("Pictures with a detection probability below this value (for both 'Mouse' and 'No Mouse' check) are not saved in the database."))),
            ui.br(),
            ui.column(12, ui.input_numeric("numMinPicturesToAnalyze", _("Minimum pictures before unlock decision"), CONFIG['MIN_PICTURES_TO_ANALYZE'], min=1)),
            ui.column(12, ui.help_text(_("Number of pictures that must be analyzed before deciding to unlock the flap. If a picture exceeds the mouse threshold, the flap will remain closed."))),
            ui.br(),
            ui.column(12, ui.input_switch("btnDetectPrey", _("Detect prey"), CONFIG['MOUSE_CHECK_ENABLED'])),
            ui.br(),
            ui.column(12, ui.input_select(
                "txtAllowedToEnter",
                _("Open inside direction for:"),
                {
                    AllowedToEnter.ALL.value: _("All cats"), AllowedToEnter.ALL_RFIDS.value: _("All cats with a RFID chip"), AllowedToEnter.KNOWN.value: _("Only registered cats"), AllowedToEnter.NONE.value: _("No cats"),
                },
                selected=str(CONFIG['ALLOWED_TO_ENTER'].value),
            )),
            ui.br(),
            ui.column(12, ui.input_switch("btnAllowedToExit", _("Allow cats to exit"), CONFIG['ALLOWED_TO_EXIT'])),
            ui.column(12, ui.help_text(_("If this is set to 'No', the direction to the outside remains closed. Useful for e.g. new year's eve or an upcoming vet visit."))),
            ui.br(),
            # TODO: Outside PIR shall not yet be configurable. Need to redesign the camera control, otherwise we will have no cat pictures at high PIR thresholds.
            #ui.column(12, ui.input_slider("sldPirOutsideThreshold", _("Sensitivity of the motion sensor on the outside"), min=0.1, max=6, step=0.1, value=CONFIG['PIR_OUTSIDE_THRESHOLD'])),
            ui.column(12, ui.input_slider("sldPirInsideThreshold", _("Reaction speed (in s) of the motion sensor on the inside"), min=0.1, max=6, step=0.1, value=CONFIG['PIR_INSIDE_THRESHOLD'])),
            ui.column(12, ui.help_text(_("A low value means a fast reaction, but also a higher probability of false alarms. A high value means a slow reaction, but also a lower probability of false alarms."))),
            ui.column(12, ui.help_text(_("NOTE: The motion sensor on the outside is not yet configurable. This will be implemented soon."))),
            ui.hr(),

            ui.column(12, ui.h5(_("Live view settings"))),
            ui.column(12, ui.input_select(
                "numLiveViewUpdateInterval",
                _("Live-View update interval:"),
                {
                    _("Refresh the live view every..."):
                    {
                        0.1: "100ms", 0.2: "200ms", 0.5: "500ms", 1.0: "1s", 2.0: "2s", 3.0: "3s", 5.0: "5s", 10.0: "10s"
                    },
                },
                selected=CONFIG['LIVE_VIEW_REFRESH_INTERVAL'],
            )),
            ui.column(12, ui.help_text(_("NOTE: A high refresh rate could slow down the performance, especially if several users are connected at the same time. Values below 1s require a fast and stable WLAN connection."))),
            ui.column(12, ui.help_text(_("This setting affects only the view in the WebUI and has no impact on the detection process."))),
            ui.hr(),

            ui.column(12, ui.h5(_("Pictures view settings"))),
            ui.column(12, ui.input_numeric("numMaxPhotosCount", _("Maximum number of photos to retain in the database"), CONFIG['MAX_PHOTOS_COUNT'], min=100)),
            ui.hr(),

            ui.column(12, ui.h5(_("Advanced settings"))),
            ui.column(12, ui.input_select("txtLoglevel", "Loglevel", {"DEBUG": "DEBUG", "INFO": "INFO", "WARN": "WARN", "ERROR": "ERROR", "CRITICAL": "CRITICAL"}, selected=CONFIG['LOGLEVEL'])),
            ui.br(),

            #ui.input_action_button("bSaveKittyhackConfig", _("Save Kittyhack Config")),
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
        # override the variable with the data from the configuration page
        language_changed = CONFIG['LANGUAGE'] != input.txtLanguage()
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
        CONFIG['ALLOWED_TO_ENTER'] = AllowedToEnter(input.txtAllowedToEnter())
        CONFIG['LIVE_VIEW_REFRESH_INTERVAL'] = float(input.numLiveViewUpdateInterval())
        CONFIG['ALLOWED_TO_EXIT'] = input.btnAllowedToExit()
        CONFIG['PERIODIC_VERSION_CHECK'] = input.btnPeriodicVersionCheck()
        #CONFIG['KITTYFLAP_DB_NAGSCREEN'] = input.btnShowKittyflapDbNagscreen()
        # TODO: Outside PIR shall not yet be configurable. Need to redesign the camera control, otherwise we will have no cat pictures at high PIR thresholds.
        #CONFIG['PIR_OUTSIDE_THRESHOLD'] = 10-int(input.sldPirOutsideThreshold())
        CONFIG['PIR_INSIDE_THRESHOLD'] = float(input.sldPirInsideThreshold())

        loglevel = logging._nameToLevel.get(input.txtLoglevel(), logging.INFO)
        logger.setLevel(loglevel)
        set_language(input.txtLanguage())
        
        if save_config():
            ui.notification_show(_("Kittyhack configuration updated successfully."), duration=5, type="message")
            if language_changed:
                ui.notification_show(_("Please restart the kittyflap in the 'System' section, to apply the new language."), duration=15, type="message")
        else:
            ui.notification_show(_("Failed to save the Kittyhack configuration."), duration=5, type="error")
    
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
        # Try to acquire database lock
        result = lock_database()
        if not result.success:
            logging.error(f"Failed to lock database: {result.message}")
            return None
        else:
            try:
                if os.path.exists("/tmp/kittyhack.db"):
                    os.remove("/tmp/kittyhack.db")

                # Check if there is enough free disk space to copy the database
                kittyhack_db_size = get_database_size()
                free_disk_space = get_free_disk_space()
                required_space = kittyhack_db_size + 250
                if free_disk_space < required_space:
                    ui.notification_show(_("Not enough free disk space to download the database. Required: {} MB, Free: {} MB").format(required_space, free_disk_space), duration=5, type="error")
                    return None
                
                # Copy the database file to /tmp
                shutil.copy2(CONFIG['KITTYHACK_DATABASE_PATH'], "/tmp/kittyhack.db")
                return "/tmp/kittyhack.db"
            except Exception as e:
                logging.error(f"Failed to copy database file: {e}")
                ui.notification_show(_("Failed to copy the database file: {}").format(e), duration=5, type="error")
                return None
            finally:
                # Release the database lock
                release_database()
    
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
            ui_update_kittyhack = ui.markdown("Unable to fetch the latest version from github. Please try it again later or check your internet connection.")
        elif git_version != latest_version:
            # Check for local changes in the git repository
            try:
                ui_update_kittyhack = ui.div(
                    ui.markdown(f"Automatic update to **{latest_version}**:"),
                    ui.input_task_button("update_kittyhack", "Update Kittyhack", icon=icon_svg("download"), class_="btn-primary"),
                    ui.br(),
                    ui.help_text("Please note: A stable WLAN connection is required for the update process."),
                    ui.br(),
                    ui.help_text("The update will end with a reboot of the Kittyflap."),
                    ui.markdown("Check out the [Changelog](https://github.com/floppyFK/kittyhack/releases) to see what's new in the latest version."),
                )

                # Check for local changes in the git repository and warn the user
                result = subprocess.run(["/bin/git", "status", "--porcelain"], capture_output=True, text=True, check=True)
                if result.stdout.strip():
                    # Local changes detected
                    result = subprocess.run(["/bin/git", "status"], capture_output=True, text=True, check=True)
                    ui_update_kittyhack = ui_update_kittyhack, ui.div(
                        ui.br(),
                        ui.markdown(
                            """
                            ⚠️ WARNING: Local changes detected in the git repository in `/root/kittyhack`.
                            If you proceed with the update, these changes will be lost (the database and configuration will not be affected).
                            Please commit or stash your changes manually before updating, if you want to keep them.
                            """
                        ),
                        ui.h6("Local changes:"),
                        ui.tags.pre(result.stdout)
                    )
                    
            except Exception as e:
                ui_update_kittyhack = ui.markdown(f"An error occurred while checking for local changes in the git repository: {e}\n\nNo automatic update possible.")
        else:
            ui_update_kittyhack = ui.markdown("You are already using the latest version of Kittyhack.")

        # Check if the original kittyflap database file still exists
        kittyflap_db_file_exists = os.path.exists(CONFIG['DATABASE_PATH'])
        if kittyflap_db_file_exists:
            if get_file_size(CONFIG['DATABASE_PATH']) > 100:
                ui_kittyflap_db = ui.div(
                    ui.markdown(
                        f"""
                        The original kittyflap database file consumes currently **{get_file_size(CONFIG['DATABASE_PATH']):.1f} MB** of disk space.  
                        The file contains a lot pictures which could not be uploaded to the original kittyflap servers anymore.
                        You could delete the pictures from it to free up disk space.  
                        """
                    ),
                    ui.input_task_button("clear_kittyflap_db", "Remove pictures from original Kittyflap Database", icon=icon_svg("trash")),
                    ui.download_button("download_kittyflap_db", "Download Kittyflap Database"),
                )
            elif get_file_size(CONFIG['DATABASE_PATH']) > 0:
                    ui_kittyflap_db = ui.div(
                        ui.markdown(f"The original kittyflap database file exists and has a regular size of **{get_file_size(CONFIG['DATABASE_PATH']):.1f} MB**. Nothing to do here."),
                        ui.download_button("download_kittyflap_db", "Download Kittyflap Database"),
                    )
            else:
                ui_kittyflap_db = ui.markdown("The original kittyflap database seems to be empty. **WARNING:** A downgrade to Kittyhack v1.1.0 will probably not work!")
        else:
            ui_kittyflap_db = ui.markdown("The original kittyflap database file does not exist anymore.\n **WARNING:** A downgrade to Kittyhack v1.1.0 is not possible without the original database file!")

        # Render the UI
        return ui.div(
            ui.h3("Information"),
            ui.p("Kittyhack is an open-source project that enables offline use of the Kittyflap cat door—completely without internet access. It was created after the manufacturer of Kittyflap filed for bankruptcy, rendering the associated app non-functional."),
            ui.h5("Important Notes"),
            ui.p("I have no connection to the manufacturer of Kittyflap. This project was developed on my own initiative to continue using my Kittyflap."),
            ui.p("Additionally, this project is in a early stage! The planned features are not fully implemented yet, and bugs are to be expected!"),
            ui.p("Please report any bugs or feature requests on the GitHub repository."),
            ui.br(),
            ui.HTML(f"<center><p><a href='https://github.com/floppyFK/kittyhack' target='_blank'>{icon_svg('square-github')} GitHub Repository</a></p></center>"),
            ui.hr(),
            ui.h5("Version Information"),
            ui.HTML(f"<center><p>Current Version: <code>{git_version}</code></p></center>"),            
            ui.HTML(f"<center><p>Latest Version: <code>{latest_version}</code></p></center>"),
            ui_update_kittyhack,
            ui.hr(),
            ui.h5("System Information"),
            ui.markdown(
                f"""
                ###### Filesystem:
                - **Free disk space:** {get_free_disk_space():.1f} MB / {get_total_disk_space():.1f} MB
                - **Database size:** {get_database_size():.1f} MB
                """
            ),
            ui.download_button("download_kittyhack_db", "Download Kittyhack Database"),
            ui.markdown("NOTE: Click the download button just once. Depending on the size of the database, it may take a while until the download starts."),
            ui.br(),
            ui.output_ui("wlan_info"),
            ui.hr(),
            ui.h5("Original Kittyflap Database"),
            ui_kittyflap_db,
            ui.hr(),
            ui.h5("Logfiles"),
            ui.download_button("download_logfile", "Download Kittyhack Logfile"),
            ui.br(),
            ui.br(),
            ui.download_button("download_journal", "Download Kittyhack Journal"),
            ui.br(),
            ui.br(),
            ui.br(),
        )
    
    @render.text
    def wlan_info():
        # Get WLAN connection status
        reactive.invalidate_later(5.0)
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
                
                return ui.markdown(f"###### WLAN Connection Status:\n- **Link Quality:** {wlan_icon} {quality}\n- **Signal Level:** {signal} dBm")
        except:
            return ui.markdown("###### WLAN Connection Status:\n Unable to determine")

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


