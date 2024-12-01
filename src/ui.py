from shiny import ui
from faicons import icon_svg
from src.helper import *

# Prepare gettext for translations
set_language(CONFIG['LANGUAGE'])

# the main kittyhack ui
app_ui = ui.page_fillable(
    ui.include_css("styles.css"),
    ui.navset_bar(
        ui.nav_panel(
            _("Pictures"),
            ui.output_ui("ui_photos_date"),
            ui.output_ui("ui_photos_cards_nav"),
            ui.output_ui("ui_photos_cards"),
            ui.br(),
        ),
        ui.nav_panel(
            _("Configuration"),
            ui.output_ui("ui_configuration")
        ),
        ui.nav_panel(
            _("Debug details"),
            ui.output_ui("ui_debuginfo"),
        ),
        title=ui.HTML("<span style='font-weight: bold;'>KITTY " + str(icon_svg("shield-cat")) + "HACK</span>"),
    ),
)