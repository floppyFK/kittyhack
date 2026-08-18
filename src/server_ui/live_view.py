"""Live view tab UI, door actions, last events."""

import os
import pandas as pd
from datetime import datetime, timedelta
from shiny import render, ui, reactive
import logging
import html as html_module
from zoneinfo import ZoneInfo
from faicons import icon_svg
import hashlib
from src.baseconfig import (
    CONFIG,
    AllowedToEnter,
    set_language,
    update_single_config_parameter,
)
from src.helper import (
    DateTimeUtil,
    EventType,
)
from src.database import (
    CatsRepo,
    EventsRepo,
    ReturnDataCatDB,
)
from src.event_timeline import (
    timeline_entries_to_html,
    timeline_fallback_from_event_type,
    timeline_extract_latest_event,
)
from src.mode import is_remote_mode
from src.backend import backend_main, update_mqtt_config, manual_door_override
from src.server_ui.state import reload_trigger_photos, reload_trigger_config
from src.server_ui.context import SessionContext

_ = set_language(CONFIG["LANGUAGE"])

if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir

from src.server_ui.event_modal import btn_show_event, show_event_server


def register_live_view(input, output, session, ctx: SessionContext):
    """Register Live View tab UI, door actions, and last-events handlers."""

    @output
    @render.ui
    def ui_live_view():
        live_view = ui.card(
            ui.output_ui("live_view_aspect_style"),
            ui.output_ui("live_view_warning_panel"),
            ui.div(
                ui.output_ui("live_view_image"),
                ui.output_ui("live_view_overlay_clock"),
                id="live_view_stage",
                class_="live-view-stage",
            ),
            ui.output_ui("live_view_overlay_status"),
            full_screen=False,
            class_="image-container live-view-card",
        )
        return ui.div(
            live_view,
        )

    @render.ui
    def live_view_warning_panel():
        # Dedicated, stable warning slot outside image processing/render path.
        html = ctx.live_view_warning_html.get() or ""
        if not html:
            return ui.HTML("")
        return ui.div(
            ui.HTML(html),
            ui.div(
                ui.input_action_button(
                    "btn_dismiss_live_view_warning",
                    _("Dismiss"),
                    class_="btn btn-sm btn-outline-secondary",
                ),
                class_="live-view-warning-dismiss",
            ),
            class_="live-view-warning-panel",
        )

    @output
    @render.ui
    def ui_live_view_footer():
        # Quick access controls for ALLOWED_TO_ENTER and ALLOWED_TO_EXIT
        return ui.div(
            ui.card(
                ui.row(
                    ui.column(
                        6,
                        ui.input_select(
                            id="quick_allowed_to_enter",
                            label=_("Inside direction:"),
                            choices={
                                AllowedToEnter.ALL.value: _(
                                    "All cats (unlock on every detected motion)"
                                ),
                                AllowedToEnter.ALL_RFIDS.value: _(
                                    "All cats with a RFID chip"
                                ),
                                AllowedToEnter.KNOWN.value: _("Only registered cats"),
                                AllowedToEnter.NONE.value: _("No cats"),
                                AllowedToEnter.CONFIGURE_PER_CAT.value: _(
                                    "Individual configuration per cat"
                                ),
                            },
                            selected=str(CONFIG["ALLOWED_TO_ENTER"].value),
                            width="100%",
                        ),
                    ),
                    ui.column(
                        6,
                        ui.input_select(
                            id="quick_allowed_to_exit",
                            label=_("Outside direction:"),
                            choices={
                                "allow": _("Allow exit"),
                                "deny": _("Do not allow exit"),
                                "configure_per_cat": _(
                                    "Individual configuration per cat"
                                ),
                            },
                            selected=str(CONFIG["ALLOWED_TO_EXIT"].value),
                            width="100%",
                        ),
                    ),
                ),
                ui.div(
                    ui.tooltip(
                        icon_svg(
                            "circle-info", margin_left="-0.1em", margin_right="auto"
                        ),
                        _(
                            "The individual configuration per cat can be set in the CATS section."
                        ),
                        id="tooltip_configure_per_cat_quick",
                        options={"trigger": "hover click"},
                    ),
                    style_="position: absolute; top: 6px; right: 10px;",
                ),
                class_="image-container",
                style_="margin-top: 0px; margin-bottom: 20px; padding-top: 8px; padding-bottom: 0px; position: relative;",
            ),
            ui.card(
                ui.input_action_button(
                    id="bManualOverride",
                    label=_("Unlock inside"),
                    icon=icon_svg("unlock"),
                    disabled=True,
                ),
                class_="image-container",
                style_="margin-top: 0px;",
            ),
            ui.card(
                ui.input_action_button(
                    id="bResetPreyCooldown",
                    label=_("Reset prey cooldown now"),
                    icon=icon_svg("clock-rotate-left"),
                    disabled=True,
                ),
                class_="image-container",
                style_="margin-top: 10px;",
            ),
        )

    pending_lock_enter = reactive.Value(None)
    pending_lock_exit = reactive.Value(None)

    def _apply_allowed_to_enter(value: str):
        CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter(value)
        update_single_config_parameter("ALLOWED_TO_ENTER")
        update_mqtt_config("ALLOWED_TO_ENTER")
        reload_trigger_config.set(reload_trigger_config.get() + 1)

    def _apply_allowed_to_exit(value: str):
        from src.baseconfig import AllowedToExit as ATE

        CONFIG["ALLOWED_TO_EXIT"] = ATE(value)
        update_single_config_parameter("ALLOWED_TO_EXIT")
        update_mqtt_config("ALLOWED_TO_EXIT")
        reload_trigger_config.set(reload_trigger_config.get() + 1)

    @reactive.Effect
    @reactive.event(input.quick_allowed_to_enter)
    def quick_update_allowed_to_enter():
        new = str(input.quick_allowed_to_enter() or "")
        old = str(CONFIG["ALLOWED_TO_ENTER"].value)
        if new == old:
            return
        if new == AllowedToEnter.NONE.value:
            pending_lock_enter.set(new)
            ui.update_select("quick_allowed_to_enter", selected=old)
            ui.modal_show(
                ui.modal(
                    _("This will block all cats from entering. Continue?"),
                    title=_("Change entry mode"),
                    easy_close=False,
                    footer=ui.div(
                        ui.input_action_button(
                            "btn_modal_confirm_lock_enter_ok", _("Continue")
                        ),
                        ui.input_action_button("btn_modal_cancel", _("Cancel")),
                    ),
                )
            )
            return
        _apply_allowed_to_enter(new)

    @reactive.Effect
    @reactive.event(input.quick_allowed_to_exit)
    def quick_update_allowed_to_exit():
        from src.baseconfig import AllowedToExit as ATE

        new = str(input.quick_allowed_to_exit() or "")
        old = str(CONFIG["ALLOWED_TO_EXIT"].value)
        if new == old:
            return
        if new == ATE.DENY.value:
            pending_lock_exit.set(new)
            ui.update_select("quick_allowed_to_exit", selected=old)
            ui.modal_show(
                ui.modal(
                    _("This will block all cats from exiting. Continue?"),
                    title=_("Change exit mode"),
                    easy_close=False,
                    footer=ui.div(
                        ui.input_action_button(
                            "btn_modal_confirm_lock_exit_ok", _("Continue")
                        ),
                        ui.input_action_button("btn_modal_cancel", _("Cancel")),
                    ),
                )
            )
            return
        _apply_allowed_to_exit(new)

    @reactive.Effect
    @reactive.event(input.btn_modal_confirm_lock_enter_ok)
    def on_confirm_lock_enter():
        value = pending_lock_enter.get()
        pending_lock_enter.set(None)
        ui.modal_remove()
        if not value:
            return
        _apply_allowed_to_enter(value)
        ui.update_select("quick_allowed_to_enter", selected=value)

    @reactive.Effect
    @reactive.event(input.btn_modal_confirm_lock_exit_ok)
    def on_confirm_lock_exit():
        value = pending_lock_exit.get()
        pending_lock_exit.set(None)
        ui.modal_remove()
        if not value:
            return
        _apply_allowed_to_exit(value)
        ui.update_select("quick_allowed_to_exit", selected=value)

    @reactive.Effect
    @reactive.event(input.btn_modal_cancel)
    def on_cancel_lock_mode():
        pending_lock_enter.set(None)
        pending_lock_exit.set(None)

    @reactive.Effect
    @reactive.event(input.bManualOverride)
    def on_action_let_kitty_in():
        magnets = getattr(Magnets, "instance", None)
        if magnets is None:
            logging.info(
                "[SERVER] Manual override ignored: magnets not initialized yet."
            )
            return

        inside_state = magnets.get_inside_state()
        if inside_state == False:
            logging.info(
                f"[SERVER] Manual override from Live View - letting Kitty in now"
            )
            manual_door_override["unlock_inside"] = True
        else:
            logging.info(f"[SERVER] Manual override from Live View - close inside now")
            manual_door_override["lock_inside"] = True

    @reactive.Effect
    @reactive.event(input.bResetPreyCooldown)
    def on_action_reset_prey_cooldown():
        logging.info(f"[SERVER] Resetting prey cooldown now")
        backend_main.prey_detection_tm = 0.0
        backend_main.prey_detection_mono = 0.0

        # Add a delayed-load flag for the Last Events table

    last_events_ready = reactive.Value(False)

    @reactive.Effect
    def init_last_events_delay():
        # Schedule one re-run ~1s later; set the flag only on that re-run
        if not hasattr(init_last_events_delay, "scheduled"):
            init_last_events_delay.scheduled = False

        if not last_events_ready.get():
            if not init_last_events_delay.scheduled:
                reactive.invalidate_later(0.5)
                init_last_events_delay.scheduled = True
                return
                # Second run (after 1s): mark ready
            last_events_ready.set(True)

    @output
    @render.ui
    def ui_last_events():
        return ui.layout_column_wrap(
            ui.div(
                ui.card(
                    ui.card_header(ui.h5(_("Last events"))),
                    ui.output_ui("ui_last_events_table"),
                    full_screen=False,
                    class_="generic-container",
                    style_="margin-bottom: 40px;",
                    min_height="150px",
                ),
                width="400px",
            )
        )

    @render.text
    @reactive.event(reload_trigger_photos, last_events_ready, ignore_none=True)
    def ui_last_events_table():
        # Show a loading spinner until the delayed flag is set
        if not last_events_ready.get():
            return ui.HTML(
                '<div class="spinner-container"><div class="spinner"></div></div>'
            )
        return get_events_table_html(block_count=25, panel_prefix="last")

    @output
    @render.ui
    def ui_events_by_date():
        return ui.card(
            ui.output_ui("ui_events_by_date_table"),
            full_screen=False,
            class_="generic-container kh-photos-grouped-card",
            min_height="150px",
        )

    @render.text
    @reactive.event(
        input.date_selector,
        input.button_cat_only,
        input.button_mouse_only,
        reload_trigger_photos,
        ignore_none=True,
    )
    def ui_events_by_date_table():
        date_start = DateTimeUtil.format_date_minmax(input.date_selector(), True)
        date_end = DateTimeUtil.format_date_minmax(input.date_selector(), False)
        timezone = ZoneInfo(CONFIG["TIMEZONE"])
        # Convert date_start and date_end to timezone-aware datetime strings in the UTC timezone
        date_start = (
            datetime.strptime(date_start, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone)
            .astimezone(ZoneInfo("UTC"))
            .strftime("%Y-%m-%d %H:%M:%S%z")
        )
        date_end = (
            datetime.strptime(date_end, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone)
            .astimezone(ZoneInfo("UTC"))
            .strftime("%Y-%m-%d %H:%M:%S%z")
        )

        # Use the refactored function that returns HTML
        return get_events_table_html(
            0,
            date_start,
            date_end,
            input.button_cat_only(),
            input.button_mouse_only(),
            CONFIG["MOUSE_THRESHOLD"],
            panel_prefix="photos",
        )

    def get_events_table_html(
        block_count=0,
        date_start="2020-01-01 00:00:00",
        date_end="2100-12-31 23:59:59",
        cats_only=False,
        mouse_only=False,
        mouse_probability=0.0,
        panel_prefix="last",
    ):
        try:
            logging.info(
                f"Reading events from the database for block_count={block_count}, date_start={date_start}, date_end={date_end}, cats_only={cats_only}, mouse_only={mouse_only}, mouse_probability={mouse_probability}"
            )
            df_events = EventsRepo.db_get_motion_blocks(
                CONFIG["KITTYHACK_DATABASE_PATH"],
                block_count,
                date_start,
                date_end,
                cats_only,
                mouse_only,
                mouse_probability,
            )

            if df_events.empty:
                return ui.HTML(
                    '<table class="dataframe shiny-table table w-auto">'
                    "<tbody><tr><td>" + _("No events found.") + "</td></tr></tbody>"
                    "</table>"
                )

                # Convert UTC timestamps to local timezone
            df_events["created_at"] = pd.to_datetime(
                df_events["created_at"]
            ).dt.tz_convert(CONFIG["TIMEZONE"])
            df_events = df_events.sort_values(by="created_at", ascending=False)
            df_events["date"] = df_events["created_at"].dt.date
            df_events["time"] = df_events["created_at"].dt.strftime("%H:%M:%S")

            # Replace dates with "Today" and "Yesterday"
            today = datetime.now(ZoneInfo(CONFIG["TIMEZONE"])).date()
            yesterday = today - timedelta(days=1)
            date_format = (
                CONFIG["DATE_FORMAT"]
                .lower()
                .replace("yyyy", "%Y")
                .replace("mm", "%m")
                .replace("dd", "%d")
            )
            df_events["date_display"] = df_events["date"].apply(
                lambda date: (
                    _("Today")
                    if date == today
                    else (
                        _("Yesterday")
                        if date == yesterday
                        else date.strftime(date_format)
                    )
                )
            )

            # Show the cat name instead of the RFID, and prepare thumbnails
            cat_name_dict = CatsRepo.get_cat_name_rfid_dict(
                CONFIG["KITTYHACK_DATABASE_PATH"]
            )
            # Build a dict: rfid -> (cat_id, name)
            df_cats = CatsRepo.db_get_cats(
                CONFIG["KITTYHACK_DATABASE_PATH"], ReturnDataCatDB.all
            )
            rfid_to_catid = {
                row["rfid"]: row["id"] for __, row in df_cats.iterrows() if row["rfid"]
            }
            cat_thumbnails = {}
            for rfid, cat_id in rfid_to_catid.items():
                thumb = CatsRepo.get_cat_thumbnail(
                    CONFIG["KITTYHACK_DATABASE_PATH"], cat_id
                )
                if thumb:
                    cat_thumbnails[rfid] = thumb

            def cat_name_with_icon(rfid):
                name = cat_name_dict.get(
                    rfid,
                    _("Unknown RFID") + f": {rfid}" if rfid else _("No RFID found"),
                )
                thumb = cat_thumbnails.get(rfid)
                if thumb:
                    return f'<img src="data:image/jpeg;base64,{thumb}" style="width:24px;height:24px;border-radius:50%;vertical-align:middle;margin-right:6px;"> {name}'
                else:
                    return name

            df_events["cat_name"] = df_events["rfid"].apply(cat_name_with_icon)

            block_ids = [int(b) for b in df_events["block_id"].tolist()]
            timelines_by_block = EventsRepo.db_get_motion_timelines(
                CONFIG["KITTYHACK_DATABASE_PATH"], block_ids
            )
            event_row_title = html_module.escape(_("Show event details"))

            # Process event types into HTML with icons
            event_icons = {}
            for __, row in df_events.iterrows():
                event_type = row["event_type"]

                # Handle comma-separated event types
                if "," in event_type:
                    event_type_list = event_type.split(",")
                    # Process each event type and combine the results
                    icons_list = []
                    tooltip_parts = []
                    for et in event_type_list:
                        icons_list.extend(EventType.to_icons(et.strip()))
                        tooltip_parts.append(EventType.to_pretty_string(et.strip()))

                    icons_html = " ".join(str(icon) for icon in icons_list)
                    tooltip_text = " + ".join(tooltip_parts)
                else:
                    # Process single event type as before
                    icons_html = " ".join(
                        str(icon) for icon in EventType.to_icons(event_type)
                    )
                    tooltip_text = EventType.to_pretty_string(event_type)

                event_icons[row.name] = {
                    "icons_html": icons_html,
                    "tooltip_text": tooltip_text,
                }

                # Start building the HTML table
            html = '<table class="dataframe shiny-table table w-100">'
            html += "<tbody>"

            # Iterate through the events and add date rows when the date changes
            last_date = None
            for idx, row in df_events.iterrows():
                if row["date_display"] != last_date:
                    html += f'<tr class="date-separator-row"><td colspan="4" class="event-date-separator">{row["date_display"]}</td></tr>'
                    last_date = row["date_display"]

                event_info = event_icons[idx]
                block_id = int(row["block_id"])
                panel_id = f"event-timeline-{panel_prefix}-{block_id}"
                timeline_entries = timelines_by_block.get(block_id)
                if not timeline_entries:
                    timeline_entries = timeline_fallback_from_event_type(
                        row["event_type"], CONFIG["TIMEZONE"], row["created_at"]
                    )
                else:
                    timeline_entries = timeline_extract_latest_event(timeline_entries)
                timeline_html = timeline_entries_to_html(
                    timeline_entries, CONFIG["TIMEZONE"]
                )
                html += (
                    f'<tr class="event-data-row kh-event-row-toggle" data-kh-panel-id="{panel_id}" '
                    f'role="button" tabindex="0" aria-expanded="false" aria-controls="{panel_id}" '
                    f'title="{event_row_title}">'
                )
                html += f"<td>{row['time']}</td>"
                html += (
                    f'<td><div class="event-icons-cell">'
                    f'<div class="event-icons">{event_info["icons_html"]}</div>'
                    f"</div></td>"
                )
                html += f"<td>{row['cat_name']}</td>"
                unique_id = hashlib.md5(os.urandom(16)).hexdigest()
                btn_id = f"btn_show_event_{unique_id}"
                html += f'<td class="event-action-cell"><div>{btn_show_event(btn_id)}</div></td>'
                show_event_server(btn_id, row["block_id"])
                html += "</tr>"
                html += (
                    f'<tr class="event-timeline-row">'
                    f'<td colspan="4" class="event-timeline-row-cell">'
                    f'<div class="event-timeline-panel collapse" id="{panel_id}">{timeline_html}</div>'
                    f"</td></tr>"
                )

            html += "</tbody></table>"
            return ui.HTML(html)
        except Exception as e:
            logging.error(f"Failed to read events from the database: {e}")
            return ui.HTML(
                '<table class="dataframe shiny-table table w-auto">'
                '<tbody><tr><td class="error">'
                + _("Failed to read events from the database.")
                + "</td></tr></tbody>"
                "</table>"
            )
