import pandas as pd
import sqlite3
from datetime import datetime, timedelta
from shiny import render, ui, reactive
import logging
from logging.handlers import RotatingFileHandler
import base64
from zoneinfo import ZoneInfo
from faicons import icon_svg
import math
from src.helper import *
from src.database import *
from src.system import *

# LOGFILE SETUP
LOGFILE = "kittyhack.log"
# Convert the log level string to the corresponding logging level constant
loglevel = logging._nameToLevel.get(CONFIG['LOGLEVEL'], logging.INFO)

# Create a rotating file handler
handler = RotatingFileHandler(LOGFILE, maxBytes=10*1024*1024, backupCount=5)  # 10 MB per file, keep 5 backups
formatter = logging.Formatter('%(asctime)s.%(msecs)d [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
handler.setFormatter(formatter)
logger = logging.getLogger()
logger.setLevel(loglevel)
logger.addHandler(handler)

# Prepare gettext for translations
set_language(CONFIG['LANGUAGE'])

logging.info("----- Startup -----------------------------------------------------------------------------------------")
logging.info(
    f"Read config.ini: timezone={CONFIG['TIMEZONE']}, date_format={CONFIG['DATE_FORMAT']} database_path={CONFIG['DATABASE_PATH']}, "
    f"simulate_kittyflap={CONFIG['SIMULATE_KITTYFLAP']}, mouse_threshold={CONFIG['MOUSE_THRESHOLD']}, "
    f"elements_per_page={CONFIG['ELEMENTS_PER_PAGE']} loglevel={loglevel}"
)

# Validate timezone
try:
    local_timezone = ZoneInfo(CONFIG['TIMEZONE'])
except Exception:
    logging.error(f"Unknown timezone '{CONFIG['TIMEZONE']}'. Falling back to UTC.")
    local_timezone = ZoneInfo('UTC')

def connect_to_db():
    conn = sqlite3.connect(CONFIG['DATABASE_PATH'])
    return conn

# The main server application
def server(input, output, session):
    @output
    @render.ui
    def ui_photos_date():
        uiDateBar = ui.div(
                ui.row(
                    ui.div(button_decrement := ui.input_action_button("button_decrement", "", icon=icon_svg("angle-left"), class_="btn-date-control"), class_="col-auto"),
                    ui.div(date := ui.input_date("date_selector", "", format=CONFIG['DATE_FORMAT']), class_="col-auto"),
                    ui.div(button_increment := ui.input_action_button("button_increment", "", icon=icon_svg("angle-right"), class_="btn-date-control"), class_="col-auto"),
                    ui.div(button_today := ui.input_action_button("button_today", _("Today"), icon=icon_svg("calendar-day"), class_="btn-date-filter"), class_="col-auto"),
                    class_="d-flex justify-content-center align-items-center"  # Centers elements horizontally
                ),
                ui.br(),
                ui.row(
                    ui.div(button_cat_only := ui.input_switch("button_cat_only", _("Show detected cats only")), class_="col-auto btn-date-filter"),
                    ui.div(button_mouse_only := ui.input_switch("button_mouse_only", _("Show detected mice only")), class_="col-auto btn-date-filter"),
                    class_="d-flex justify-content-center align-items-center"  # Centers elements horizontally
                ),
                class_="container"  # Adds centering within a smaller container
            )
        return uiDateBar

    @reactive.Effect
    @reactive.event(input.button_decrement, ignore_none=True)
    def dec_ui_photos_date():
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
        df_photo_ids = db_get_photos(CONFIG['DATABASE_PATH'], ReturnDataPhotosDB.only_ids, date_start, date_end, input.button_cat_only(), input.button_mouse_only(), CONFIG['MOUSE_THRESHOLD'])
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
    def ui_photos_cards():
        ui_cards = []

        current_date = input.date_selector()
        logging.debug(f"Fetching images for {current_date}")

        date_start = format_date_minmax(input.date_selector(), True)
        date_end = format_date_minmax(input.date_selector(), False)
        page_index = int(input.ui_photos_cards_tabs()) - 1
        df_photos = db_get_photos(
            CONFIG['DATABASE_PATH'],
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
                            ui.card_header(photo_timestamp),
                            ui.HTML(img_html),
                            ui.card_footer(
                                _("Mouse probability: {:.1f}% | Cat: {}").format(mouse_probability, cat_name)
                            ),
                            full_screen=True,
                            class_="image-container" + (" image-container-alert" if mouse_probability >= CONFIG['MOUSE_THRESHOLD'] else "")
                        )
                     )
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
            # prepare the data into a dictionary
            data = {
                "updated_at": [datetime.now(ZoneInfo("UTC"))],
                "acceptance_rate": [input.sldAcceptanceRate()],
                "accept_all_cats": [input.btnAcceptAllCats()],
                "detect_prey": [input.btnDetectPrey()],
                "cat_prob_threshold": [input.sldCatProbThreshold()]
            }
            logging.info(f"Writing new kittyflap configuration to 'config' table: {data}")
            
            # update the database with the data from the dictionary
            try:
                with connect_to_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE config
                        SET 
                            updated_at = ?,
                            acceptance_rate = ?,
                            accept_all_cats = ?,
                            detect_prey = ?,
                            cat_prob_threshold = ?
                        WHERE id = (SELECT id FROM config LIMIT 1)
                    """, (data["updated_at"][0], data["acceptance_rate"][0], data["accept_all_cats"][0], data["detect_prey"][0], data["cat_prob_threshold"][0]))
                    conn.commit()
            except Exception as e:
                logging.error(f"An error occurred while updating the database: {e}")
                ui.notification_show(_("An error occurred while updating the database: {}").format(e), duration=5, type="error")

            else:
                # success
                logging.info("Kittyflap configuration updated successfully.")
                ui.notification_show(_("Kittyflap configuration updated successfully."), duration=5, type="message")
        else:
            # stop kwork service failed
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
        df = db_get_photos(CONFIG['DATABASE_PATH'], ReturnDataPhotosDB.all_except_photos)
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
