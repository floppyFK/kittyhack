"""Session preamble: remote sync, live view stream, shared effects."""

import os
import configparser
from datetime import datetime
import time as tm
from src.clock import monotonic_time
from shiny import render, ui, reactive
import logging
import base64
from zoneinfo import ZoneInfo
from faicons import icon_svg
import math
import threading
import hashlib
from src.baseconfig import (
    CONFIG,
    load_config,
    set_language,
    UserNotifications,
    read_remote_config_values,
    update_single_config_parameter,
)
from src.helper import (
    SystemInfo,
    Versioning,
)
from src.system import (
    LabelStudioInstall,
    ServiceOps,
)
from src.database import last_imgblock_ts
from src.paths import kittyhack_root
from src.mode import is_remote_mode
from src.model import RemoteModelTrainer
from src.backend import backend_main, model_handler
import src.startup as startup
from src.server_ui.state import (
    reload_trigger_photos,
    reload_trigger_ai,
    reload_trigger_config,
    live_view_refresh_nonce,
    live_view_aspect,
)
from src.server_ui.context import SessionContext

_ = set_language(CONFIG["LANGUAGE"])

if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir


def register_session_setup(input, output, session, ctx: SessionContext):
    """Register session preamble: remote sync, live stream, shared effects."""

    # Keep the browser session reconnectable after brief mobile backgrounding.
    # Shiny for Python 1.2.1 has no session.allow_reconnect(); send the protocol
    # message that shiny.js already understands ("force" = reconnect without Shiny Server).
    try:
        session._send_message_sync({"allowReconnect": "force"})
    except Exception:
        logging.debug("[SESSION] Could not enable Shiny client reconnect", exc_info=True)

    @reactive.effect
    def update_live_view_warning_html():
        # Keep warning rendering independent from live image rendering.
        __ = live_view_refresh_nonce.get()

        refresh_s = float(CONFIG.get("LIVE_VIEW_REFRESH_INTERVAL", 2.0) or 2.0)
        refresh_s = max(0.25, refresh_s)

        warning_html = ""
        warning_signature = ""
        if (not is_remote_mode()) and CONFIG.get("CAMERA_SOURCE") == "ip_camera":
            try:
                res = model_handler.get_camera_resolution()
                if res and isinstance(res, (tuple, list)) and len(res) == 2:
                    width, height = int(res[0]), int(res[1])
                    if width > 0 and height > 0 and (width * height > 1280 * 720):
                        warning_signature = f"ip_camera:{width}x{height}"
                        warning_html = str(
                            ui.div(
                                ui.div(
                                    icon_svg(
                                        "triangle-exclamation",
                                        margin_left="0",
                                        margin_right="0.2em",
                                    ),
                                    _("Warning")
                                    + ": "
                                    + _(
                                        "Your IP camera resolution is higher than recommended (max. 1280x720)."
                                    )
                                    + " "
                                    + _(
                                        "Current: {width}x{height}. This may have negative effects on performance."
                                    ).format(width=width, height=height),
                                    class_="generic-container warning-container",
                                ),
                                style_="text-align: center;",
                            )
                        )
            except Exception as e:
                logging.error(f"Failed to check IP camera resolution for warning: {e}")

                # Reset dismiss state when the warning context changes (e.g. resolution changed).
        try:
            prev_signature = ctx.live_view_warning_signature.get()
        except Exception:
            prev_signature = ""
        if prev_signature != warning_signature:
            ctx.live_view_warning_signature.set(warning_signature)
            ctx.live_view_warning_dismissed.set(False)

            # Respect user dismissal while warning context stays unchanged.
        if warning_html and bool(ctx.live_view_warning_dismissed.get()):
            warning_html = ""

        try:
            prev_warning_html = ctx.live_view_warning_html.get()
            if prev_warning_html != warning_html:
                # Log only on state changes to avoid log spam.
                if warning_html:
                    logging.info("[LIVE_VIEW] IP camera resolution warning enabled.")
                elif prev_warning_html:
                    logging.info("[LIVE_VIEW] IP camera resolution warning cleared.")
                ctx.live_view_warning_html.set(warning_html)
        except Exception:
            ctx.live_view_warning_html.set(warning_html)

        reactive.invalidate_later(refresh_s)

    @reactive.Effect
    @reactive.event(input.btn_dismiss_live_view_warning)
    def dismiss_live_view_warning():
        ctx.live_view_warning_dismissed.set(True)
        ctx.live_view_warning_html.set("")

    muteable_ids_in_current_modal = []

    def show_user_notifications():
        user_notifications = UserNotifications.get_all()
        if len(user_notifications) > 0:
            # Create a combined message from all notifications
            combined_message = ""
            muteable_ids_in_current_modal.clear()
            for i, notification in enumerate(user_notifications):
                combined_message += (
                    f"## {notification['header']}\n\n{notification['message']}"
                )
                # Add a separator between notifications, but not after the last one
                if i < len(user_notifications) - 1:
                    combined_message += "\n\n---\n\n"
                if notification.get("muteable") and notification.get("id"):
                    muteable_ids_in_current_modal.append(notification["id"])
                UserNotifications.remove(notification["id"])

            footer_items = [ui.input_action_button("btn_modal_cancel", _("Close"))]
            if muteable_ids_in_current_modal:
                footer_items.insert(
                    0,
                    ui.input_action_button(
                        "btn_mute_user_notifications",
                        _("Do not notify again"),
                        class_="btn-outline-secondary",
                    ),
                )

            ui.modal_show(
                ui.modal(
                    ui.div(
                        ui.markdown(combined_message),
                    ),
                    title=_("Notifications"),
                    easy_close=False,
                    size="lg",
                    footer=ui.div(*footer_items),
                )
            )

    @reactive.Effect
    @reactive.event(input.btn_mute_user_notifications)
    def mute_shown_user_notifications():
        for notification_id in list(muteable_ids_in_current_modal):
            UserNotifications.mute(notification_id)
        muteable_ids_in_current_modal.clear()
        ui.modal_remove()

    @output
    @render.ui
    def ui_remote_connection_badge():
        # Navbar indicator: show if we are in remote-mode and currently connected to the target.
        if not is_remote_mode():
            return ui.HTML("")

        reactive.invalidate_later(1.0)

        host, is_ready, manual_disconnect = ctx._remote_connection_state()
        if not host:
            badge = ui.tags.span(
                {
                    "class": "badge rounded-pill text-bg-secondary",
                    "title": _("Remote target host not configured"),
                },
                _("Remote: not configured"),
            )
            return ui.tags.div({"class": "remote-nav-controls"}, badge)

        if is_ready:
            badge = ui.tags.span(
                {
                    "class": "badge rounded-pill text-bg-success",
                    "title": _("Connected to Kittyflap") + f": {host}",
                },
                _("Remote: connected"),
            )
            return ui.tags.div(
                {"class": "remote-nav-controls"},
                badge,
                ui.input_action_button(
                    "btn_remote_disconnect",
                    _("Disconnect"),
                    class_="btn-sm btn-outline-danger remote-nav-disconnect-btn",
                ),
            )

        if manual_disconnect:
            badge = ui.tags.span(
                {
                    "class": "badge rounded-pill text-bg-secondary",
                    "title": _("Disconnected from Kittyflap") + f": {host}",
                },
                _("Remote: disconnected"),
            )
            return ui.tags.div(
                {"class": "remote-nav-controls"},
                badge,
                ui.input_action_button(
                    "btn_remote_disconnect",
                    _("Disconnect"),
                    class_="btn-sm btn-outline-danger remote-nav-disconnect-btn",
                ),
            )

            # Connecting / reconnecting
        badge = ui.tags.span(
            {
                "class": "badge rounded-pill text-bg-warning",
                "title": _("Connecting to Kittyflap") + f": {host}",
            },
            ui.tags.span(
                {
                    "class": "spinner-border spinner-border-sm me-1",
                    "role": "status",
                    "aria-hidden": "true",
                }
            ),
            _("Remote: connecting"),
        )
        return ui.tags.div(
            {"class": "remote-nav-controls"},
            badge,
            ui.input_action_button(
                "btn_remote_disconnect",
                _("Disconnect"),
                class_="btn-sm btn-outline-danger remote-nav-disconnect-btn",
            ),
        )

    def show_remote_setup_modal():
        if not (is_remote_mode() and startup.remote_setup_required):
            return

            # If a restart is pending (e.g. user refreshed after sync), block with restart modal.
        try:
            if os.path.exists(ctx._remote_restart_pending_marker_path()):
                show_remote_restart_required_modal()
                return
        except Exception:
            pass

            # If an initial sync is currently in progress (or pending), show the sync modal instead
            # of the settings form. This also covers browser refresh during sync.
        if bool(CONFIG.get("REMOTE_SYNC_ON_FIRST_CONNECT", True)):
            try:
                from src.remote.control_client import RemoteControlClient

                client = RemoteControlClient.instance()
                client.ensure_started()
                st = client.get_sync_status()
                in_progress = bool(st.get("in_progress"))
                requested = bool(st.get("requested"))
                ok = st.get("ok")
                if requested and (in_progress or ok is None):
                    ctx.remote_sync_status.set(st)
                    show_remote_initial_sync_modal()
                    ctx.remote_sync_modal_open.set(True)
                    return
            except Exception:
                pass

        values = read_remote_config_values()
        ui.modal_show(
            ui.modal(
                ui.div(
                    ui.markdown(
                        _("Remote-mode setup is required before Kittyhack can start.")
                        + "\n\n"
                        + _(
                            "After saving, an initial sync can take several minutes (depending on pictures/models size)."
                        )
                        + "\n"
                        + _(
                            "Please keep this browser tab open and do not close or reload it until synchronization is finished."
                        )
                        + "\n"
                        + _(
                            "Startup continues after a successful sync and service restart."
                        )
                    ),
                    ui.input_text(
                        "remote_target_host",
                        _("Kittyflap IP:"),
                        value=values["remote_target_host"],
                        width="100%",
                    ),
                    ui.input_switch(
                        "remote_sync_on_first_connect",
                        _("Sync on first connect"),
                        values["remote_sync_on_first_connect"],
                    ),
                    ui.input_switch(
                        "remote_sync_labelstudio",
                        _("Sync Label Studio user data"),
                        values.get("remote_sync_labelstudio", True),
                    ),
                ),
                title=_("Remote-mode setup"),
                easy_close=False,
                footer=ui.div(
                    ui.input_action_button("btn_remote_setup_save", _("Save settings"))
                ),
                size="lg",
            )
        )

    def show_remote_initial_sync_modal(error_reason: str | None = None):
        body = ui.div(
            ui.div(
                ui.tags.div(
                    {
                        "class": "spinner-border text-primary",
                        "role": "status",
                    },
                    ui.tags.span({"class": "visually-hidden"}, _("Loading...")),
                ),
                class_="d-flex align-items-center gap-3",
            ),
            ui.div(ui.output_ui("remote_initial_sync_status_ui"), class_="mt-3"),
        )

        footer = ui.div(
            ui.input_action_button(
                "btn_remote_sync_abort", _("Abort"), class_="btn-danger"
            )
        )
        if error_reason:
            body = ui.div(
                ui.markdown(_("Initial sync failed.")),
                ui.markdown(error_reason),
                ui.div(ui.output_ui("remote_initial_sync_status_ui"), class_="mt-3"),
            )
            footer = ui.div(
                ui.input_action_button("btn_remote_sync_failed_close", _("Back")),
                ui.input_action_button(
                    "btn_remote_sync_abort", _("Abort"), class_="btn-danger"
                ),
            )

        ui.modal_show(
            ui.modal(
                body,
                title=_("Initial sync"),
                easy_close=False,
                footer=footer,
                size="lg",
            )
        )

    def show_remote_restart_required_modal():
        ui.modal_show(
            ui.modal(
                ui.div(
                    ui.markdown(
                        _("Initial sync has completed.")
                        + "\n\n"
                        + _(
                            "To fully apply all settings, the Kittyhack service must be restarted now."
                        )
                        + "\n"
                        + _(
                            "The web interface will be briefly unavailable during the restart."
                        )
                    )
                ),
                title=_("Restart required"),
                easy_close=False,
                footer=ui.div(
                    ui.input_action_button(
                        "btn_remote_restart_kittyhack", _("Restart Kittyhack service")
                    )
                ),
                size="lg",
            )
        )

    def show_remote_disconnected_modal(host: str, manual_disconnect: bool = False):
        if str(host).startswith("http://") or str(host).startswith("https://"):
            target_ui_url = str(host)
        else:
            target_ui_url = f"http://{host}"

        headline = _("Connection to Kittyflap is disconnected.")
        if manual_disconnect:
            headline = _("Connection to Kittyflap has been disconnected manually.")

        ui.modal_show(
            ui.modal(
                ui.div(
                    ui.markdown(
                        headline
                        + "\n\n"
                        + _(
                            "The Kittyflap can now be controlled directly in its web UI:"
                        )
                        + f"\n{target_ui_url}"
                    )
                ),
                title=_("Remote disconnected"),
                easy_close=False,
                footer=ui.div(
                    ui.input_action_button("btn_remote_reconnect", _("Reconnect"))
                ),
                size="lg",
            )
        )

    def show_remote_disconnect_confirm_modal():
        ui.modal_show(
            ui.modal(
                ui.div(
                    ui.markdown(_("Do you really want to disconnect from Kittyflap?"))
                ),
                title=_("Disconnect"),
                easy_close=True,
                footer=ui.div(
                    ui.input_action_button(
                        "btn_remote_disconnect_cancel",
                        _("Cancel"),
                        class_="btn-secondary",
                    ),
                    ui.input_action_button(
                        "btn_remote_disconnect_confirm",
                        _("Disconnect"),
                        class_="btn-danger",
                    ),
                ),
                size="m",
            )
        )

    if is_remote_mode() and startup.remote_setup_required:
        show_remote_setup_modal()

        # If a restart is pending (e.g. browser refreshed after sync), show the restart modal.
    if is_remote_mode():
        try:
            if os.path.exists(ctx._remote_restart_pending_marker_path()):
                show_remote_restart_required_modal()
                ctx.remote_restart_modal_open.set(True)
        except Exception:
            pass

    @reactive.Effect
    def _remote_disconnect_modal_poller():
        if not is_remote_mode():
            return

        reactive.invalidate_later(0.5)

        if bool(ctx.remote_setup_active.get()):
            return

        host, is_ready, manual_disconnect = ctx._remote_connection_state()

        if is_ready:
            if not bool(ctx.remote_connected_once.get()):
                ctx.remote_connected_once.set(True)
            if bool(ctx.remote_disconnect_modal_open.get()):
                ui.modal_remove()
                ctx.remote_disconnect_modal_open.set(False)
            return

        should_show = bool(host) and (
            bool(ctx.remote_connected_once.get()) or manual_disconnect
        )
        if should_show and not bool(ctx.remote_disconnect_modal_open.get()):
            show_remote_disconnected_modal(
                host=host, manual_disconnect=manual_disconnect
            )
            ctx.remote_disconnect_modal_open.set(True)

    @reactive.Effect
    @reactive.event(input.btn_remote_disconnect)
    def _remote_disconnect_button_click():
        if not is_remote_mode():
            return
        show_remote_disconnect_confirm_modal()

    @reactive.Effect
    @reactive.event(input.btn_remote_disconnect_cancel)
    def _remote_disconnect_cancel_click():
        if not is_remote_mode():
            return
        ui.modal_remove()

    @reactive.Effect
    @reactive.event(input.btn_remote_disconnect_confirm)
    def _remote_disconnect_confirm_click():
        if not is_remote_mode():
            return
        try:
            from src.remote.control_client import RemoteControlClient

            client = RemoteControlClient.instance()
            client.ensure_started()
            client.disconnect()
            ctx.remote_connected_once.set(True)
            ui.modal_remove()
            ui.notification_show(
                _("Disconnected from Kittyflap."), duration=4, type="message"
            )
        except Exception as e:
            ui.notification_show(
                _("Failed to disconnect from Kittyflap: {}.").format(e),
                duration=8,
                type="error",
            )

    @reactive.Effect
    @reactive.event(input.btn_remote_reconnect)
    def _remote_reconnect_button_click():
        if not is_remote_mode():
            return
        try:
            from src.remote.control_client import RemoteControlClient

            client = RemoteControlClient.instance()
            client.ensure_started()
            client.reconnect()
            ctx.remote_disconnect_modal_open.set(False)
            ui.modal_remove()
            ui.notification_show(
                _("Reconnecting to Kittyflap, please wait..."),
                duration=15,
                type="message",
            )
        except Exception as e:
            ui.notification_show(
                _("Failed to reconnect to Kittyflap: {}.").format(e),
                duration=8,
                type="error",
            )

    @output
    @render.ui
    def remote_initial_sync_status_ui():
        st = ctx.remote_sync_status.get() or {}
        requested = bool(st.get("requested"))
        in_progress = bool(st.get("in_progress"))
        ok = st.get("ok")
        reason = (st.get("reason") or "").strip()
        bytes_mb = float(st.get("bytes_received") or 0) / (1024.0 * 1024.0)
        items = st.get("items") or []
        items_count = len(items) if isinstance(items, list) else 0

        if not requested:
            headline = _("Waiting for sync to start...")
        elif in_progress:
            headline = _("Initial sync in progress...")
        elif ok is True:
            headline = _("Initial sync completed.")
        elif ok is False:
            headline = _("Initial sync failed.")
        else:
            headline = _("Connecting to Kittyflap...")

        detail = _("Transferred: {:.1f} MB").format(bytes_mb)
        if items_count:
            detail = _("Transferred ({} items): {:.1f} MB").format(
                items_count, bytes_mb
            )

        extra = ""
        if requested and ok is False and reason:
            extra = _("Reason: {} ").format(reason)

        return ui.div(
            ui.markdown(f"**{headline}**"),
            ui.div(detail),
            ui.div(extra) if extra else ui.HTML(""),
        )

    @reactive.Effect
    def _remote_sync_poller():
        # Poll RemoteControlClient to keep the modal state in sync and survive refreshes.
        if not is_remote_mode():
            return

            # Always keep restart marker-driven flow visible.
        try:
            if os.path.exists(ctx._remote_restart_pending_marker_path()):
                if not bool(ctx.remote_restart_modal_open.get()):
                    show_remote_restart_required_modal()
                    ctx.remote_restart_modal_open.set(True)
                return
        except Exception:
            pass

        if not bool(ctx.remote_setup_active.get()):
            return
        if not bool(CONFIG.get("REMOTE_SYNC_ON_FIRST_CONNECT", True)):
            return

        try:
            from src.remote.control_client import RemoteControlClient

            client = RemoteControlClient.instance()
            client.ensure_started()
            st = client.get_sync_status()
        except Exception:
            reactive.invalidate_later(1.0)
            return

        ctx.remote_sync_status.set(st)
        requested = bool(st.get("requested"))
        in_progress = bool(st.get("in_progress"))
        ok = st.get("ok")

        # If a sync was requested (even if not yet in_progress), show modal.
        if (
            requested
            and (in_progress or ok is None)
            and not bool(ctx.remote_sync_modal_open.get())
        ):
            ui.modal_remove()
            show_remote_initial_sync_modal()
            ctx.remote_sync_modal_open.set(True)

            # Timeouts (best effort): if requested but never finishes, mark as failed.
        try:
            timeout_s = float(CONFIG.get("REMOTE_CONTROL_TIMEOUT") or 10.0)
        except Exception:
            timeout_s = 10.0
        connect_timeout = max(20.0, min(120.0, float(timeout_s or 10.0) * 4.0))
        sync_timeout = max(180.0, float(timeout_s or 10.0) * 120.0)
        try:
            requested_at = float(st.get("requested_at") or 0.0)
            started_at = float(st.get("started_at") or 0.0)
            requested_at_mono = float(st.get("requested_at_mono") or 0.0)
            started_at_mono = float(st.get("started_at_mono") or 0.0)
        except Exception:
            requested_at = 0.0
            started_at = 0.0
            requested_at_mono = 0.0
            started_at_mono = 0.0

        if requested_at_mono:
            now_mono = monotonic_time()
            age = max(0.0, now_mono - requested_at_mono)
        else:
            now_wall = tm.time()
            age = max(0.0, now_wall - requested_at) if requested_at else 0.0

        if started_at_mono:
            now_mono = monotonic_time()
            sync_age = max(0.0, now_mono - started_at_mono)
        else:
            now_wall = tm.time()
            sync_age = max(0.0, now_wall - started_at) if started_at else 0.0

        if requested and ok is None and not in_progress and age > connect_timeout:
            show_remote_initial_sync_modal(
                error_reason=_("Could not connect to the remote Kittyflap.")
            )
            ctx.remote_sync_modal_open.set(True)
            reactive.invalidate_later(2.0)
            return

        if (
            requested
            and ok is None
            and (in_progress or started_at)
            and sync_age > sync_timeout
        ):
            show_remote_initial_sync_modal(error_reason=_("Initial sync timed out."))
            ctx.remote_sync_modal_open.set(True)
            reactive.invalidate_later(2.0)
            return

            # Sync finished.
        if (
            requested
            and not in_progress
            and ok is True
            and not bool(ctx.remote_sync_finalized.get())
        ):
            ctx.remote_sync_finalized.set(True)

            # Sync may have replaced config.ini: reload it now so next startup uses the synced values.
            try:
                load_config()
            except Exception as e:
                ui.notification_show(
                    _("Failed to reload synced config.ini: {}.").format(e),
                    duration=12,
                    type="error",
                )
                return

                # Trigger immediate UI refresh after synced DB/config are applied.
            try:
                reload_trigger_photos.set(reload_trigger_photos.get() + 1)
                ctx.reload_trigger_cats.set(ctx.reload_trigger_cats.get() + 1)
                reload_trigger_config.set(reload_trigger_config.get() + 1)
                ctx.reload_trigger_info.set(ctx.reload_trigger_info.get() + 1)
            except Exception:
                pass

                # Mark "restart required" to persist across refresh.
            try:
                with open(
                    ctx._remote_restart_pending_marker_path(), "w", encoding="utf-8"
                ) as f:
                    f.write(str(tm.time()))
            except Exception:
                pass

            startup.remote_setup_required = False
            ctx.remote_setup_active.set(False)
            ui.modal_remove()
            show_remote_restart_required_modal()
            ctx.remote_restart_modal_open.set(True)
            return

        if requested and not in_progress and ok is False:
            reason = (st.get("reason") or "").strip()
            show_remote_initial_sync_modal(error_reason=reason or _("unknown error"))
            ctx.remote_sync_modal_open.set(True)
            reactive.invalidate_later(2.0)
            return

        reactive.invalidate_later(0.25)

    @reactive.Effect
    @reactive.event(input.btn_remote_setup_save)
    def on_remote_setup_save():
        if not (is_remote_mode() and ctx.remote_setup_active.get()):
            return
        host = (input.remote_target_host() or "").strip()
        if not host:
            ui.notification_show(
                _("Remote target host must not be empty."), duration=8, type="error"
            )
            return
        port = 8888
        try:
            timeout = float(
                read_remote_config_values().get("remote_control_timeout", 30.0) or 30.0
            )
        except Exception:
            timeout = 30.0
        if timeout <= 0:
            timeout = 30.0

        sync_first = bool(input.remote_sync_on_first_connect())
        sync_labelstudio = bool(input.remote_sync_labelstudio())
        remote_cfg_path = os.path.join(kittyhack_root(), "config.remote.ini")
        parser = configparser.ConfigParser()
        parser["Settings"] = {
            "remote_target_host": host,
            "remote_control_port": str(port),
            "remote_control_timeout": str(timeout),
            "remote_sync_on_first_connect": str(sync_first),
            "remote_sync_labelstudio": str(sync_labelstudio),
        }
        try:
            with open(remote_cfg_path, "w", encoding="utf-8") as f:
                parser.write(f)
        except Exception as e:
            ui.notification_show(
                _("Failed to write config.remote.ini: {}").format(e),
                duration=10,
                type="error",
            )
            return

        CONFIG["REMOTE_TARGET_HOST"] = host
        CONFIG["REMOTE_CONTROL_PORT"] = port
        CONFIG["REMOTE_CONTROL_TIMEOUT"] = timeout
        CONFIG["REMOTE_SYNC_ON_FIRST_CONNECT"] = sync_first
        CONFIG["REMOTE_SYNC_LABELSTUDIO"] = sync_labelstudio

        if sync_first:
            # Start sync (non-blocking). Progress is shown in a non-closable modal and
            # survives browser refresh by polling the RemoteControlClient status.
            try:
                from src.remote.control_client import RemoteControlClient

                client = RemoteControlClient.instance()
                client.ensure_started()
                client.start_initial_sync(force=True)
                ctx.remote_sync_status.set(client.get_sync_status())
            except Exception as e:
                ui.notification_show(
                    _("Failed to initialize remote control client: {}.").format(e),
                    duration=12,
                    type="error",
                )
                return

            ui.modal_remove()
            show_remote_initial_sync_modal()
            ctx.remote_sync_modal_open.set(True)
            return

        startup.remote_setup_required = False
        ctx.remote_setup_active.set(False)
        ui.modal_remove()
        ui.notification_show(
            _("Remote-mode configuration saved. Starting services..."),
            duration=8,
            type="message",
        )

        startup.start_backend_if_needed()
        startup.start_background_task_if_needed()

    @reactive.Effect
    @reactive.event(input.btn_remote_sync_failed_close)
    def _remote_sync_failed_close():
        # Allow user to go back to the remote setup form after a sync failure.
        if not is_remote_mode():
            return
        ctx.remote_sync_modal_open.set(False)
        ui.modal_remove()
        show_remote_setup_modal()

    @reactive.Effect
    @reactive.event(input.btn_remote_sync_abort)
    def _remote_sync_abort():
        if not is_remote_mode():
            return

        try:
            from src.remote.control_client import RemoteControlClient

            client = RemoteControlClient.instance()
            client.abort_initial_sync(reason="aborted by user")
        except Exception:
            pass

            # Clear target host and persist cleanup in config.remote.ini/config.ini.
        CONFIG["REMOTE_TARGET_HOST"] = ""
        try:
            update_single_config_parameter("REMOTE_TARGET_HOST")
        except Exception:
            pass

        ctx.remote_sync_status.set(
            {
                "requested": False,
                "in_progress": False,
                "ok": False,
                "reason": "aborted by user",
                "requested_at": 0.0,
                "started_at": 0.0,
                "finished_at": tm.time(),
                "bytes_received": 0,
                "items": [],
            }
        )
        ctx.remote_sync_modal_open.set(False)
        ctx.remote_sync_finalized.set(False)
        ctx.remote_setup_active.set(True)

        ui.modal_remove()
        ui.notification_show(
            _("Initial sync aborted. Remote target host has been cleared."),
            duration=8,
            type="message",
        )
        show_remote_setup_modal()

    @reactive.Effect
    @reactive.event(input.btn_remote_restart_kittyhack)
    def _remote_restart_after_sync():
        if not is_remote_mode():
            return
            # Best effort: clear marker before restart so it doesn't reappear after restart.
        try:
            if os.path.exists(ctx._remote_restart_pending_marker_path()):
                os.remove(ctx._remote_restart_pending_marker_path())
        except Exception:
            pass

        ui.notification_show(
            _("Restarting Kittyhack service..."), duration=10, type="message"
        )

        def _do_restart():
            try:
                tm.sleep(0.75)
                ServiceOps.systemctl("restart", "kittyhack")
            except Exception:
                pass

        threading.Thread(target=_do_restart, daemon=True).start()
        # The service restart will typically terminate this process shortly.
        ui.modal_remove()

        # Show a notification if a new version of Kittyhack is available

    if (
        CONFIG["LATEST_VERSION"] != "unknown"
        and not Versioning.is_same_kittyhack_version(
            startup.git_version, CONFIG["LATEST_VERSION"]
        )
        and CONFIG["PERIODIC_VERSION_CHECK"]
    ):
        ui.notification_show(
            _(
                "A new version of Kittyhack is available: {}. Go to the [INFO] section for update instructions."
            ).format(CONFIG["LATEST_VERSION"]),
            duration=10,
            type="message",
        )

        # Show a warning if the remaining disk space is below the critical threshold
    kittyflap_db_file_exists = os.path.exists(CONFIG["DATABASE_PATH"])
    if startup.free_disk_space < 500:
        if kittyflap_db_file_exists:
            additional_info = _(
                " or consider deleting pictures from the original kittyflap database file. For more details, see the [INFO] section."
            )
        else:
            additional_info = ""
        ui.notification_show(
            _(
                "Remaining disk space is low: {:.1f} MB. Please free up some space (e.g. reduce the max amount of pictures in the database{})."
            ).format(startup.free_disk_space, additional_info),
            duration=20,
            type="warning",
        )

        # Monitor updates of the database
    sess_last_imgblock_ts = [last_imgblock_ts.get_timestamp()]

    # Auto-refresh AI Training view while a finalized model is being downloaded/installed.
    # This keeps the UI responsive and updates the progress text without requiring manual reload.
    _model_dl_last_finished_at = [0.0]

    @reactive.effect
    def auto_refresh_ai_training_during_model_download():
        reactive.invalidate_later(2)
        try:
            state = RemoteModelTrainer.get_model_download_state()
        except Exception:
            return

        status = (state or {}).get("status")
        if status in ("downloading", "extracting"):
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)

        finished_at = float((state or {}).get("finished_at") or 0.0)
        if finished_at > 0.0 and finished_at != float(
            _model_dl_last_finished_at[0] or 0.0
        ):
            _model_dl_last_finished_at[0] = finished_at
            # Finalize completion in the main process (clears MODEL_TRAINING, stores notification)
            try:
                RemoteModelTrainer.check_model_training_result(show_notification=False)
            except Exception:
                pass
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)
            reload_trigger_config.set(reload_trigger_config.get() + 1)

    @reactive.Effect
    @reactive.event(input.main_nav, ignore_none=True)
    def refresh_ai_training_on_tab_activate():
        try:
            current_tab = str(input.main_nav() or "").strip()
        except Exception:
            return

        if current_tab == "ai-training":
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)

            # Show user notifications if there are any

    show_user_notifications()

    # Migration in progress notice
    @reactive.effect
    def migration_progress_notification():
        reactive.invalidate_later(15)
        if len(startup.ids_with_original_blob) > 0:
            # Estimate remaining time: 15 seconds per 200 IDs (10s processing + 5s delay)
            batches_remaining = math.ceil(len(startup.ids_with_original_blob) / 200)
            remaining_seconds = batches_remaining * 15
            remaining_minutes = max(1, round(remaining_seconds / 60))
            ui.notification_show(
                _(
                    "Database migration in progress. Performance may be a bit slower until finished ({} pictures remaining, ~{} min)."
                ).format(len(startup.ids_with_original_blob), remaining_minutes),
                duration=14,
                type="warning",
            )

    @reactive.effect
    def ext_trigger_reload_photos():
        reactive.invalidate_later(3)
        if last_imgblock_ts.get_timestamp() != sess_last_imgblock_ts[0]:
            sess_last_imgblock_ts[0] = last_imgblock_ts.get_timestamp()
            reload_trigger_photos.set(reload_trigger_photos.get() + 1)
            logging.info("Reloading photos due to external trigger.")

    @reactive.effect
    def periodic_ram_check():
        reactive.invalidate_later(60)
        used_ram_space = SystemInfo.get_used_ram_space()
        total_ram_space = SystemInfo.get_total_ram_space()
        ram_usage_percentage = (used_ram_space / total_ram_space) * 100
        if ram_usage_percentage >= 90:
            if LabelStudioInstall.get_labelstudio_status() == True:
                additional_text = " " + _(
                    "LabelStudio is running. If you do not need it anymore to label images, please stop it in the [AI TRAINING] section."
                )
            else:
                additional_text = ""
            ui.notification_show(
                _("Warning: RAM usage is at {:.1f}%!{}").format(
                    ram_usage_percentage, additional_text
                ),
                duration=20,
                type="warning",
            )

    @reactive.effect
    def update_live_status():
        reactive.invalidate_later(0.25)

        def _set_live_status_if_changed(new_state: dict) -> None:
            # IMPORTANT: Use reactive.isolate() when reading live_status here.
            # Without it, this effect would take a reactive dependency on live_status,
            # creating a tight invalidation loop whenever the value changes (e.g. during
            # prey-detection cooldown where time_until_release changes every tick).
            try:
                with reactive.isolate():
                    prev = ctx.live_status.get() or {}
            except Exception:
                prev = {}
            if prev != new_state:
                ctx.live_status.set(new_state)

        try:
            if is_remote_mode():
                remote_ready = False
                try:
                    from src.remote.control_client import RemoteControlClient

                    client = RemoteControlClient.instance()
                    client.ensure_started()
                    remote_ready = bool(client.wait_until_ready(timeout=0))
                except Exception:
                    remote_ready = False

                if not remote_ready:
                    _set_live_status_if_changed({"ok": False, "remote_waiting": True})

                    now = monotonic_time()
                    last_log = getattr(
                        update_live_status, "_last_remote_not_ready_log", 0.0
                    )
                    if (now - float(last_log or 0.0)) > 10.0:
                        logging.info(
                            "[LIVE_STATUS] Remote control not connected yet; skipping live status update."
                        )
                        update_live_status._last_remote_not_ready_log = now

                    try:
                        ui.update_action_button(
                            "bManualOverride",
                            label=_("Unlock inside"),
                            icon=icon_svg("unlock"),
                            disabled=True,
                        )
                    except Exception:
                        pass
                    try:
                        ui.update_action_button("bResetPreyCooldown", disabled=True)
                    except Exception:
                        pass
                    return

            magnets = getattr(Magnets, "instance", None)
            if magnets is None:
                # During boot the backend (and thus Magnets.init()) may not be ready yet.
                _set_live_status_if_changed({"ok": False, "remote_waiting": False})

                now = monotonic_time()
                last_log = getattr(update_live_status, "_last_hw_not_ready_log", 0.0)
                if (now - float(last_log or 0.0)) > 10.0:
                    logging.info(
                        "[LIVE_STATUS] Hardware not yet initialized; skipping live status update."
                    )
                    update_live_status._last_hw_not_ready_log = now

                    # Keep buttons disabled until we have a valid magnet instance.
                try:
                    ui.update_action_button(
                        "bManualOverride",
                        label=_("Unlock inside"),
                        icon=icon_svg("unlock"),
                        disabled=True,
                    )
                except Exception:
                    pass
                try:
                    ui.update_action_button("bResetPreyCooldown", disabled=True)
                except Exception:
                    pass
                return

            inside_lock_state = magnets.get_inside_state()
            outside_lock_state = magnets.get_outside_state()

            from src.backend import motion_state, motion_state_lock

            with motion_state_lock:
                outside_motion_state = motion_state["outside"]
                inside_motion_state = motion_state["inside"]

            prey_detection_mono = float(
                getattr(backend_main, "prey_detection_mono", 0.0) or 0.0
            )
            if prey_detection_mono > 0.0:
                delta_to_last_prey_detection = monotonic_time() - prey_detection_mono
                time_until_release = (
                    float(CONFIG["LOCK_DURATION_AFTER_PREY_DETECTION"])
                    - delta_to_last_prey_detection
                )
                forced_lock_due_prey = time_until_release > 0
            else:
                delta_to_last_prey_detection = 0
                time_until_release = 0
                forced_lock_due_prey = False

            if not forced_lock_due_prey:
                # Keep stable values when no prey lock is active to avoid unnecessary UI re-renders.
                delta_to_last_prey_detection = 0
                time_until_release = 0
            else:
                # Round to whole seconds so the dict stays identical across sub-second ticks,
                # preventing unnecessary reactive invalidations that would flood the UI.
                time_until_release = float(int(time_until_release))
                delta_to_last_prey_detection = float(int(delta_to_last_prey_detection))

            _set_live_status_if_changed(
                {
                    "ok": True,
                    "remote_waiting": False,
                    "inside_lock": bool(inside_lock_state),
                    "outside_lock": bool(outside_lock_state),
                    "inside_motion": bool(inside_motion_state),
                    "outside_motion": bool(outside_motion_state),
                    "forced_lock_due_prey": bool(forced_lock_due_prey),
                    "time_until_release": float(max(0, time_until_release)),
                    "delta_to_last_prey_detection": float(
                        max(0, delta_to_last_prey_detection)
                    ),
                }
            )

            try:
                if inside_lock_state:
                    ui.update_action_button(
                        "bManualOverride",
                        label=_("Close inside now"),
                        icon=icon_svg("lock"),
                        disabled=False,
                    )
                else:
                    ui.update_action_button(
                        "bManualOverride",
                        label=_("Open inside now"),
                        icon=icon_svg("lock-open"),
                        disabled=False,
                    )
            except Exception:
                pass

            try:
                if forced_lock_due_prey:
                    ui.update_action_button("bResetPreyCooldown", disabled=False)
                else:
                    ui.update_action_button("bResetPreyCooldown", disabled=True)
            except Exception:
                pass
        except Exception as e:
            # Throttle errors here; during boot transient None/IO errors can happen.
            now = monotonic_time()
            last_log = getattr(update_live_status, "_last_error_log", 0.0)
            if (now - float(last_log or 0.0)) > 10.0:
                logging.exception("Failed to update live status: %s", e)
                update_live_status._last_error_log = now
            _set_live_status_if_changed({"ok": False, "remote_waiting": False})

    @render.ui
    def live_view_aspect_style():
        # Updated whenever the image renderer learns a new aspect ratio.
        w, h = live_view_aspect.get() or (4, 3)
        w = int(w or 4)
        h = int(h or 3)
        w = max(1, w)
        h = max(1, h)
        return ui.HTML(
            f"<style>#live_view_stage{{--kh-live-aspect:{w} / {h};}}</style>"
        )

    @render.ui
    def live_view_warning():
        return ui.HTML(ctx.live_view_warning_html.get() or "")

    @render.ui
    def live_view_image():
        # Allows other code paths (e.g., config save) to force an immediate refresh.
        __ = live_view_refresh_nonce.get()

        refresh_s = float(CONFIG.get("LIVE_VIEW_REFRESH_INTERVAL", 2.0) or 2.0)
        refresh_s = max(0.1, refresh_s)

        # Mark running to avoid concurrent forced refresh triggers.
        live_view_image._is_running = True

        if not hasattr(live_view_image, "last_frame_hash"):
            live_view_image.last_frame_hash = None
            live_view_image.last_change_time = monotonic_time()
            live_view_image.no_frame_since = None
            live_view_image.last_ar_w = 4
            live_view_image.last_ar_h = 3
            live_view_image.visible_frame_jpg = None
            live_view_image.visible_frame_hash = None
            live_view_image.preload_frame_jpg = None
            live_view_image.preload_frame_hash = None
            live_view_image.last_camera_key = None
            live_view_image._last_aspect_set = (4, 3)

        def _set_aspect(w: int, h: int) -> None:
            try:
                w = int(w)
                h = int(h)
                if w <= 0 or h <= 0:
                    return
                prev = getattr(live_view_image, "_last_aspect_set", None)
                if tuple(prev or ()) != (w, h):
                    live_view_image._last_aspect_set = (w, h)
                    live_view_aspect.set((w, h))
            except Exception:
                return

        def _spinner_html() -> str:
            # Keep spinner phase continuous across output rerenders.
            try:
                phase_s = float(monotonic_time() % 1.0)
            except Exception:
                phase_s = 0.0
            return f'<div class="spinner-container"><div class="spinner" style="animation-delay:-{phase_s:.3f}s;"></div></div>'

        camera_key = (CONFIG.get("CAMERA_SOURCE"), CONFIG.get("IP_CAMERA_URL"))
        if live_view_image.last_camera_key is None:
            live_view_image.last_camera_key = camera_key
        elif camera_key != live_view_image.last_camera_key:
            # Camera config changed: never show an old buffered frame.
            live_view_image.last_camera_key = camera_key
            live_view_image.last_frame_hash = None
            live_view_image.last_change_time = monotonic_time()
            live_view_image.visible_frame_jpg = None
            live_view_image.visible_frame_hash = None
            live_view_image.preload_frame_jpg = None
            live_view_image.preload_frame_hash = None
            live_view_image.no_frame_since = None
            live_view_image.last_ar_w = 4
            live_view_image.last_ar_h = 3
            _set_aspect(4, 3)

        try:
            if getattr(live_view_image, "preload_frame_jpg", None) is not None:
                live_view_image.visible_frame_jpg = live_view_image.preload_frame_jpg
                live_view_image.visible_frame_hash = live_view_image.preload_frame_hash
                live_view_image.preload_frame_jpg = None
                live_view_image.preload_frame_hash = None

            frame = model_handler.get_camera_frame()
            if frame is None:
                if getattr(live_view_image, "no_frame_since", None) is None:
                    live_view_image.no_frame_since = monotonic_time()
                no_frame_elapsed = max(
                    0.0, monotonic_time() - float(live_view_image.no_frame_since or 0.0)
                )

                if CONFIG.get("CAMERA_SOURCE") == "ip_camera":
                    cam_state = model_handler.get_camera_state()
                    cam_state_text = (
                        str(cam_state) if cam_state is not None else _("Unknown")
                    )
                    startup_grace_s = 12.0

                    if no_frame_elapsed < startup_grace_s:
                        img_html = (
                            '<div class="placeholder-image" style="padding-top: 20px; padding-bottom: 20px;">'
                            "<div></div>"
                            "<div><strong>"
                            + _("Connecting to the IP camera...")
                            + "</strong></div>"
                            "<div>"
                            + _(
                                "Please wait a few seconds while the stream is initialized."
                            )
                            + "</div>"
                            + _spinner_html()
                            + "<div>"
                            + _("Current status: ")
                            + cam_state_text
                            + "</div>"
                            "<div></div>"
                            "</div>"
                        )
                    else:
                        reconnect_hint = ""
                        if no_frame_elapsed >= 25.0:
                            reconnect_hint = (
                                "<div>"
                                + _(
                                    "If the stream does not recover, verify the URL, camera network connection, and camera credentials."
                                )
                                + "</div>"
                            )
                        img_html = (
                            '<div class="placeholder-image" style="padding-top: 20px; padding-bottom: 20px;">'
                            "<div></div>"
                            "<div><strong>"
                            + _("Connection to the IP camera failed.")
                            + "</strong></div>"
                            "<div>"
                            + _(
                                "Please check the stream URL and the network connection of your IP camera."
                            )
                            + "</div>"
                            + _spinner_html()
                            + "<div>"
                            + _(
                                "If you have just changed the camera settings, please wait a few seconds for the camera to reconnect."
                            )
                            + "</div>"
                            "<div>"
                            + _("Current status: ")
                            + cam_state_text
                            + "</div>"
                            + reconnect_hint
                            + "<div></div>"
                            "</div>"
                        )
                else:
                    extra_hint = ""
                    if (not is_remote_mode()) and (no_frame_elapsed >= 25.0):
                        extra_hint = (
                            "<div>"
                            + _(
                                'If this message does not disappear within 60 seconds, please (re-)install the required camera drivers with the "Reinstall Camera Driver" button in the "System" section.'
                            )
                            + "</div>"
                        )
                    img_html = (
                        '<div class="placeholder-image" style="padding-top: 20px; padding-bottom: 20px;">'
                        "<div></div>"
                        "<div><strong>"
                        + _("Connection to the camera failed.")
                        + "</strong></div>"
                        "<div>"
                        + _("Please wait...")
                        + "</div>"
                        + _spinner_html()
                        + extra_hint
                        + "<div></div>"
                        "</div>"
                    )
            else:
                live_view_image.no_frame_since = None
                frame_jpg = model_handler.get_camera_frame_jpg()
                frame_hash = hashlib.md5(frame_jpg).hexdigest() if frame_jpg else None

                if frame_jpg:
                    try:
                        h, w = frame.shape[:2]
                        if w > 0 and h > 0:
                            live_view_image.last_ar_w = int(w)
                            live_view_image.last_ar_h = int(h)
                            _set_aspect(
                                live_view_image.last_ar_w, live_view_image.last_ar_h
                            )
                    except Exception:
                        pass

                    if frame_hash != live_view_image.last_frame_hash:
                        live_view_image.last_change_time = monotonic_time()
                        live_view_image.last_frame_hash = frame_hash
                        live_view_image.preload_frame_jpg = frame_jpg
                        live_view_image.preload_frame_hash = frame_hash

                    if (
                        monotonic_time() - live_view_image.last_change_time > 5
                        and CONFIG.get("CAMERA_SOURCE") == "ip_camera"
                    ):
                        img_html = (
                            '<div class="placeholder-image" style="padding-top: 20px; padding-bottom: 20px;">'
                            "<div></div>"
                            "<div><strong>"
                            + _("Camera stream appears to be frozen.")
                            + "</strong></div>"
                            "<div>"
                            + _(
                                "Please check your external IP camera connection or network settings."
                            )
                            + "</div>"
                            + _spinner_html()
                            + "<div></div>"
                            "</div>"
                        )
                    else:
                        front_style = 'style="position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;z-index:2;"'
                        back_style = 'style="position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;z-index:1;"'
                        visible_html = ""
                        preload_html = ""

                        if getattr(live_view_image, "visible_frame_jpg", None):
                            visible_b64 = base64.b64encode(
                                live_view_image.visible_frame_jpg
                            ).decode("utf-8")
                            visible_html = f'<img src="data:image/jpeg;base64,{visible_b64}" {front_style} />'
                        else:
                            now_b64 = base64.b64encode(frame_jpg).decode("utf-8")
                            visible_html = f'<img src="data:image/jpeg;base64,{now_b64}" {front_style} />'
                            live_view_image.visible_frame_jpg = frame_jpg
                            live_view_image.visible_frame_hash = frame_hash
                            live_view_image.preload_frame_jpg = None
                            live_view_image.preload_frame_hash = None

                        if getattr(live_view_image, "preload_frame_jpg", None):
                            preload_b64 = base64.b64encode(
                                live_view_image.preload_frame_jpg
                            ).decode("utf-8")
                            preload_html = f'<img src="data:image/jpeg;base64,{preload_b64}" {back_style} aria-hidden="true" />'

                        img_html = f"{preload_html}{visible_html}"
                else:
                    img_html = (
                        f'<div class="placeholder-image"><strong>'
                        + _("Could not read the picture from the camera.")
                        + "</strong></div>"
                    )

            result = ui.HTML(img_html)
        except Exception as e:
            logging.error(f"Failed to fetch the live view image: {e}")
            result = ui.HTML(
                '<div class="placeholder-image"><strong>'
                + _("An error occured while fetching the live view image.")
                + "</strong></div>"
            )

        finally:
            live_view_image._is_running = False
            if getattr(live_view_image, "_refresh_after_run", False):
                live_view_image._refresh_after_run = False
                try:
                    live_view_refresh_nonce.set(live_view_refresh_nonce.get() + 1)
                except Exception:
                    pass

                    # Schedule the next refresh only after this render completed.
        reactive.invalidate_later(refresh_s)
        return result

    @render.ui
    def live_view_overlay_clock():
        # Clock stays on top of the picture.
        reactive.invalidate_later(1.0)
        ts = datetime.now(ZoneInfo(CONFIG["TIMEZONE"])).strftime("%H:%M:%S")

        clock_label = _("Clock")
        clock_title = _("Current time")
        return ui.HTML(
            f'<div class="live-view-overlay-clock" aria-label="{clock_label}" title="{clock_title}">{ts}</div>'
        )

    @render.ui
    def live_view_overlay_status():
        # Status chips (inside/outside lock + motion) are rendered below the picture.
        st = ctx.live_status.get() or {}

        def _pill_html(
            inner_html: str, variant: str, *, aria_label: str, title: str
        ) -> str:
            return (
                f'<span class="live-view-pill live-view-pill--{variant}" '
                f'aria-label="{aria_label}" title="{title}">'
                f"{inner_html}"
                f"</span>"
            )

        def _icon_html(name: str) -> str:
            try:
                return str(icon_svg(name, margin_left="0", margin_right="0"))
            except Exception:
                return "•"

        inside_label = _("Inside")
        outside_label = _("Outside")
        status_unavailable_label = _("Status unavailable")

        remote_indicator_html = ""
        if is_remote_mode():
            host, is_ready, manual_disconnect = ctx._remote_connection_state()

            remote_variant = "muted"
            remote_title = _("Remote target host not configured")
            remote_aria = _("Remote: not configured")

            if host and is_ready:
                remote_variant = "ok"
                remote_title = _("Connected to Kittyflap") + f": {host}"
                remote_aria = _("Remote: connected")
            elif host and manual_disconnect:
                remote_variant = "muted"
                remote_title = _("Disconnected from Kittyflap") + f": {host}"
                remote_aria = _("Remote: disconnected")
            elif host:
                remote_variant = "blocked"
                remote_title = _("Connecting to Kittyflap") + f": {host}"
                remote_aria = _("Remote: connecting")

            remote_indicator_html = (
                '<div class="remote-connection-indicator" '
                f'aria-label="{remote_aria}">' + '<div class="remote-connection-dot" '
                f'aria-label="{remote_aria}" title="{remote_title}" data-bs-title="{remote_title}" '
                'data-bs-toggle="tooltip" data-bs-trigger="hover focus" data-bs-placement="top" tabindex="0">'
                + (
                    f'<span class="remote-status-chip remote-status-chip--{remote_variant}" '
                    f'role="img" aria-label="{remote_aria}"></span>'
                )
                + "</div>"
                + "</div>"
            )

        if st.get("ok"):
            inside_lock_is_open = bool(st.get("inside_lock"))
            outside_lock_is_open = bool(st.get("outside_lock"))
            inside_motion = bool(st.get("inside_motion"))
            outside_motion = bool(st.get("outside_motion"))

            inside_lock_variant = "ok" if inside_lock_is_open else "muted"
            outside_lock_variant = "ok" if outside_lock_is_open else "muted"
            inside_motion_variant = "active" if inside_motion else "muted"
            outside_motion_variant = "active" if outside_motion else "muted"

            inside_lock_icon = _icon_html(
                "lock-open" if inside_lock_is_open else "lock"
            )
            outside_lock_icon = _icon_html(
                "lock-open" if outside_lock_is_open else "lock"
            )
            inside_motion_icon = _icon_html("eye" if inside_motion else "eye-slash")
            outside_motion_icon = _icon_html("eye" if outside_motion else "eye-slash")

            prey_banner_html = ""
            if st.get("forced_lock_due_prey"):
                prey_banner_html = (
                    '<div class="live-view-banner live-view-banner-warn live-view-overlay-banner" role="status">'
                    + _(
                        "Prey detected {0:.0f}s ago. Inside lock remains closed for {1:.0f}s."
                    ).format(
                        float(st.get("delta_to_last_prey_detection", 0.0)),
                        float(st.get("time_until_release", 0.0)),
                    )
                    + "</div>"
                )

            status_html = (
                '<div class="live-view-statusbar">'
                '<div class="live-view-overlay-row">'
                '<div class="live-view-overlay-chip">'
                f'<span class="live-view-overlay-title">{inside_label}</span>'
                + _pill_html(
                    inside_lock_icon,
                    inside_lock_variant,
                    aria_label=_("Inside lock: Open")
                    if inside_lock_is_open
                    else _("Inside lock: Closed"),
                    title=_("Inside lock: Open")
                    if inside_lock_is_open
                    else _("Inside lock: Closed"),
                )
                + _pill_html(
                    inside_motion_icon,
                    inside_motion_variant,
                    aria_label=_("Inside motion: Motion")
                    if inside_motion
                    else _("Inside motion: No motion"),
                    title=_("Inside motion: Motion")
                    if inside_motion
                    else _("Inside motion: No motion"),
                )
                + "</div>"
                '<div class="live-view-overlay-chip">'
                f'<span class="live-view-overlay-title">{outside_label}</span>'
                + _pill_html(
                    outside_lock_icon,
                    outside_lock_variant,
                    aria_label=_("Outside lock: Open")
                    if outside_lock_is_open
                    else _("Outside lock: Closed"),
                    title=_("Outside lock: Open")
                    if outside_lock_is_open
                    else _("Outside lock: Closed"),
                )
                + _pill_html(
                    outside_motion_icon,
                    outside_motion_variant,
                    aria_label=_("Outside motion: Motion")
                    if outside_motion
                    else _("Outside motion: No motion"),
                    title=_("Outside motion: Motion")
                    if outside_motion
                    else _("Outside motion: No motion"),
                )
                + "</div>"
                + "</div>"
                + remote_indicator_html
                + prey_banner_html
                + "</div>"
            )
        else:
            unavailable_text = status_unavailable_label
            if is_remote_mode() and bool(st.get("remote_waiting")):
                unavailable_text = _("Remote control not connected")

            status_html = (
                '<div class="live-view-statusbar">'
                '<div class="live-view-overlay-row">'
                f'<div class="live-view-overlay-chip live-view-overlay-chip--error">{unavailable_text}</div>'
                + "</div>"
                + remote_indicator_html
                + "</div>"
            )

        return ui.HTML(status_html)

    @reactive.Effect
    def immediate_bg_task_site_load():
        startup.immediate_bg_task("site load")

    @reactive.Effect
    @reactive.event(input.button_today)
    def immediate_bg_task_reload_button():
        startup.immediate_bg_task("today button")

    @reactive.Effect
    @reactive.event(input.button_detection_overlay)
    def update_config_images_with_overlay():
        CONFIG["SHOW_IMAGES_WITH_OVERLAY"] = input.button_detection_overlay()
        update_single_config_parameter("SHOW_IMAGES_WITH_OVERLAY")

    @reactive.Effect
    @reactive.event(input.button_events_view)
    def update_config_group_pictures_to_events():
        CONFIG["GROUP_PICTURES_TO_EVENTS"] = input.button_events_view()
        update_single_config_parameter("GROUP_PICTURES_TO_EVENTS")

        # Cross-module hooks used by configuration / ai_training

    ctx.show_user_notifications = show_user_notifications
    ctx.live_view_image = live_view_image
