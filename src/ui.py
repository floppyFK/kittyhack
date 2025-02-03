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
            _("Live view"),
            ui.output_ui("ui_live_view"),
            ui.output_ui("ui_live_view_footer"),
        ),
        ui.nav_panel(
            _("Pictures"),
            ui.output_ui("ui_photos_date"),
            ui.output_ui("ui_photos_cards_nav"),
            ui.output_ui("ui_photos_cards"),
            ui.br(),
        ),
        ui.nav_panel(
            _("Manage cats"),
            ui.output_ui("ui_manage_cats"),
            ui.br(),
        ),
        ui.nav_panel(
            _("Add new cat"),
            ui.output_ui("ui_add_new_cat"),
            ui.br(),
        ),
        ui.nav_panel(
            _("System"),
            ui.output_ui("ui_system")
        ),
        ui.nav_panel(
            _("Configuration"),
            ui.output_ui("ui_configuration")
        ),
        ui.nav_panel(
            _("WLAN Configuration"),
            ui.output_ui("ui_wlan_configured_connections"),
            ui.output_ui("ui_wlan_available_networks"),
        ),
        ui.nav_panel(
            _("Info"),
            ui.output_ui("ui_info")
        ),
        title=ui.HTML("<span style='font-weight: bold;'>KITTY " + str(icon_svg("shield-cat")) + "HACK</span>"),
        position="fixed-top",
        padding="3rem"
    ),
)
