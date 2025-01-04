import pandas as pd
from datetime import datetime, timedelta
import time as tm
from shiny import render, ui, reactive
import logging
from logging.handlers import RotatingFileHandler
import base64
from zoneinfo import ZoneInfo
from faicons import icon_svg
import math
import threading
import requests
import random
from src.helper import *
from src.database import *
from src.system import *
from src.backend import *

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

# Log all configuration values from CONFIG dictionary
logging.info("Configuration values:")
for key, value in CONFIG.items():
    logging.info(f"{key}={value}")

# Check, if the kittyhack database file exists. If not, create it.
if not os.path.exists(CONFIG['KITTYHACK_DATABASE_PATH']):
    logging.info(f"Database '{CONFIG['KITTYHACK_DATABASE_PATH']}' not found. Creating it...")
    create_kittyhack_events_table(CONFIG['KITTYHACK_DATABASE_PATH'])

if not check_if_table_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events"):
    logging.warning(f"Table 'events' not found in the kittyhack database. Creating it...")
    create_kittyhack_events_table(CONFIG['KITTYHACK_DATABASE_PATH'])

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

logging.info("Starting backend...")
backend_thread = threading.Thread(target=backend_main, args=(CONFIG['SIMULATE_KITTYFLAP'],), daemon=True)
backend_thread.start()

logging.info("Starting frontend...")

# Frontend background task in a separate thread
def start_background_task():
    # Register task in the sigterm_monitor object
    sigterm_monitor.register_task()

    def run_periodically():
        while not sigterm_monitor.stop_now:
            immediate_bg_task("background task")

            # Perform VACUUM only once a day
            last_vacuum_date = datetime.now().date()
            if last_vacuum_date != getattr(run_periodically, 'last_vacuum_date', None):
                logging.info("[TRIGGER: background task] Start VACUUM of the kittyhack database...")
                write_stmt_to_database(CONFIG['KITTYHACK_DATABASE_PATH'], "VACUUM")
                logging.info("[TRIGGER: background task] VACUUM done")
                run_periodically.last_vacuum_date = last_vacuum_date
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

# Read the GIT version
git_version = get_git_version()

# Initialize the frame count for the live view
frame_count = 0

# The main server application
def server(input, output, session):

    reactive_frame_count = reactive.value(0)

    # Create a reactive trigger
    reload_trigger = reactive.Value(0)

    # List of deleted photo IDs
    deleted_ids = []

    @reactive.effect
    def framecount():
        """
        This effect is used to trigger an update of a ui.output every n seconds (based on CONFIG['LIVE_VIEW_REFRESH_INTERVAL']).
        """
        global frame_count
        reactive.invalidate_later(CONFIG['LIVE_VIEW_REFRESH_INTERVAL'])
        frame_count = (frame_count + 1) % 1000000 # reset the frame count after 1000000
        reactive_frame_count.set(frame_count)

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
        CONFIG['IMAGES_WITH_OVERLAY'] = input.button_detection_overlay()
        update_config_images_overlay(input.button_detection_overlay())

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
    def ui_photos_cards_nav():
        ui_tabs = []
        date_start = format_date_minmax(input.date_selector(), True)
        date_end = format_date_minmax(input.date_selector(), False)
        df_photo_ids = db_get_photos(CONFIG['KITTYHACK_DATABASE_PATH'], ReturnDataPhotosDB.only_ids, date_start, date_end, input.button_cat_only(), input.button_mouse_only(), CONFIG['MOUSE_THRESHOLD'])
        try:
            data_elements_count = df_photo_ids.shape[0]
        except:
            data_elements_count = 0
        tabs_count = int(math.ceil(data_elements_count / CONFIG['ELEMENTS_PER_PAGE']))

        if tabs_count > 0:
            for i in range(tabs_count):
                ui_tabs.append(ui.nav_panel(f"{i+1}", ""))
            return ui.navset_tab(*ui_tabs, id="ui_photos_cards_tabs")
        else:
            return ui.div()

    @output
    @render.ui
    @reactive.event(input.button_reload, input.date_selector, input.ui_photos_cards_tabs, input.button_mouse_only, input.button_cat_only, input.button_detection_overlay, reload_trigger, ignore_none=True)
    def ui_photos_cards():
        ui_cards = []

        current_date = input.date_selector()
        logging.debug(f"Fetching images for {current_date}")

        date_start = format_date_minmax(input.date_selector(), True)
        date_end = format_date_minmax(input.date_selector(), False)
        page_index = int(input.ui_photos_cards_tabs()) - 1
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

        if df_photos is None or df_photos.empty:
            logging.info("No pictures for the selected filter criteria found.")
            return ui.help_text(_("No pictures for the selected filter criteria found."), class_="no-images-found")
        
        else:
            df_cats = db_get_cats(CONFIG['KITTYHACK_DATABASE_PATH'], ReturnDataCatDB.all)

            for index, data_row in df_photos.iterrows():
                if input.button_detection_overlay():
                    blob_picture = data_row["modified_image"]
                else:
                    blob_picture = data_row["original_image"]
                try:
                    decoded_picture = base64.b64encode(blob_picture).decode('utf-8')
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
                        cat_name = data_row["rfid"]
                else:
                    cat_name = ""

                card_footer_mouse = f"{icon_svg('magnifying-glass')} {mouse_probability:.1f}%"
                if cat_name:
                    card_footer_cat = f" | {icon_svg('cat')} {cat_name}"
                else:
                    card_footer_cat = ""
                

                if decoded_picture:
                    img_html = f'<img src="data:image/jpeg;base64,{decoded_picture}" />'
                else:
                    img_html = _('No picture found!')
                    logging.warning(f"No blob_picture found for entry {photo_timestamp}")
                
                ui_cards.append(
                         ui.card(
                            ui.card_header(
                                ui.div(
                                    ui.HTML(f"{photo_timestamp} | {data_row['id']}"),
                                    ui.input_action_button(f"delete_photo_{data_row['id']}", "", icon=icon_svg("trash"), class_="btn-delete-photo px-1 btn-no-border", style_="float: right;"),
                                )
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
            
            delete_buttons = [f"delete_photo_{id}" for id in df_photos['id']]

            @reactive.Effect
            @reactive.event(*[input[btn] for btn in delete_buttons], ignore_none=True)
            def delete_photo():
                for btn in delete_buttons:
                    if input[btn]() and btn not in deleted_ids:
                        # Add the ID to the list of deleted IDs
                        deleted_ids.append(btn)

                        photo_id = int(btn.replace("delete_photo_", ""))
                        result = delete_photo_by_id(CONFIG['KITTYHACK_DATABASE_PATH'], photo_id)
                        if result.success:
                            ui.notification_show(_("Photo {} deleted successfully.").format(photo_id), duration=5, type="message")
                        else:
                            ui.notification_show(_("An error occurred while deleting the photo: {}").format(result.message), duration=5, type="error")

                        # Reload the images by updating button_reload
                        reload_trigger.set(reload_trigger.get() + 1)
                        break

            return ui.layout_columns(*ui_cards)

    @output
    @render.ui
    def ui_live_view():
        tmp = reactive_frame_count.get() # keep this to allow a periodic update of the live view

        frame = tflite.get_camera_frame()
        if frame is None:
            img_html = _('Connection to the camera failed.')
        else:
            frame_jpg = tflite.encode_jpg_image(frame)
            if frame_jpg:
                frame_b64 = base64.b64encode(frame_jpg).decode('utf-8')
                img_html = f'<img src="data:image/jpeg;base64,{frame_b64}" />'
            else:
                img_html = _('Could not read the picture from the camera.')

        
        return ui.div(
            ui.card(
                ui.card_header(
                    ui.div(
                        ui.HTML(f"{tmp} | {datetime.now(ZoneInfo(CONFIG['TIMEZONE'])).strftime('%H:%M:%S')}"),
                    )
                ),
                ui.HTML(img_html),
                full_screen=False,
                class_="image-container"
            )
        )

    @output
    @render.ui
    def ui_system():
            return ui.div(
                ui.column(12, ui.h3(_("Kittyflap System Actions"))),
                ui.column(12, ui.help_text(_("Start tasks/actions on the Kittyflap"))),
                ui.br(),
                ui.input_action_button("bRestartKittyflap", _("Restart Kittyflap")),
                ui.hr(),
                ui.br(),
                ui.br()
            )
    
    @reactive.Effect
    @reactive.event(input.bRestartKittyflap)
    def on_action_restart_system():
        success = systemcmd(["/sbin/reboot"], CONFIG['SIMULATE_KITTYFLAP'])
        if success:
            ui.notification_show(_("Kittyflap is rebooting now..."), duration=5, type="message")
        else:
            ui.notification_show(_("An error occurred while rebooting Kittyflap."), duration=5, type="error")

    @output
    @render.ui
    def ui_configuration():
        ui_config =  ui.div(
            ui.column(12, ui.h3(_("Kittyhack configuration"))),
            ui.column(12, ui.help_text(_("In this section you can change the behaviour of the Kittyhack user interface"))),
            ui.br(),

            ui.column(12, ui.h5(_("General settings"))),
            ui.column(12, txtLanguage := ui.input_select("txtLanguage", "Language", {"en":"English", "de":"Deutsch"}, selected=CONFIG['LANGUAGE'])),
            ui.column(12, txtConfigTimezone := ui.input_text("txtConfigTimezone", _("Timezone"), CONFIG['TIMEZONE'])),
            ui.column(12, ui.HTML('<span class="help-block">' + _('See') +  ' <a href="https://en.wikipedia.org/wiki/List_of_tz_database_time_zones" target="_blank">Wikipedia</a> ' + _('for valid timezone strings') + '</span>')),
            ui.br(),
            ui.column(12, txtConfigDateformat := ui.input_text("txtConfigDateformat", _("Date format"), CONFIG['DATE_FORMAT'])),
            ui.br(),
            ui.column(12, numElementsPerPage := ui.input_numeric("numElementsPerPage", _("Maximum pictures per page"), CONFIG['ELEMENTS_PER_PAGE'], min=1)),
            ui.column(12, ui.help_text(_("NOTE: Too many pictures per page could slow down the performance drastically!"))),
            ui.hr(),

            ui.column(12, ui.h5(_("Door control settings"))),
            ui.column(12, sldMouseThreshold := ui.input_slider("sldMouseThreshold", _("Mouse filter threshold"), min=0, max=100, value=CONFIG['MOUSE_THRESHOLD'])),
            ui.column(12, ui.help_text(_("NOTE: Kittyhack decides based on this value, if a picture contains a mouse or not. A higher value means more strict filtering."))),
            ui.br(),
            ui.column(12, btnDetectPrey := ui.input_switch("btnDetectPrey", _("Detect prey"), CONFIG['MOUSE_CHECK_ENABLED'])),
            ui.br(),
            ui.column(12, txtAllowedToEnter := ui.input_select(
                "txtAllowedToEnter",
                _("Open inside direction for:"),
                {
                    AllowedToEnter.ALL.value: _("All cats"), AllowedToEnter.ALL_RFIDS.value: _("All cats with a RFID chip"), AllowedToEnter.KNOWN.value: _("Only registered cats"), AllowedToEnter.NONE.value: _("No cats"),
                },
                selected=str(CONFIG['ALLOWED_TO_ENTER'].value),
            )),
            ui.hr(),

            ui.column(12, ui.h5(_("Live view settings"))),
            ui.column(12, numLiveViewUpdateInterval := ui.input_select(
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
            ui.column(12, ui.help_text(_("NOTE: A high refresh rate could slow down the performance, especially if several users are connected at the same time. Values below 1s require a fast and stable WiFi connection."))),
            ui.hr(),

            ui.column(12, ui.h5(_("Pictures view settings"))),
            ui.column(12, numMaxPhotosCount := ui.input_numeric("numMaxPhotosCount", _("Maximum number of photos to retain in the database"), CONFIG['MAX_PHOTOS_COUNT'], min=100)),
            ui.hr(),

            ui.column(12, ui.h5(_("Advanced settings"))),
            ui.column(12, txtLoglevel := ui.input_select("txtLoglevel", "Loglevel", {"DEBUG": "DEBUG", "INFO": "INFO", "WARN": "WARN", "ERROR": "ERROR", "CRITICAL": "CRITICAL"}, selected=CONFIG['LOGLEVEL'])),
            ui.hr(),

            ui.column(12, ui.h5(_("Manage cats"))),
            # TODO: Add cat configuration here
            ui.column(12, ui.help_text("TODO: Add cat configuration here")),
            ui.br(),

            ui.input_action_button("bSaveKittyhackConfig", _("Save Kittyhack Config")),
            ui.br(),
            ui.br(),
        )
        return ui_config

    @reactive.Effect
    @reactive.event(input.bSaveKittyhackConfig)
    def on_save_kittyhack_config():
        # override the variable with the data from the configuration page
        language_changed = CONFIG['LANGUAGE'] != input.txtLanguage()
        CONFIG['LANGUAGE'] = input.txtLanguage()
        CONFIG['TIMEZONE'] = input.txtConfigTimezone()
        CONFIG['DATE_FORMAT'] = input.txtConfigDateformat()
        CONFIG['MOUSE_THRESHOLD'] = float(input.sldMouseThreshold())
        CONFIG['ELEMENTS_PER_PAGE'] = int(input.numElementsPerPage())
        CONFIG['MAX_PHOTOS_COUNT'] = int(input.numMaxPhotosCount())
        CONFIG['LOGLEVEL'] = input.txtLoglevel()
        CONFIG['MOUSE_CHECK_ENABLED'] = input.btnDetectPrey()
        CONFIG['ALLOWED_TO_ENTER'] = AllowedToEnter(input.txtAllowedToEnter())
        CONFIG['LIVE_VIEW_REFRESH_INTERVAL'] = float(input.numLiveViewUpdateInterval())

        loglevel = logging._nameToLevel.get(input.txtLoglevel(), logging.INFO)
        logger.setLevel(loglevel)
        set_language(input.txtLanguage())
        
        if save_config():
            ui.notification_show(_("Kittyhack configuration updated successfully."), duration=5, type="message")
            if language_changed:
                ui.notification_show(_("Reload this website to apply the new language."), duration=5, type="message")
        else:
            ui.notification_show(_("Failed to save the Kittyhack configuration."), duration=5, type="error")
    
    @render.download()
    def download_logfile():
        return LOGFILE
    
    @output
    @render.ui
    def ui_info():
        # Fetch the latest kittyhack version via the GitHub API
        try:
            response = requests.get("https://api.github.com/repos/floppyFK/kittyhack/releases/latest")
            latest_version = response.json().get("tag_name", "unknown")
        except Exception as e:
            logging.error(f"Failed to fetch the latest version from GitHub: {e}")
            latest_version = "unknown"

        return ui.div(
            ui.h3("Information"),
            ui.p("Kittyhack is an open-source project that enables offline use of the Kittyflap cat door—completely without internet access. It was created after the manufacturer of Kittyflap filed for bankruptcy, rendering the associated app non-functional."),
            ui.h5("Important Notes"),
            ui.p("I have no connection to the manufacturer of Kittyflap. This project was developed on my own initiative to continue using my Kittyflap."),
            ui.p("Additionally, this project is in a very early stage! The planned features are not fully implemented yet, and bugs are to be expected!"),
            ui.br(),
            ui.HTML(f"<center><p><a href='https://github.com/floppyFK/kittyhack' target='_blank'>{icon_svg('square-github')} GitHub Repository</a></p></center>"),
            ui.br(),
            ui.br(),
            ui.HTML(f"<center><p>Current Version: <code>{git_version}</code></p></center>"),            
            ui.HTML(f"<center><p>Latest Version: <code>{latest_version}</code></p></center>"),
            ui.hr(),
            ui.br(),
            ui.div(
                ui.download_button("download_logfile", "Download Logfile"),
                class_="d-flex justify-content-center"
            )
        )
