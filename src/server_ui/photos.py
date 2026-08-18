"""Pictures tab handlers."""

import os
import html
import pandas as pd
from datetime import datetime, timedelta
from shiny import render, ui, reactive
import logging
from zoneinfo import ZoneInfo
from faicons import icon_svg
import asyncio
from src.baseconfig import CONFIG, set_language, update_single_config_parameter
from src.helper import DateTimeUtil
from src.system import LabelStudioInstall
from src.database import (
    CatsRepo,
    DatabaseCore,
    EventsRepo,
    ReturnDataPhotosDB,
)
from src.paths import pictures_original_dir
from src.mode import is_remote_mode
from src.labelstudio_api import upload_image_to_labelstudio_project
from src.server_ui.state import reload_trigger_photos
from src.server_ui.context import SessionContext
from src.server_ui.event_modal import btn_show_event, show_event_server

_ = set_language(CONFIG["LANGUAGE"])

if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir


def register_photos(input, output, session, ctx: SessionContext):
    """Register Pictures tab UI and handlers."""

    def _photos_local_today():
        return datetime.now(DateTimeUtil.get_timezone()).date()

    @output
    @render.ui
    def ui_photos_date():
        """Date picker and compact filter chips for the Pictures tab."""
        uiDateBar = ui.div(
            ui.div(
                ui.input_action_button(
                    "button_decrement",
                    "",
                    icon=icon_svg("angle-left", margin_right="auto"),
                    class_="btn-date-control",
                ),
                ui.input_date(
                    "date_selector",
                    "",
                    value=_photos_local_today(),
                    format=CONFIG["DATE_FORMAT"],
                ),
                ui.input_action_button(
                    "button_increment",
                    "",
                    icon=icon_svg("angle-right", margin_right="auto"),
                    class_="btn-date-control",
                ),
                ui.input_action_button(
                    "button_today",
                    _("Today"),
                    class_="kh-photo-today-btn",
                ),
                class_="kh-photo-filter-date",
            ),
            ui.div(
                ui.div(
                    ui.span(_("Filter by…"), class_="kh-photo-filter-group-label"),
                    ui.div(
                        ui.tooltip(
                            ui.div(
                                ui.input_switch(
                                    "button_cat_only",
                                    _("Cats"),
                                    CONFIG["SHOW_CATS_ONLY"],
                                ),
                                class_="kh-photo-filter-chip",
                            ),
                            _("Show only pictures with a detected cat"),
                            options={"trigger": "hover"},
                            placement="bottom",
                        ),
                        ui.tooltip(
                            ui.div(
                                ui.input_switch(
                                    "button_mouse_only",
                                    _("Prey"),
                                    CONFIG["SHOW_MICE_ONLY"],
                                ),
                                class_="kh-photo-filter-chip",
                            ),
                            _("Show only pictures with detected prey"),
                            options={"trigger": "hover"},
                            placement="bottom",
                        ),
                        class_="kh-photo-filter-group-chips",
                    ),
                    class_="kh-photo-filter-group",
                ),
                ui.div(
                    ui.tooltip(
                        ui.div(
                            ui.input_switch(
                                "button_detection_overlay",
                                _("Overlay"),
                                CONFIG["SHOW_IMAGES_WITH_OVERLAY"],
                            ),
                            class_="kh-photo-filter-chip",
                        ),
                        _("Show detection overlay"),
                        options={"trigger": "hover"},
                        placement="bottom",
                    ),
                    ui.tooltip(
                        ui.div(
                            ui.input_switch(
                                "button_events_view",
                                _("Group"),
                                CONFIG["GROUP_PICTURES_TO_EVENTS"],
                            ),
                            class_="kh-photo-filter-chip",
                        ),
                        _(
                            "Group pictures into events, or show them one by one"
                        ),
                        options={"trigger": "hover"},
                        placement="bottom",
                    ),
                    class_="kh-photo-filter-view",
                ),
                class_="kh-photo-filter-chips",
            ),
            ui.input_action_button("photos_load_more", "", class_="d-none"),
            class_="kh-photo-filter-bar",
        )
        return ui.div(
            ui.tags.div(
                {
                    "id": "kh_photo_delete_i18n",
                    "style": "display:none;",
                    "data-msg": _("Delete this picture?"),
                }
            ),
            uiDateBar,
            class_="kh-photos-filter-dock",
        )

    @reactive.Effect
    @reactive.event(input.button_cat_only)
    def update_config_show_cats_only():
        CONFIG["SHOW_CATS_ONLY"] = input.button_cat_only()
        update_single_config_parameter("SHOW_CATS_ONLY")

    @reactive.Effect
    @reactive.event(input.button_mouse_only)
    def update_config_show_mice_only():
        CONFIG["SHOW_MICE_ONLY"] = input.button_mouse_only()
        update_single_config_parameter("SHOW_MICE_ONLY")

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
            session.send_input_message(
                "date_selector", {"value": new_date.strftime("%Y-%m-%d")}
            )

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
            session.send_input_message(
                "date_selector", {"value": new_date.strftime("%Y-%m-%d")}
            )

    def _photos_filters_to_utc_range() -> tuple[str, str]:
        date_start = DateTimeUtil.format_date_minmax(input.date_selector(), True)
        date_end = DateTimeUtil.format_date_minmax(input.date_selector(), False)
        timezone = ZoneInfo(CONFIG["TIMEZONE"])
        date_start_utc = (
            datetime.strptime(date_start, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone)
            .astimezone(ZoneInfo("UTC"))
            .strftime("%Y-%m-%d %H:%M:%S%z")
        )
        date_end_utc = (
            datetime.strptime(date_end, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone)
            .astimezone(ZoneInfo("UTC"))
            .strftime("%Y-%m-%d %H:%M:%S%z")
        )
        return date_start_utc, date_end_utc

    PHOTOS_BATCH = 24
    _photos_shown = [0]
    _photos_total = [0]
    _photos_last_block = [None]
    _photos_filter_key = [None]
    _photos_loading = [False]

    def _photos_generation() -> str:
        key = _photos_filter_key[0]
        if not key:
            return ""
        return "|".join(str(part) for part in key)

    def _photos_count() -> int:
        date_start_utc, date_end_utc = _photos_filters_to_utc_range()
        return EventsRepo.db_count_photos(
            CONFIG["KITTYHACK_DATABASE_PATH"],
            date_start_utc,
            date_end_utc,
            input.button_cat_only(),
            input.button_mouse_only(),
            CONFIG["MOUSE_THRESHOLD"],
        )

    def _photos_fetch(offset: int, limit: int, return_data=ReturnDataPhotosDB.all_except_photos):
        date_start_utc, date_end_utc = _photos_filters_to_utc_range()
        return EventsRepo.db_get_photos(
            CONFIG["KITTYHACK_DATABASE_PATH"],
            return_data,
            date_start_utc,
            date_end_utc,
            input.button_cat_only(),
            input.button_mouse_only(),
            CONFIG["MOUSE_THRESHOLD"],
            0,
            max(1, int(limit)),
            True,
            max(0, int(offset)),
        )

    def _photos_footer_ui():
        shown = int(_photos_shown[0] or 0)
        total = int(_photos_total[0] or 0)
        has_more = shown < total
        return ui.div(
            ui.span(
                f"{shown} / {total} " + _("pictures"),
                id="photos_infinite_status",
                class_="kh-photos-status",
            ),
            ui.div(
                ui.div(class_="spinner-border spinner-border-sm", role="status"),
                id="photos_infinite_sentinel",
                class_="kh-photos-sentinel" + ("" if has_more else " d-none"),
                **{"data-has-more": "1" if has_more else "0"},
            ),
            id="photos_infinite_wrap",
            class_="kh-photos-infinite",
            **{
                "data-generation": _photos_generation(),
                "data-shown": str(shown),
                "data-total": str(total),
            },
        )

    @reactive.Effect
    @reactive.event(input.button_today, ignore_none=True)
    def reset_ui_photos_date():
        today = _photos_local_today()
        session.send_input_message(
            "date_selector", {"value": today.strftime("%Y-%m-%d")}
        )

    _photos_seen_today = [_photos_local_today()]
    _photo_action_registered_ids: set = set()
    _event_modal_registered_ids: set = set()

    @reactive.effect
    def advance_photos_date_after_midnight():
        """Keep a 'today' selection on the current local day after midnight."""
        reactive.invalidate_later(30)
        today = _photos_local_today()
        prev = _photos_seen_today[0]
        if today == prev:
            return
        _photos_seen_today[0] = today
        try:
            selected = input.date_selector()
        except Exception:
            return
        if not selected:
            return
        try:
            selected_date = pd.to_datetime(selected).date()
        except Exception:
            return
        if selected_date == prev:
            session.send_input_message(
                "date_selector", {"value": today.strftime("%Y-%m-%d")}
            )

    @output
    @render.ui
    @reactive.event(input.button_events_view, ignore_none=True)
    def ui_photos_events():
        if input.button_events_view():
            return ui.div(
                ui.output_ui("ui_events_by_date"),
                class_="kh-photos-stage kh-photos-stage--grouped",
            )
        else:
            return ui.output_ui("ui_photos_cards")

    def _photos_items_from_df(df_photos, cat_name_dict, show_overlay, extra_class=""):
        ui_items = []
        last_block_id = _photos_last_block[0]
        for _row_i, row in df_photos.iterrows():
            try:
                block_id = int(row["block_id"])
            except Exception:
                block_id = None
            if block_id is not None and block_id != last_block_id:
                last_block_id = block_id
                btn_id = f"photo_event_{block_id}"
                if block_id not in _event_modal_registered_ids:
                    show_event_server(btn_id, block_id)
                    _event_modal_registered_ids.add(block_id)
                ui_items.append(_build_event_header(block_id, row))
            ui_items.append(
                _build_photo_card(row, cat_name_dict, show_overlay, extra_class)
            )
        _photos_last_block[0] = last_block_id
        return ui_items

    @output
    @render.ui
    @reactive.event(
        input.button_events_view,
        input.button_detection_overlay,
        input.date_selector,
        input.button_cat_only,
        input.button_mouse_only,
        reload_trigger_photos,
        ignore_none=True,
    )
    def ui_photos_cards():
        if input.button_events_view():
            return ui.div()

        filter_key = (
            str(input.date_selector()),
            bool(input.button_cat_only()),
            bool(input.button_mouse_only()),
            int(reload_trigger_photos.get() or 0),
        )
        if filter_key != _photos_filter_key[0]:
            _photos_filter_key[0] = filter_key
            _photos_shown[0] = 0
            _photos_last_block[0] = None

        total_count = _photos_count()
        _photos_total[0] = total_count
        limit = PHOTOS_BATCH if _photos_shown[0] == 0 else max(PHOTOS_BATCH, _photos_shown[0])
        df_photos = _photos_fetch(0, limit) if total_count else pd.DataFrame()

        if df_photos.empty:
            _photos_shown[0] = 0
            logging.info("No pictures for the selected filter criteria found.")
            return ui.div(
                ui.div(
                    ui.div(
                        ui.HTML(
                            str(
                                icon_svg(
                                    "shield-cat",
                                    height="1.5em",
                                    width="1.5em",
                                    margin_left="0",
                                    margin_right="0",
                                )
                            )
                        ),
                        class_="kh-empty-state-icon",
                    ),
                    ui.h5(_("No pictures"), class_="kh-empty-state-title"),
                    ui.p(_("No pictures for the selected filter criteria found.")),
                    class_="kh-empty-state",
                ),
                id="photos_cards_root",
                class_="kh-photos-stage kh-photos-stage--grid",
                **{
                    "data-generation": _photos_generation(),
                    "data-shown": "0",
                    "data-total": str(total_count),
                },
            )

        cat_name_dict = CatsRepo.get_cat_name_rfid_dict(
            CONFIG["KITTYHACK_DATABASE_PATH"]
        )
        show_overlay = bool(input.button_detection_overlay())
        _photos_last_block[0] = None
        ui_items = _photos_items_from_df(df_photos, cat_name_dict, show_overlay)
        _photos_shown[0] = len(df_photos)

        return ui.div(
            ui.tags.div(
                *ui_items,
                class_="kh-photo-grid",
                id="photos_grid",
            ),
            _photos_footer_ui(),
            id="photos_cards_root",
            class_="kh-photos-stage kh-photos-stage--grid",
            **{
                "data-generation": _photos_generation(),
                "data-shown": str(_photos_shown[0]),
                "data-total": str(_photos_total[0]),
            },
        )

    @reactive.Effect
    @reactive.event(input.photos_load_more, ignore_none=True)
    def load_more_photos():
        if input.button_events_view():
            return
        if _photos_loading[0]:
            return
        _photos_loading[0] = True
        try:
            total_count = _photos_count()
            _photos_total[0] = total_count
            offset = int(_photos_shown[0] or 0)
            if offset >= total_count:
                ui.remove_ui("#photos_infinite_wrap")
                ui.insert_ui(_photos_footer_ui(), "#photos_cards_root", where="beforeEnd")
                return
            df_photos = _photos_fetch(offset, PHOTOS_BATCH)
            if df_photos.empty:
                ui.remove_ui("#photos_infinite_wrap")
                ui.insert_ui(_photos_footer_ui(), "#photos_cards_root", where="beforeEnd")
                return
            cat_name_dict = CatsRepo.get_cat_name_rfid_dict(
                CONFIG["KITTYHACK_DATABASE_PATH"]
            )
            show_overlay = False
            try:
                show_overlay = bool(input.button_detection_overlay())
            except Exception:
                pass
            ui_items = _photos_items_from_df(
                df_photos, cat_name_dict, show_overlay, extra_class="kh-fadein"
            )
            ui.insert_ui(
                ui.TagList(*ui_items), "#photos_grid", where="beforeEnd"
            )
            for pid in df_photos["id"].tolist():
                pid_int = int(pid)
                if pid_int not in _photo_action_registered_ids:
                    _photo_action_registered_ids.add(pid_int)
                    _register_single_photo_delete(pid_int)
                    _register_single_photo_send_ls(pid_int)
            _photos_shown[0] = offset + len(df_photos)
            ui.remove_ui("#photos_infinite_wrap")
            ui.insert_ui(_photos_footer_ui(), "#photos_cards_root", where="beforeEnd")
        except Exception:
            logging.exception("Failed to load more pictures")
        finally:
            _photos_loading[0] = False

    def _build_photo_card(
        data_row, cat_name_dict, show_overlay: bool, extra_class: str = ""
    ):
        """Build a single photo card UI element."""
        mouse_probability = data_row["mouse_probability"]

        event_text = data_row["event_text"]
        if event_text:
            detected_objects = EventsRepo.read_event_from_json(event_text)
        else:
            detected_objects = []

        try:
            photo_timestamp = pd.to_datetime(
                DateTimeUtil.get_local_date_from_utc_date(data_row["created_at"])
            ).strftime("%H:%M:%S")
        except ValueError:
            photo_timestamp = "Unknown date"

        if data_row["rfid"]:
            cat_name = cat_name_dict.get(
                data_row["rfid"], _("Unknown RFID: {}".format(data_row["rfid"]))
            )
        else:
            cat_name = _("No RFID found")

        pid = int(data_row["id"])
        try:
            block_id = int(data_row["block_id"])
        except Exception:
            block_id = 0
        event_label = _("Event {}").format(block_id)
        event_chip = f"#{block_id}"
        thumb_src = f"/thumb/{pid}.jpg"
        orig_src = f"/orig/{pid}.jpg"
        is_prey = mouse_probability >= CONFIG["MOUSE_THRESHOLD"]
        prey_class = "kh-photo-prey-badge" + (" is-prey" if is_prey else "")
        prey_title = html.escape(_("Prey probability"))
        cat_icon = str(icon_svg("cat", margin_left="0", margin_right="0.3rem"))

        img_html = f'''<div class="kh-photo-thumb" data-photo-id="{pid}" data-orig-src="{orig_src}">
                <img src="{thumb_src}" loading="lazy" decoding="async" alt="" />'''

        if show_overlay and detected_objects:
            mouse_threshold = float(CONFIG["MOUSE_THRESHOLD"])
            for detected_object in detected_objects:
                obj_name = str(detected_object.object_name or "").strip()
                name_l = obj_name.lower()
                if name_l == "false-accept":
                    continue
                is_prey_obj = name_l in ("prey", "beute")
                box_class = "kh-detect-box"
                if is_prey_obj:
                    if float(detected_object.probability or 0) >= mouse_threshold:
                        box_class += " is-prey"
                    else:
                        box_class += " is-prey-soft"
                label_pos = "bottom: -26px" if detected_object.y < 16 else "top: -26px"
                img_html += f'''
                <div class="{box_class}" style="left:{detected_object.x}%; top:{detected_object.y}%; width:{detected_object.width}%; height:{detected_object.height}%;">
                    <div class="kh-detect-label" style="{label_pos};">
                        {html.escape(obj_name)} ({detected_object.probability:.0f}%)
                    </div>
                </div>'''

        img_html += f'''
            <div class="kh-photo-overlay">
                <div class="kh-photo-meta-top">
                    <span class="kh-photo-meta-left">
                        <span class="kh-photo-time">{html.escape(photo_timestamp)}</span>
                        <span class="kh-photo-event-chip" title="{html.escape(event_label)}">{html.escape(event_chip)}</span>
                    </span>
                    <span class="{prey_class}" data-bs-toggle="tooltip" data-bs-placement="top" data-bs-title="{prey_title}" title="{prey_title}">{mouse_probability:.0f}%</span>
                </div>
                <div class="kh-photo-meta-bottom">
                    <span class="kh-photo-cat">{cat_icon}{html.escape(str(cat_name))}</span>
                </div>
            </div>
        </div>'''

        ls_disabled = (
            not CONFIG.get("LABELSTUDIO_API_TOKEN")
            or not CONFIG.get("LABELSTUDIO_PROJECT")
            or not LabelStudioInstall.get_labelstudio_status()
        )

        card_class = "kh-photo-card" + (" kh-photo-card-prey" if is_prey else "")
        if extra_class:
            card_class += f" {extra_class}"

        return ui.card(
            ui.div(
                ui.HTML(img_html),
                ui.div(
                    ui.tooltip(
                        ui.tags.a(
                            ui.HTML(
                                str(
                                    icon_svg(
                                        "image", margin_left="0", margin_right="0"
                                    )
                                )
                            ),
                            href=f"/orig/{pid}.jpg",
                            download=f"kittyhack_photo_{pid}.jpg",
                            class_="btn btn-icon-square kh-photo-action-btn",
                        ),
                        _("Download picture"),
                        options={"trigger": "hover"},
                    ),
                    ui.tooltip(
                        ui.input_action_button(
                            id=f"photo_send_ls_{pid}",
                            label="",
                            icon=icon_svg(
                                "upload", margin_left="0", margin_right="0"
                            ),
                            class_="btn-icon-square kh-photo-action-btn",
                            disabled_=ls_disabled,
                        ),
                        _("Send picture to Label Studio"),
                        options={"trigger": "hover"},
                    ),
                    ui.tooltip(
                        ui.input_action_button(
                            id=f"photo_delete_{pid}",
                            label="",
                            icon=icon_svg(
                                "trash-can", margin_left="0", margin_right="0"
                            ),
                            class_="btn-icon-square kh-photo-action-btn kh-photo-action-danger",
                        ),
                        _("Delete picture"),
                        options={"trigger": "hover"},
                    ),
                    class_="kh-photo-actions",
                ),
                class_="kh-photo-tile",
            ),
            id=f"photo_card_{pid}",
            class_=card_class,
        )

    def _build_event_header(block_id: int, data_row):
        """Full-width gallery header for a motion-block group."""
        try:
            event_time = pd.to_datetime(
                DateTimeUtil.get_local_date_from_utc_date(data_row["created_at"])
            ).strftime("%H:%M:%S")
        except Exception:
            event_time = ""
        btn_id = f"photo_event_{int(block_id)}"
        head_main = [
            ui.span(_("Event {}").format(int(block_id)), class_="kh-photo-event-id")
        ]
        if event_time:
            head_main.append(ui.span(event_time, class_="kh-photo-event-time"))
        return ui.div(
            ui.div(*head_main, class_="kh-photo-event-head-main"),
            ui.tooltip(
                btn_show_event(btn_id),
                _("Show event details"),
                options={"trigger": "hover"},
            ),
            class_="kh-photo-event-head",
        )

    @reactive.effect
    def _register_photo_actions():
        """Dynamically register per-photo action handlers for current page."""
        try:
            reload_trigger_photos.get()
            if input.button_events_view():
                return
        except Exception:
            pass

        try:
            shown = max(PHOTOS_BATCH, int(_photos_shown[0] or PHOTOS_BATCH))
            df_photos = _photos_fetch(0, shown, ReturnDataPhotosDB.only_ids)
        except Exception:
            df_photos = pd.DataFrame()

        current_ids = (
            set(df_photos["id"].tolist())
            if not df_photos.empty and "id" in df_photos.columns
            else set()
        )
        new_ids = current_ids - _photo_action_registered_ids

        for pid in new_ids:
            _photo_action_registered_ids.add(pid)
            _register_single_photo_delete(int(pid))
            _register_single_photo_send_ls(int(pid))

    def _register_single_photo_delete(pid: int):
        @reactive.effect
        @reactive.event(input[f"photo_delete_{pid}"])
        def _handler():
            result = EventsRepo.delete_photo_by_id(
                CONFIG["KITTYHACK_DATABASE_PATH"], pid
            )
            if result.success:
                ui.notification_show(
                    _("Photo {} deleted successfully.").format(pid),
                    duration=5,
                    type="message",
                )
                # Card is removed client-side instantly via JS.
                try:
                    _photos_shown[0] = max(0, int(_photos_shown[0] or 1) - 1)
                    _photos_total[0] = max(0, int(_photos_total[0] or 1) - 1)
                    if _photos_total[0] == 0:
                        reload_trigger_photos.set(reload_trigger_photos.get() + 1)
                        return
                    ui.remove_ui("#photos_infinite_wrap")
                    ui.insert_ui(
                        _photos_footer_ui(), "#photos_cards_root", where="beforeEnd"
                    )
                except Exception:
                    pass
            else:
                ui.notification_show(
                    _("An error occurred while deleting the photo: {}").format(
                        result.message
                    ),
                    duration=10,
                    type="error",
                )

    def _register_single_photo_send_ls(pid: int):
        @reactive.effect
        @reactive.event(input[f"photo_send_ls_{pid}"])
        async def _handler():
            project_id = CONFIG.get("LABELSTUDIO_PROJECT", "").strip()
            api_token = CONFIG.get("LABELSTUDIO_API_TOKEN", "").strip()

            if not project_id or not api_token:
                ui.notification_show(
                    _(
                        "Label Studio is not configured. Please set an API token and select a project in the settings."
                    ),
                    type="warning",
                    duration=5,
                )
                return

            if not LabelStudioInstall.get_labelstudio_status():
                ui.notification_show(
                    _("Label Studio is not running."), type="warning", duration=5
                )
                return

            img_bytes = None
            try:
                fp = os.path.join(pictures_original_dir(), f"{pid}.jpg")
                if os.path.exists(fp):
                    with open(fp, "rb") as f:
                        img_bytes = f.read()
            except Exception:
                img_bytes = None

            if img_bytes is None:
                try:
                    df = DatabaseCore.read_df_from_database(
                        CONFIG["KITTYHACK_DATABASE_PATH"],
                        f"SELECT original_image FROM events WHERE id = {int(pid)}",
                    )
                    if not df.empty:
                        ob = df.iloc[0].get("original_image")
                        if isinstance(ob, (bytes, bytearray)) and len(ob) > 0:
                            img_bytes = bytes(ob)
                except Exception:
                    pass

            if not isinstance(img_bytes, (bytes, bytearray)) or len(img_bytes) == 0:
                ui.notification_show(
                    _("Could not load the image."), type="warning", duration=5
                )
                return

            ui.notification_show(
                ui.HTML(
                    '<div class="d-flex align-items-center gap-2">'
                    '<div class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></div>'
                    f"<span>{_('Sending picture to Label Studio...')}</span>"
                    "</div>"
                ),
                id="ls_upload_progress",
                type="message",
                duration=None,
            )

            filename = f"kittyhack_photo_{pid}.jpg"
            success = await asyncio.to_thread(
                upload_image_to_labelstudio_project,
                project_id=int(project_id),
                image_bytes=img_bytes,
                filename=filename,
                token=api_token,
            )

            ui.notification_remove("ls_upload_progress")

            if success:
                ui.notification_show(
                    _("Picture sent to Label Studio successfully."),
                    type="message",
                    duration=3,
                )
            else:
                ui.notification_show(
                    _("Failed to send picture to Label Studio."),
                    type="error",
                    duration=5,
                )
