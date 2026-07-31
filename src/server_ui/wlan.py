"""WLAN configuration tab."""

import os
import pandas as pd
from shiny import render, ui, reactive
import logging
from faicons import icon_svg
import hashlib
from src.baseconfig import CONFIG, set_language
from src.system import WlanManager
from src.mode import is_remote_mode
from src.server_ui.state import reload_trigger_wlan, _set_wlan_action_in_progress
from src.server_ui.helpers import wlan_add_dialog
from src.server_ui.context import SessionContext

_ = set_language(CONFIG["LANGUAGE"])

if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir

from src.server_ui.wlan_modules import (
    btn_wlan_modify,
    btn_wlan_connect,
    wlan_connect_server,
    wlan_modify_server,
)


def register_wlan(input, output, session, ctx: SessionContext):
    """Register WLAN configuration tab handlers."""

    @output
    @render.ui
    def ui_wlan_configured_connections():
        return ui.layout_column_wrap(
            ui.div(
                ui.card(
                    ui.card_header(ui.p(_("Configured WLANs"))),
                    ui.HTML(
                        '<div id="pleasewait_wlan_configured" class="spinner-container"><div class="spinner"></div></div>'
                    ),
                    ui.output_ui("configured_wlans_table"),
                    ui.hr(),
                    ui.input_action_button(
                        id="btn_wlan_add",
                        label=_("Add new WLAN"),
                        icon=icon_svg("plus"),
                    ),
                    full_screen=False,
                    class_="generic-container",
                    min_height="150px",
                ),
                width="400px",
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

            configured_wlans = WlanManager.get_wlan_connections()
            i = 0
            for wlan in configured_wlans:
                unique_id = hashlib.md5(os.urandom(16)).hexdigest()
                if wlan["connected"]:
                    wlan["connected_icon"] = _table_status_icon(
                        "circle-check", "text-success", _("Connected")
                    )
                else:
                    wlan["connected_icon"] = _table_status_icon(
                        "circle", "text-muted", _("Not connected")
                    )
                wlan["actions"] = ui.div(
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
            df = df[
                ["ssid", "priority", "connected_icon", "actions"]
            ]  # Select only the columns we want to display
            df.columns = ["SSID", _("Priority"), "", ""]  # Rename columns for display

            return df.style.set_table_attributes(
                'class="dataframe shiny-table table w-auto"'
            ).hide(axis="index")
        except Exception as e:
            logging.error(f"Failed to scan for available WLANs: {e}")
            # Return an empty DataFrame with an error message
            return pd.DataFrame({"ERROR": [_("Failed to scan for available WLANs")]})
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
        logging.info(
            f"Updating WLAN configuration: SSID={ssid}, Priority={priority}, Password changed={password_changed}"
        )
        ui.modal_remove()
        ui.modal_show(
            ui.modal(
                _("The WLAN connection will be interrupted now!"),
                ui.br(),
                _(
                    "Please wait a few seconds. If the page does not reload automatically within 30 seconds, please reload it manually."
                ),
                title=_("Updating WLAN configuration..."),
                footer=None,
            )
        )
        try:
            success = WlanManager.manage_and_switch_wlan(
                ssid, password, priority, password_changed
            )
            if success:
                ui.notification_show(
                    _("WLAN configuration for {} updated successfully.").format(ssid),
                    duration=5,
                    type="message",
                )
                reload_trigger_wlan.set(reload_trigger_wlan.get() + 1)
            else:
                ui.notification_show(
                    _("Failed to update WLAN configuration for {}").format(ssid),
                    duration=10,
                    type="error",
                )
        finally:
            _set_wlan_action_in_progress(False)
            ui.modal_remove()

    @output
    @render.ui
    def ui_wlan_available_networks():
        return ui.layout_column_wrap(
            ui.div(
                ui.card(
                    ui.card_header(ui.p(_("Available WLANs"))),
                    ui.HTML(
                        '<div id="pleasewait_wlan_scan" class="spinner-container"><div class="spinner"></div></div>'
                    ),
                    ui.output_ui("scan_wlan_results_table"),
                    full_screen=False,
                    class_="generic-container",
                    min_height="150px",
                ),
                width="400px",
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

            available_wlans = WlanManager.scan_wlan_networks()
            for wlan in available_wlans:
                signal_strength = wlan["bars"]
                signal_percent = int(wlan.get("signal") or 0)
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

                wlan["signal_icon"] = ui.span(
                    f"{signal_percent}% ",
                    _table_signal_icon(color_class, title),
                )

                # Create a pandas DataFrame from the available WLANs
            df = pd.DataFrame(available_wlans)
            df = df[
                ["ssid", "channel", "signal_icon"]
            ]  # Select only the columns we want to display
            df.columns = [
                "SSID",
                _("Channel"),
                _("Signal"),
            ]  # Rename columns for display
            return df.style.set_table_attributes(
                'class="dataframe shiny-table table w-auto"'
            ).hide(axis="index")
        except Exception as e:
            logging.error(f"Failed to scan for available WLANs: {e}")
            # Return an empty DataFrame with an error message
            return pd.DataFrame({"ERROR": [_("Failed to scan for available WLANs")]})
        finally:
            ui.remove_ui("#pleasewait_wlan_scan")
