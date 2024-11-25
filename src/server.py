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
from src.helper import *
from src.database import *
from src.system import *

# LOGFILE SETUP
# Convert the log level string from the configuration to the corresponding logging level constant
loglevel = logging._nameToLevel.get(CONFIG['LOGLEVEL'], logging.INFO)

# Create a rotating file handler for logging
# This handler will create log files with a maximum size of 10 MB each and keep up to 5 backup files
handler = RotatingFileHandler(LOGFILE, maxBytes=10*1024*1024, backupCount=5)

# Define the format for log messages
formatter = logging.Formatter('%(asctime)s.%(msecs)d [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
handler.setFormatter(formatter)

# Get the root logger and set its level and handler
logger = logging.getLogger()
logger.setLevel(loglevel)
logger.addHandler(handler)

# Prepare gettext for translations based on the configured language
set_language(CONFIG['LANGUAGE'])

logging.info("----- Startup -----------------------------------------------------------------------------------------")
logging.info("Application has started successfully.")

# Log all configuration values from CONFIG dictionary
logging.info("Configuration values:")
for key, value in CONFIG.items():
    logging.info(f"{key}={value}")

# Validate timezone
try:
    local_timezone = ZoneInfo(CONFIG['TIMEZONE'])
except Exception:
    logging.error(f"Unknown timezone '{CONFIG['TIMEZONE']}'. Falling back to UTC.")
    local_timezone = ZoneInfo('UTC')

# Check, if the kittyhack database file exists. If not, create it.
if not os.path.exists(CONFIG['KITTYHACK_DATABASE_PATH']):
    logging.info(f"Database '{CONFIG['KITTYHACK_DATABASE_PATH']}' not found. Creating it...")
    create_kittyhack_photo_table(CONFIG['KITTYHACK_DATABASE_PATH'])

# Background task in a separate thread
def start_background_task():
    def run_periodically():
        while True:
            logging.info(f"[TRIGGER: background task] Check and transfer new photos from kittyflap db to kittyhack db")
            db_duplicate_photos(src_database=CONFIG['DATABASE_PATH'],
                                dst_database=CONFIG['KITTYHACK_DATABASE_PATH'],
                                dst_max_photos=CONFIG['MAX_PHOTOS_COUNT']
            )
            logging.info("[TRIGGER: background task] Check and transfer done")

            # Perform VACUUM only once a day
            last_vacuum_date = datetime.now().date()
            if last_vacuum_date != getattr(run_periodically, 'last_vacuum_date', None):
                logging.info("[TRIGGER: background task] VACUUM the kittyhack database")
                write_stmt_to_database(CONFIG['KITTYHACK_DATABASE_PATH'], "VACUUM")
                run_periodically.last_vacuum_date = last_vacuum_date
            tm.sleep(CONFIG['PERIODIC_JOBS_INTERVAL'])

    thread = threading.Thread(target=run_periodically, daemon=True)
    thread.start()

# Start the background task
start_background_task()

# The main server application
def server(input, output, session):

    # Create a reactive trigger
    reload_trigger = reactive.Value(0)

    # List of deleted photo IDs
    deleted_ids = []

    @reactive.Effect
    def immediately_sync_photos():
        logging.info("[TRIGGER: site load] Check and transfer new photos from kittyflap db to kittyhack db")
        db_duplicate_photos(src_database=CONFIG['DATABASE_PATH'],
                            dst_database=CONFIG['KITTYHACK_DATABASE_PATH'],
                            dst_max_photos=CONFIG['MAX_PHOTOS_COUNT']
        )
        logging.info("[TRIGGER: site load] Check and transfer done")

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
            #reload_trigger.set(reload_trigger.get() + 1)

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
            #reload_trigger.set(reload_trigger.get() + 1)

    @reactive.Effect
    @reactive.event(input.button_today, ignore_none=True)
    def reset_ui_photos_date():
        # Get the current date
        now = datetime.now()
        session.send_input_message("date_selector", {"value": now.strftime("%Y-%m-%d")})
        #reload_trigger.set(reload_trigger.get() + 1)
    
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
    @reactive.event(input.button_reload, input.date_selector, input.ui_photos_cards_tabs, input.button_mouse_only, input.button_cat_only, reload_trigger, ignore_none=True)
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
            df_photos["created_at"] = pd.to_datetime(df_photos["created_at"]).dt.tz_convert(local_timezone)
            df_cats = db_get_cats(CONFIG['DATABASE_PATH'], ReturnDataCatDB.all)

            for index, data_row in df_photos.iterrows():
                blob_picture = base64.b64encode(data_row["blob_picture"]).decode('utf-8')
                mouse_probability = data_row["mouse_probability"]

                try:
                    photo_timestamp = data_row["created_at"].strftime('%H:%M:%S')
                except ValueError:
                    photo_timestamp = "Unknown date"
                
                if data_row["rfid"]:
                    try:
                        cat_name = df_cats.loc[df_cats["rfid"] == data_row["rfid"], "name"].values[0]
                    except ValueError:
                        cat_name = data_row["rfid"]
                else:
                    cat_name = _("No cat detected.")

                data_uri = f"data:image/jpeg;base64,{blob_picture}"
                if blob_picture:
                    img_html = f'<img src="{data_uri}" />'
                else:
                    img_html = _('No picture found!')
                    logging.warning(f"No blob_picture found for entry {photo_timestamp}")
                
                ui_cards.append(
                         ui.card(
                            ui.card_header(f"{photo_timestamp} | {data_row['id']}"),
                            ui.HTML(img_html),
                            ui.card_footer(
                                ui.div(
                                    ui.HTML(_("Mouse probability: {:.1f}% | Cat: {}").format(mouse_probability, cat_name)),
                                    ui.input_action_button(f"delete_photo_{data_row['id']}", "", icon=icon_svg("trash"), class_="btn-delete-photo",)
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
                            ui.notification_show(_(f"Photo {photo_id} deleted successfully."), duration=5, type="message")
                        else:
                            ui.notification_show(_("An error occurred while deleting the photo: {}").format(result.message), duration=5, type="error")

                        # Reload the images by updating button_reload
                        reload_trigger.set(reload_trigger.get() + 1)
                        break

            return ui.layout_columns(*ui_cards)

    @output
    @render.ui
    def ui_configuration():
        df_config = db_get_config(CONFIG['DATABASE_PATH'], ReturnDataConfigDB.all)
        if not df_config.empty:
            accept_all_cats = bool(df_config.iloc[0]["accept_all_cats"])
            detect_prey = bool(df_config.iloc[0]["detect_prey"])
            acceptance_rate = float(df_config.iloc[0]["acceptance_rate"])
            cat_prob_threshold = float(df_config.iloc[0]["cat_prob_threshold"])
            ui_config =  ui.div(
                ui.column(12, ui.h3(_("Kittyflap configuration"))),
                ui.column(12, ui.help_text(_("Change the configuration of the Kittyflap"))),
                ui.br(),
                ui.column(12, btnAcceptAllCats := ui.input_switch("btnAcceptAllCats", _("Accept all cats"), accept_all_cats)),
                ui.column(12, btnDetectPrey := ui.input_switch("btnDetectPrey", _("Detect prey"), detect_prey)),
                ui.column(12, sldAcceptanceRate := ui.input_slider("sldAcceptanceRate", _("Acceptance rate"), min=0, max=100, value=acceptance_rate)),
                ui.column(12, sldCatProbThreshold := ui.input_slider("sldCatProbThreshold", _("Cat probability threshold"), min=0, max=100, value=cat_prob_threshold)),
                ui.input_action_button("bSaveKittyflapConfig", _("Save Kittyflap Config")),
                ui.hr(),
                ui.column(12, ui.h3(_("Kittyhack configuration"))),
                ui.column(12, ui.help_text(_("In this section you can change the behaviour of the Kittyhack user interface"))),
                ui.br(),
                ui.column(12, txtLanguage := ui.input_select("txtLanguage", "Language", {"en":"English", "de":"Deutsch"}, selected=CONFIG['LANGUAGE'])),
                ui.column(12, txtConfigTimezone := ui.input_text("txtConfigTimezone", _("Timezone"), CONFIG['TIMEZONE'])),
                ui.column(12, ui.HTML('<span class="help-block">' + _('See') +  ' <a href="https://en.wikipedia.org/wiki/List_of_tz_database_time_zones" target="_blank">Wikipedia</a> ' + _('for valid timezone strings') + '</span>')),
                ui.br(),
                ui.column(12, txtConfigDateformat := ui.input_text("txtConfigDateformat", _("Date format"), CONFIG['DATE_FORMAT'])),
                ui.column(12, sldMouseThreshold := ui.input_slider("sldMouseThreshold", _("Mouse filter threshold"), min=0, max=100, value=CONFIG['MOUSE_THRESHOLD'])),
                ui.column(12, ui.help_text(_("NOTE: This value has no impact on the mouse detection of the Kittyflap! It is only used for the filter function of the 'Show detected mouse only' button in the pictures view."))),
                ui.br(),
                ui.column(12, numElementsPerPage := ui.input_numeric("numElementsPerPage", _("Maximum pictures per page"), CONFIG['ELEMENTS_PER_PAGE'], min=1)),
                ui.column(12, ui.help_text(_("NOTE: Too many pictures per page could slow down the performance drastically!"))),
                ui.br(),
                ui.column(12, numMaxPhotosCount := ui.input_numeric("numMaxPhotosCount", _("Maximum number of photos to retain in the database"), CONFIG['MAX_PHOTOS_COUNT'], min=100)),
                ui.br(),
                ui.column(12, txtLoglevel := ui.input_select("txtLoglevel", "Loglevel", {"DEBUG": "DEBUG", "INFO": "INFO", "WARN": "WARN", "ERROR": "ERROR", "CRITICAL": "CRITICAL"}, selected=CONFIG['LOGLEVEL'])),
                ui.input_action_button("bSaveKittyhackConfig", _("Save Kittyhack Config")),
                ui.br(),
                ui.br(),
            )
            return ui_config
        else:
            logging.error("Failed to read the configuration from the kittyflap")
            return ui.help_text(_("ERROR: Failed to read the configuration from the kittyflap."))

    @reactive.Effect
    @reactive.event(input.bSaveKittyflapConfig)
    def on_save_kittyflap_config():
        simulate_kittyflap = CONFIG['SIMULATE_KITTYFLAP'].lower() == "true"
        success = systemctl("stop", "kwork", simulate_kittyflap)
        if success:            
            # update the database with the data from the dictionary
            result = db_set_config(CONFIG['DATABASE_PATH'], 
                                   datetime.now(ZoneInfo("UTC")), 
                                   input.sldAcceptanceRate(), 
                                   input.btnAcceptAllCats(), 
                                   input.btnDetectPrey(), 
                                   input.sldCatProbThreshold())
            if result.success:
                ui.notification_show(_("Kittyflap configuration updated successfully."), duration=5, type="message")
            else:
                ui.notification_show(_("An error occurred while updating the database: {}").format(result.message), duration=5, type="error")

        else:
            # Stop kwork service failed
            ui.notification_show(_("An error occurred while stopping the Kittyflap service. The changed configuration was not saved."), duration=5, type="error")

        # Start the kwork process again
        success = systemctl("start", "kwork", simulate_kittyflap)
        if success != True:
            ui.notification_show(_("An error occurred while starting the Kittyflap service. Please restart the Kittyflap manually by unplugging the power cable and plugging it back in."), duration=None, type="error")

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

        loglevel = logging._nameToLevel.get(input.txtLoglevel(), logging.INFO)
        logger.setLevel(loglevel)
        set_language(input.txtLanguage())
        
        if save_config():
            ui.notification_show(_("Kittyhack configuration updated successfully."), duration=5, type="message")
            if language_changed:
                ui.notification_show(_("Reload this website to apply the new language."), duration=5, type="message")
        else:
            ui.notification_show(_("Failed to save the Kittyhack configuration."), duration=5, type="error")

    @output
    @render.ui
    def ui_debuginfo():
        uiDebuginfos = ui.row(
            ui.card(
                ui.card_header("Table 'cat'"),
                ui.div(ui.output_table("ui_table_cat"), style="overflow-x: auto; width: 100%;")
            ),
            ui.card(
                ui.card_header("Table 'config'"),
                ui.div(ui.output_table("ui_table_config"), style="overflow-x: auto; width: 100%;")
            ),
            ui.card(
                ui.card_header("Table 'photo'"),
                ui.div(ui.output_table("ui_table_photo"), style="overflow-x: auto; width: 100%;")
            ),
        )
        return uiDebuginfos

    @output
    @render.table
    def ui_table_photo():
        df = db_get_photos(CONFIG['KITTYHACK_DATABASE_PATH'], ReturnDataPhotosDB.all_except_photos)
        return df.style.set_table_attributes('class="dataframe table shiny-table w-auto table_nobgcolor"')
    
    @output
    @render.table
    def ui_table_cat():
        df = db_get_cats(CONFIG['DATABASE_PATH'], ReturnDataCatDB.all_except_photos)
        return df.style.set_table_attributes('class="dataframe table shiny-table w-auto table_nobgcolor"')
    
    @output
    @render.table
    def ui_table_config():
        df = db_get_config(CONFIG['DATABASE_PATH'], ReturnDataConfigDB.all_except_password)
        return df.style.set_table_attributes('class="dataframe table shiny-table w-auto table_nobgcolor"')
