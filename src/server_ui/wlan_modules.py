"""WLAN connect/modify Shiny modules."""

from shiny import ui, reactive, module
import logging
from faicons import icon_svg
from src.baseconfig import CONFIG, set_language
from src.system import WlanManager
from src.mode import is_remote_mode
from src.server_ui.state import reload_trigger_wlan, _set_wlan_action_in_progress

_ = set_language(CONFIG["LANGUAGE"])

if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir

from src.server_ui.state import reload_trigger_wlan, _set_wlan_action_in_progress


def btn_wlan_modify():
    """UI fragment: pencil button to open the WLAN edit modal."""
    return ui.input_action_button(
        id=f"btn_wlan_modify",
        label="",
        icon=icon_svg("pencil", margin_left="-0.1em", margin_right="auto"),
        class_="btn-narrow btn-vertical-margin",
        style_="width: 42px;",
    )


@module.ui
def btn_wlan_connect():
    """UI fragment: wifi button to connect to a configured SSID."""
    return ui.input_action_button(
        id=f"btn_wlan_connect",
        label="",
        icon=icon_svg("wifi", margin_left="-0.2em", margin_right="auto"),
        class_="btn-narrow btn-vertical-margin",
        style_="width: 42px;",
    )


@module.server
def wlan_connect_server(input, output, session, ssid: str):
    """Server logic for switching the active WLAN connection to ``ssid``."""

    @reactive.effect
    @reactive.event(input.btn_wlan_connect)
    def wlan_connect():
        _set_wlan_action_in_progress(True)
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
            WlanManager.switch_wlan_connection(ssid)
            reload_trigger_wlan.set(reload_trigger_wlan.get() + 1)
        finally:
            _set_wlan_action_in_progress(False)
            ui.modal_remove()


@module.server
def wlan_modify_server(input, output, session, ssid: str):
    """Server logic for editing or deleting a configured WLAN by ``ssid``."""

    @reactive.effect
    @reactive.event(input.btn_wlan_modify)
    def wlan_modify():
        # We need to read the configured wlans again here to get the current connection status
        configured_wlans = WlanManager.get_wlan_connections()
        wlan = next((w for w in configured_wlans if w["ssid"] == ssid), None)
        if wlan is None:
            logging.error(f"WLAN with SSID {ssid} not found.")
            return
        connected = wlan["connected"]
        if connected:
            additional_note = ui.div(
                _(
                    "This WLAN is currently connected. You can not delete it or change the password."
                ),
                class_="alert alert-info",
                style_="margin-bottom: 0px; margin-top: 10px; padding: 10px",
            )
        else:
            additional_note = ""

        m = ui.modal(
            ui.div(
                ui.input_text("txtWlanSSID", _("SSID"), wlan["ssid"]),
                class_="disabled-wrapper",
            ),
            ui.div(
                ui.input_password(
                    "txtWlanPassword",
                    _("Password"),
                    "",
                    placeholder=_("Leave empty to keep the current password"),
                ),
                class_="disabled-wrapper" if connected else "",
            ),
            ui.input_numeric(
                "numWlanPriority",
                _("Priority"),
                wlan["priority"],
                min=0,
                max=100,
                step=1,
            ),
            ui.help_text(
                _(
                    "The priority determines the order in which the WLANs are tried to connect. Higher numbers are tried first."
                )
            ),
            additional_note,
            title=_("Change WLAN configuration"),
            easy_close=False,
            footer=ui.div(
                ui.input_action_button(
                    id="btn_wlan_save",
                    label=_("Save"),
                    class_="btn-vertical-margin btn-narrow",
                ),
                ui.input_action_button(
                    id="btn_modal_cancel",
                    label=_("Cancel"),
                    class_="btn-vertical-margin btn-narrow",
                ),
                ui.input_action_button(
                    id=f"btn_wlan_delete",
                    label=_("Delete"),
                    icon=icon_svg("trash"),
                    class_=f"btn-vertical-margin btn-narrow btn-danger {'disabled-wrapper' if connected else ''}",
                ),
            ),
        )
        ui.modal_show(m)

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

    @reactive.effect
    @reactive.event(input.btn_modal_cancel)
    def modal_cancel():
        ui.modal_remove()

    @reactive.effect
    @reactive.event(input.btn_wlan_delete)
    def wlan_delete():
        ssid = input.txtWlanSSID()
        success = WlanManager.delete_wlan_connection(ssid)
        if success:
            ui.notification_show(
                _("WLAN connection {} deleted successfully.").format(ssid),
                duration=5,
                type="message",
            )
            ui.modal_remove()
            reload_trigger_wlan.set(reload_trigger_wlan.get() + 1)
        else:
            ui.notification_show(
                _("Failed to delete WLAN connection {}").format(ssid),
                duration=10,
                type="error",
            )
