"""System tab, API tokens, reboot/shutdown."""

import os
import time as tm
from shiny import render, ui, reactive
import logging
from faicons import icon_svg
import subprocess
import re
from urllib import request as urllib_request
from urllib import error as urllib_error
from src.baseconfig import CONFIG, set_language
from src.helper import (
    Versioning,
    sigterm_monitor,
)
from src.system import ServiceOps
from src.mode import is_remote_mode
from src.server_ui.state import (
    reload_trigger_api_tokens,
    set_update_progress,
    get_update_progress,
)
from src.paths import kittyhack_root
from src.server_ui.context import SessionContext
from src.server_ui.helpers import centered_form_row

_ = set_language(CONFIG["LANGUAGE"])


def _load_api_docs_markdown() -> str | None:
    """Return the bundled REST API docs, or None if the file cannot be read."""
    path = os.path.join(kittyhack_root(), "doc", "api.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        logging.warning(f"[API] Failed to load API documentation '{path}': {e}")
        return None
    # Drop a leading H1 — the modal title already names the document.
    lines = text.splitlines()
    if lines and re.match(r"^\s*#\s+", lines[0]):
        text = "\n".join(lines[1:]).lstrip("\n")
    return text


if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir


def register_system_tab(input, output, session, ctx: SessionContext):
    """Register System tab UI (API tokens, reboot/shutdown, updates)."""

    @output
    @render.ui
    def ui_system():
        camera_driver_action = (
            ui.div(
                ui.hr(),
                ui.div(
                    ui.input_task_button(
                        "reinstall_camera_driver",
                        _("Reinstall Camera Driver"),
                        icon=icon_svg("rotate-right"),
                        class_="btn-default",
                    ),
                    style_="text-align: center;",
                ),
                ui.help_text(
                    _(
                        "Reinstall the camera driver if the live view does not work properly."
                    )
                ),
                ui.br(),
            )
            if not is_remote_mode()
            else ui.HTML("")
        )

        system_actions_body = None
        if is_remote_mode():
            system_actions_body = ui.div(
                ui.markdown(_("Start tasks/actions on the devices")),
                ui.br(),
                ui.h5(_("Kittyflap"), style_="text-align: center;"),
                ui.markdown(_("Target device actions")),
                ui.div(
                    ui.input_action_button(
                        "bTargetRebootKittyflap",
                        _("Restart Kittyflap"),
                        class_="btn-default",
                    ),
                    style_="text-align: center;",
                ),
                ui.br(),
                ui.div(
                    ui.input_action_button(
                        "bTargetShutdownKittyflap",
                        _("Shutdown Kittyflap"),
                        class_="btn-default",
                    ),
                    style_="text-align: center;",
                ),
                ui.help_text(
                    _(
                        "To avoid data loss, always shut down the Kittyflap properly before unplugging the power cable. After a shutdown, wait 30 seconds before unplugging the power cable. To start the Kittyflap again, just plug in the power again."
                    )
                ),
                ui.hr(),
                ui.h5(_("This device"), style_="text-align: center;"),
                ui.markdown(_("Remote-mode device actions")),
                ui.div(
                    ui.input_action_button(
                        "bRemoteReboot", _("Restart this device"), class_="btn-default"
                    ),
                    style_="text-align: center;",
                ),
                ui.br(),
                ui.div(
                    ui.input_action_button(
                        "bRemoteShutdown",
                        _("Shutdown this device"),
                        class_="btn-default",
                    ),
                    style_="text-align: center;",
                ),
                ui.help_text(
                    _(
                        "Shutting down this device will stop remote control and the web interface until it is started again."
                    )
                ),
            )
        else:
            system_actions_body = ui.div(
                ui.br(),
                ui.markdown(_("Start tasks/actions on the Kittyflap")),
                ui.br(),
                ui.div(
                    ui.input_action_button(
                        "bRestartKittyflap",
                        _("Restart Kittyflap"),
                        class_="btn-default",
                    ),
                    style_="text-align: center;",
                ),
                ui.br(),
                ui.div(
                    ui.input_action_button(
                        "bShutdownKittyflap",
                        _("Shutdown Kittyflap"),
                        class_="btn-default",
                    ),
                    style_="text-align: center;",
                ),
                ui.help_text(
                    _(
                        "To avoid data loss, always shut down the Kittyflap properly before unplugging the power cable. After a shutdown, wait 30 seconds before unplugging the power cable. To start the Kittyflap again, just plug in the power again."
                    )
                ),
            )

        api_tokens_card = ui.card(
            ui.card_header(
                ui.h4(_("API Tokens"), style_="text-align: center;"),
            ),
            ui.div(
                ui.markdown(
                    _(
                        "Tokens let scripts and devices — for example Home Assistant, "
                        "Stream Deck, or iOS Shortcuts — control the flap through the "
                        "REST API. Each token is shown only once when you create it, "
                        "so copy it immediately and store it in a safe place."
                        "\n\n"
                        "Send the token as an `Authorization: Bearer <token>` header. "
                        "If a client can only open a URL (Stream Deck, bookmarks), "
                        "you can pass it as a `?token=<token>` query parameter instead."
                    )
                ),
                style_="text-align: left;",
            ),
            ui.div(
                ui.input_action_button(
                    "btn_api_docs",
                    _("Open API documentation"),
                    icon=icon_svg("book"),
                    class_="btn-default",
                ),
                style_="text-align: center; margin-top: 0.5rem;",
            ),
            ui.br(),
            ui.output_ui("ui_api_tokens_table"),
            ui.hr(),
            ui.h5(_("Create new token"), style_="text-align: center;"),
            centered_form_row(
                ui.input_text(
                    "api_token_label",
                    _("Label"),
                    placeholder=_("e.g. stream-deck"),
                    width="90%",
                ),
            ),
            ui.div(
                ui.input_action_button(
                    "btn_create_api_token",
                    _("Create Token"),
                    icon=icon_svg("plus"),
                    class_="btn-default",
                ),
                style_="text-align: center;",
            ),
            ui.hr(),
            ui.h5(_("Revoke token"), style_="text-align: center;"),
            ui.output_ui("ui_api_tokens_revoke_select"),
            ui.div(
                ui.input_action_button(
                    "btn_revoke_api_token",
                    _("Revoke Selected"),
                    icon=icon_svg("trash"),
                    class_="btn-default",
                ),
                style_="text-align: center;",
            ),
            ui.br(),
            full_screen=False,
            class_="generic-container",
            style_=(
                "padding-left: 1rem !important; "
                "padding-right: 1rem !important; "
                "padding-bottom: 1.5rem !important;"
            ),
        )

        return ui.div(
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(
                            _("Kittyflap System Actions"), style_="text-align: center;"
                        ),
                    ),
                    system_actions_body,
                    camera_driver_action,
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px",
            ),
            ui.br(),
            ui.div(api_tokens_card, width="400px"),
            (
                ui.div(
                    ui.br(),
                    ui.output_ui("ui_wlan_configured_connections"),
                    ui.output_ui("ui_wlan_available_networks"),
                )
                if not is_remote_mode()
                else ui.HTML("")
            ),
            ui.br(),
            ui.br(),
        )

    def _trigger_target_power_action(action: str) -> bool:
        """Best-effort trigger for target reboot/shutdown (remote-mode only)."""
        if not is_remote_mode():
            return False

        act = str(action or "").strip().lower()
        if act not in {"reboot", "shutdown"}:
            return False

        target_host = str(CONFIG.get("REMOTE_TARGET_HOST") or "").strip()
        ok = False

        # Prefer control websocket when available.
        try:
            from src.remote.control_client import RemoteControlClient

            client = RemoteControlClient.instance()
            client.ensure_started()
            if client.wait_until_ready(timeout=2.0):
                if act == "reboot":
                    ok = bool(client.request_target_reboot(timeout=2.0))
                else:
                    ok = bool(client.request_target_shutdown(timeout=2.0))
        except Exception as e:
            logging.warning(
                f"[REMOTE_MODE] Target {act} trigger via websocket failed: {e}"
            )

            # Fallback: direct HTTP call to kittyhack_control on target.
        if (not ok) and target_host:
            try:
                endpoint = "reboot" if act == "reboot" else "shutdown"
                req = urllib_request.Request(
                    f"http://{target_host}/api/{endpoint}",
                    data=b"",
                    method="POST",
                )
                with urllib_request.urlopen(req, timeout=2.0) as resp:
                    ok = int(getattr(resp, "status", 200) or 200) < 300
            except urllib_error.URLError:
                ok = False
            except Exception:
                ok = False

        return bool(ok)

    @reactive.Effect
    @reactive.event(input.reinstall_camera_driver)
    def on_action_reinstall_camera_driver():
        if is_remote_mode():
            return
        m = ui.modal(
            _(
                "Do you really want to reinstall the camera driver? This operation can take several minutes."
            ),
            title=_("Reinstall Camera Driver"),
            easy_close=False,
            footer=ui.div(
                ui.input_task_button("btn_modal_reinstall_cam_ok", _("OK")),
                ui.input_action_button("btn_modal_cancel", _("Cancel")),
            ),
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_modal_reinstall_cam_ok)
    def reinstall_camera_driver_process():
        if is_remote_mode():
            return
            # Read the dependencies (*.deb packages) from the "camera_dependencies.txt" file
        dependencies_file = "./camera_dependencies.txt"
        dependencies_url = "https://github.com/floppyFK/kittyhack-dependencies/raw/refs/heads/main/camera/"
        download_path = "/tmp/kittyhack-dependencies/camera/"

        with open(dependencies_file, "r") as file:
            dependencies = file.readlines()
        installation_steps = 4 + len(dependencies)

        with ui.Progress(min=1, max=installation_steps) as p:
            p.set(
                message=_("Reinstall Camera Driver in progress."),
                detail=_("This may take a while..."),
            )
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
                        if not Versioning.execute_update_step(
                            f"wget -O {dependency_path} {dependency_url}", msg
                        ):
                            raise subprocess.CalledProcessError(
                                1, f"wget {dependency_url}"
                            )

                            # Step 3: Uninstall all libcamera packages above version 0.3 (if there are any)
                msg = "Uninstalling libcamera packages above version 0.3"
                msg_localized = _("Uninstalling libcamera packages above version 0.3")
                i += 1
                p.set(i, message=msg_localized)
                logging.info(msg)
                result = subprocess.run(
                    "dpkg -l | grep libcamera",
                    shell=True,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                pattern = r"libcamera(\d+)\.(\d+)"
                for line in result.stdout.splitlines():
                    if line.startswith("ii"):
                        parts = line.split()
                        package_name = parts[1].split(":")[
                            0
                        ]  # Remove architecture suffix
                        if package_name.startswith("libcamera"):
                            # Try to match the version pattern
                            match = re.search(pattern, package_name)
                            if match:
                                major_version = int(match.group(1))
                                minor_version = int(match.group(2))
                                version = float(f"{major_version}.{minor_version}")
                                if version > 0.3:
                                    logging.info(
                                        f"Uninstalling package {parts[1]} (version {version})"
                                    )
                                    Versioning.execute_update_step(
                                        f"apt-get remove -y {parts[1]}",
                                        f"Uninstalling package {parts[1]}",
                                    )

                                    # Step 4: Install all dependencies
                msg = "Installing all dependencies"
                msg_localized = _("Installing all dependencies")
                i += 1
                p.set(i, message=msg_localized)
                logging.info(msg)
                dependencies_paths = " ".join(
                    [
                        os.path.join(download_path, dep.strip())
                        for dep in dependencies
                        if dep.strip()
                    ]
                )
                if not Versioning.execute_update_step(
                    f"dpkg -i {dependencies_paths}", msg
                ):
                    raise subprocess.CalledProcessError(
                        1, f"dpkg -i {dependencies_paths}"
                    )

            except subprocess.CalledProcessError as e:
                ui.modal_remove()
                logging.error(f"An error occurred during the installation process: {e}")
                ui.notification_show(
                    _(
                        "An error occurred during the installation process. Please check the logs for details."
                    ),
                    duration=None,
                    type="error",
                )

            else:
                logging.info(f"Camera driver reinstallation successful.")
                # Show the restart dialog
                ui.modal_remove()
                m = ui.modal(
                    _(
                        "A restart is required to apply the update. Do you want to restart the Kittyflap now?"
                    ),
                    title=_("Restart required"),
                    easy_close=False,
                    footer=ui.div(
                        ui.input_action_button("btn_modal_reboot_ok", _("OK")),
                        ui.input_action_button("btn_modal_cancel", _("Cancel")),
                    ),
                )
                ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_modal_cancel)
    def modal_cancel():
        ui.modal_remove()
        # Reset update progress result if the restart-required modal was open
        state = get_update_progress()
        if state["result"] == "ok" or state["result"] == "reboot_dialog":
            set_update_progress(in_progress=False, result=None)

    @reactive.effect
    @reactive.event(input.btn_modal_reboot_ok)
    def modal_reboot():
        state = get_update_progress()
        if state["result"] == "ok" or state["result"] == "reboot_dialog":
            set_update_progress(in_progress=False, result=None)

        if is_remote_mode():
            # Fast path: do not block local reboot for long reconnect waits.
            # Trigger target reboot best-effort using quick websocket/http attempts.
            reboot_ack = False
            target_host = str(CONFIG.get("REMOTE_TARGET_HOST") or "").strip()

            try:
                from src.remote.control_client import RemoteControlClient

                client = RemoteControlClient.instance()
                client.ensure_started()
                # Only use immediate readiness check (no long wait).
                if client.wait_until_ready(timeout=0.0):
                    reboot_ack = bool(client.request_target_reboot(timeout=1.0))
            except Exception as e:
                logging.warning(
                    f"[REMOTE_MODE] Quick websocket target reboot trigger failed: {e}"
                )

            if (not reboot_ack) and target_host:
                try:
                    req = urllib_request.Request(
                        f"http://{target_host}/api/reboot",
                        data=b"",
                        method="POST",
                    )
                    with urllib_request.urlopen(req, timeout=1.0) as resp:
                        reboot_ack = int(getattr(resp, "status", 200) or 200) < 300
                except urllib_error.URLError:
                    reboot_ack = False
                except Exception:
                    reboot_ack = False

            if not reboot_ack:
                logging.warning(
                    "[REMOTE_MODE] Target reboot trigger not confirmed; proceeding with local reboot immediately."
                )

                # Tiny grace so the target reboot request can be sent before this process exits.
            tm.sleep(0.2)

        ui.modal_remove()
        reboot_message = _(
            "Kittyflap is rebooting now... This will take 1 or 2 minutes. Please reload the page after the restart."
        )
        if is_remote_mode():
            reboot_message = _(
                "Both devices are rebooting now... This may take 1 or 2 minutes. Please reconnect and reload the page afterwards."
            )
        ui.modal_show(
            ui.modal(reboot_message, title=_("Restart Kittyflap"), footer=None)
        )
        ServiceOps.systemcmd(["/sbin/reboot"])

        # ------------------------------------------------------------------
        # API token management (System tab)
        # ------------------------------------------------------------------

    @reactive.effect
    @reactive.event(input.btn_api_docs)
    def on_show_api_docs():
        docs_md = _load_api_docs_markdown()
        if docs_md is None:
            body = ui.markdown(_("The API documentation could not be loaded."))
        else:
            parts = []
            if str(CONFIG.get("LANGUAGE") or "en").strip().lower() == "de":
                parts.append(
                    ui.p(
                        _("This documentation is currently available in English only."),
                        class_="kh-api-docs-lang-note",
                    )
                )
            parts.append(ui.markdown(docs_md))
            body = ui.div(*parts, class_="kh-api-docs")
        ui.modal_show(
            ui.modal(
                body,
                title=_("REST API documentation"),
                easy_close=True,
                size="xl",
                footer=ui.div(
                    ui.input_action_button("btn_modal_cancel", _("Close")),
                ),
            )
        )

    @output
    @render.ui
    def ui_api_tokens_table():
        # Subscribe to reload trigger so create/revoke actions refresh the view.
        reload_trigger_api_tokens.get()
        try:
            from src.api import list_tokens

            tokens = list_tokens()
        except Exception as e:
            logging.exception("[API] failed to list tokens")
            return ui.tags.em(_("Failed to load tokens: ") + str(e))
        if not tokens:
            return ui.tags.em(_("No tokens yet."))
        rows = []
        for t in tokens:
            created = (t.get("created_at") or "")[:19].replace("T", " ")
            last_used = t.get("last_used_at")
            last_used_str = (
                (last_used or "")[:19].replace("T", " ") if last_used else _("never")
            )
            rows.append(
                ui.tags.tr(
                    ui.tags.td(t.get("label", "?")),
                    ui.tags.td(created),
                    ui.tags.td(last_used_str),
                )
            )
        return ui.tags.table(
            ui.tags.thead(
                ui.tags.tr(
                    ui.tags.th(_("Label")),
                    ui.tags.th(_("Created")),
                    ui.tags.th(_("Last used")),
                )
            ),
            ui.tags.tbody(*rows),
            class_="dataframe shiny-table table w-auto",
        )

    @output
    @render.ui
    def ui_api_tokens_revoke_select():
        reload_trigger_api_tokens.get()
        try:
            from src.api import list_tokens

            tokens = list_tokens()
        except Exception:
            tokens = []
        choices = {"": _("— select a token —")}
        for t in tokens:
            tid = t.get("id", "")
            label = t.get("label", "?")
            choices[tid] = f"{label} ({tid})"
        return centered_form_row(
            ui.input_select(
                "api_token_revoke_id", None, choices=choices, width="90%"
            ),
        )

    @reactive.effect
    @reactive.event(input.btn_create_api_token)
    def on_create_api_token():
        label = (input.api_token_label() or "").strip()
        if not label:
            ui.notification_show(
                _("Please enter a label first."), duration=5, type="warning"
            )
            return
        try:
            from src.api import create_token

            raw, _record = create_token(label)
        except Exception as e:
            logging.exception("[API] token create failed")
            ui.notification_show(
                _("Failed to create token: ") + str(e), duration=10, type="error"
            )
            return
        logging.info(f"[API] Created new API token '{label}' via System tab")
        reload_trigger_api_tokens.set(reload_trigger_api_tokens.get() + 1)
        ui.update_text("api_token_label", value="")
        m = ui.modal(
            ui.markdown(_("**Copy this token now — it will not be shown again.**")),
            ui.tags.pre(
                ui.tags.code(raw),
                # Use Bootstrap theme tokens so the token box stays readable in
                # both light and dark themes. Previous hard-coded light bg led
                # to white text on (near-)white background in dark mode.
                style_=(
                    "background: var(--bs-tertiary-bg, #f5f5f5); "
                    "color: var(--bs-body-color, #212529); "
                    "border: 1px solid var(--bs-border-color, #dee2e6); "
                    "padding: 0.5rem; border-radius: 4px; "
                    "word-break: break-all; white-space: pre-wrap; "
                    "user-select: all;"
                ),
            ),
            ui.markdown(
                _(
                    "Use it as an `Authorization: Bearer <token>` header. If a client "
                    "can only open a URL (Stream Deck, bookmarks), you can pass it as "
                    "a `?token=<token>` query parameter instead. Tokens in URLs may "
                    "appear in web-server access logs and browser history — prefer a "
                    "header whenever possible."
                )
            ),
            title=_("Token created"),
            easy_close=False,
            footer=ui.input_action_button("btn_modal_cancel", _("Close")),
        )
        ui.modal_show(m)

    @reactive.Effect
    @reactive.event(input.btn_revoke_api_token)
    def on_revoke_api_token():
        token_id = (input.api_token_revoke_id() or "").strip()
        if not token_id:
            ui.notification_show(
                _("Please select a token to revoke."), duration=5, type="warning"
            )
            return
        m = ui.modal(
            _(
                "Revoke token '{}'? This cannot be undone. Any client using it will immediately lose access."
            ).format(token_id),
            title=_("Revoke API token"),
            easy_close=False,
            footer=ui.div(
                ui.input_action_button("btn_modal_revoke_api_token_ok", _("Revoke")),
                ui.input_action_button("btn_modal_cancel", _("Cancel")),
            ),
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_modal_revoke_api_token_ok)
    def on_modal_revoke_api_token_ok():
        token_id = (input.api_token_revoke_id() or "").strip()
        ui.modal_remove()
        if not token_id:
            return
        try:
            from src.api import revoke_token

            ok = revoke_token(token_id)
        except Exception as e:
            logging.exception("[API] token revoke failed")
            ui.notification_show(
                _("Failed to revoke token: ") + str(e), duration=10, type="error"
            )
            return
        if ok:
            logging.info(f"[API] Revoked API token {token_id} via System tab")
            ui.notification_show(_("Token revoked."), duration=5, type="message")
        else:
            ui.notification_show(
                _("Token not found — it may have been revoked already."),
                duration=5,
                type="warning",
            )
        reload_trigger_api_tokens.set(reload_trigger_api_tokens.get() + 1)

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
            ),
        )
        ui.modal_show(m)

    @reactive.Effect
    @reactive.event(input.bTargetRebootKittyflap)
    def on_action_target_reboot_system():
        if not is_remote_mode():
            return
        m = ui.modal(
            _("Do you really want to restart the Kittyflap?"),
            title=_("Restart Kittyflap"),
            easy_close=True,
            footer=ui.div(
                ui.input_action_button("btn_modal_reboot_target_ok", _("OK")),
                ui.input_action_button("btn_modal_cancel", _("Cancel")),
            ),
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_modal_reboot_target_ok)
    def modal_reboot_target_only():
        if not is_remote_mode():
            return
        ui.modal_remove()
        ok = _trigger_target_power_action("reboot")
        if ok:
            ui.modal_show(
                ui.modal(
                    _(
                        "Kittyflap is rebooting now... This will take 1 or 2 minutes. Please reconnect and reload the page afterwards."
                    ),
                    title=_("Restart Kittyflap"),
                    footer=None,
                )
            )
        else:
            ui.notification_show(
                _(
                    "Failed to trigger Kittyflap reboot. Check the remote connection and REMOTE_TARGET_HOST."
                ),
                duration=12,
                type="error",
            )

    @reactive.Effect
    @reactive.event(input.bRemoteReboot)
    def on_action_local_reboot_system():
        if not is_remote_mode():
            return
        m = ui.modal(
            _("Do you really want to restart this device?"),
            title=_("Restart this device"),
            easy_close=True,
            footer=ui.div(
                ui.input_action_button("btn_modal_reboot_local_ok", _("OK")),
                ui.input_action_button("btn_modal_cancel", _("Cancel")),
            ),
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_modal_reboot_local_ok)
    def modal_reboot_local_only():
        if not is_remote_mode():
            return
        ui.modal_remove()
        ui.modal_show(
            ui.modal(
                _(
                    "This device is rebooting now... Please reconnect and reload the page afterwards."
                ),
                title=_("Restart this device"),
                footer=None,
            )
        )
        ServiceOps.systemcmd(["/sbin/reboot"])

    @reactive.effect
    @reactive.event(input.btn_modal_shutdown_ok)
    def modal_shutdown():
        ui.modal_remove()
        ui.modal_show(
            ui.modal(
                _(
                    "Kittyflap is shutting down now... Please wait 30 seconds before unplugging the power."
                ),
                title=_("Shutdown Kittyflap"),
                footer=None,
            )
        )
        ServiceOps.systemcmd(["/usr/sbin/shutdown", "-H", "now"])

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
            ),
        )
        ui.modal_show(m)

    @reactive.Effect
    @reactive.event(input.bTargetShutdownKittyflap)
    def on_action_target_shutdown_system():
        if not is_remote_mode():
            return
        m = ui.modal(
            _("Do you really want to shut down the Kittyflap?"),
            title=_("Shutdown Kittyflap"),
            easy_close=True,
            footer=ui.div(
                ui.input_action_button("btn_modal_shutdown_target_ok", _("OK")),
                ui.input_action_button("btn_modal_cancel", _("Cancel")),
            ),
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_modal_shutdown_target_ok)
    def modal_shutdown_target_only():
        if not is_remote_mode():
            return
        ui.modal_remove()
        ok = _trigger_target_power_action("shutdown")
        if ok:
            ui.modal_show(
                ui.modal(
                    _(
                        "Kittyflap is shutting down now... Please wait 30 seconds before unplugging the power."
                    ),
                    title=_("Shutdown Kittyflap"),
                    footer=None,
                )
            )
        else:
            ui.notification_show(
                _(
                    "Failed to trigger Kittyflap shutdown. Check the remote connection and REMOTE_TARGET_HOST."
                ),
                duration=12,
                type="error",
            )

    @reactive.Effect
    @reactive.event(input.bRemoteShutdown)
    def on_action_local_shutdown_system():
        if not is_remote_mode():
            return
        m = ui.modal(
            _("Do you really want to shut down this device?"),
            title=_("Shutdown this device"),
            easy_close=True,
            footer=ui.div(
                ui.input_action_button("btn_modal_shutdown_local_ok", _("OK")),
                ui.input_action_button("btn_modal_cancel", _("Cancel")),
            ),
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_modal_shutdown_local_ok)
    def modal_shutdown_local_only():
        if not is_remote_mode():
            return
        ui.modal_remove()
        ui.modal_show(
            ui.modal(
                _("This device is shutting down now..."),
                title=_("Shutdown this device"),
                footer=None,
            )
        )
        ServiceOps.systemcmd(["/usr/sbin/shutdown", "-H", "now"])
