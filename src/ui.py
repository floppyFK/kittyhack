from shiny import ui
from faicons import icon_svg
from pathlib import Path
from src.baseconfig import CONFIG, set_language

js_file = Path(__file__).parent.parent / "www" / "app.js"
server_ui_js_file = Path(__file__).parent.parent / "www" / "server-ui.js"
event_modal_js_file = Path(__file__).parent.parent / "www" / "event-modal.js"
css_file = Path(__file__).parent.parent / "www" / "styles.css"

try:
    _asset_version = str(int(css_file.stat().st_mtime))
except Exception:
    _asset_version = "0"

try:
    _js_version = str(int(js_file.stat().st_mtime))
except Exception:
    _js_version = "0"

try:
    _server_ui_js_version = str(int(server_ui_js_file.stat().st_mtime))
except Exception:
    _server_ui_js_version = "0"

try:
    _event_modal_js_version = str(int(event_modal_js_file.stat().st_mtime))
except Exception:
    _event_modal_js_version = "0"

# Prepare gettext for translations
_ = set_language(CONFIG['LANGUAGE'])

# the main kittyhack ui
app_ui = ui.page_fillable(
    ui.tags.script(src=f"app.js?v={_js_version}", defer=True),
    ui.tags.script(src=f"server-ui.js?v={_server_ui_js_version}", defer=True),
    ui.tags.script(src=f"event-modal.js?v={_event_modal_js_version}", defer=True),
    ui.tags.link(rel="stylesheet", href=f"styles.css?v={_asset_version}"),
    ui.head_content(
        ui.tags.meta(name="theme-color", content="#FFFFFF"),
        ui.tags.link(rel="manifest", href="manifest.json"),
        ui.tags.link(rel="icon", type="image/png", sizes="64x64", href="favicon-64x64.png?v=6"),
        ui.tags.link(rel="icon", type="image/png", sizes="48x48", href="favicon-48x48.png?v=6"),
        ui.tags.link(rel="icon", type="image/png", sizes="32x32", href="favicon-32x32.png?v=6"),
        ui.tags.link(rel="icon", type="image/png", sizes="16x16", href="favicon-16x16.png?v=6"),
        ui.tags.link(rel="apple-touch-icon", sizes="180x180", href="apple-touch-icon.png?v=6"),
        ui.tags.link(rel="icon", type="image/x-icon", href="favicon.ico?v=6"),
    ),
    ui.navset_bar(
        ui.nav_panel(
            _("Live view"),
            ui.output_ui("ui_live_view"),
            ui.output_ui("ui_live_view_footer"),
            ui.output_ui("ui_last_events"),
        ),
        ui.nav_panel(
            _("Pictures"),
            ui.output_ui("ui_photos_date"),
            ui.output_ui("ui_photos_events"),
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
            _("AI Training"),
            ui.output_ui("ui_ai_training"),
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
        title=ui.tags.div(
            {
                "class": "d-flex align-items-center gap-2 flex-wrap navbar-title-wrap",
                "style": "font-weight: bold;"
            },
            ui.HTML("KITTY " + str(icon_svg("shield-cat")) + "HACK"),
            ui.tags.span(
                {"class": "theme-toggle-container"},
                ui.tags.button(
                    {
                        "id": "theme_toggle_button",
                        "type": "button",
                        "class": "btn btn-sm btn-outline-secondary theme-toggle-btn",
                        "aria-label": _("Toggle theme"),
                        "title": _("Toggle theme")
                    },
                    _("Theme: Auto")
                ),
            ),
        ),
        position="fixed-top",
        padding="3rem",
    ),
)
