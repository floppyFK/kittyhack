"""Manage cats / add new cat tabs."""

from shiny import render, ui, reactive
from shiny.types import FileInfo
import base64
from faicons import icon_svg
import re
from src.baseconfig import CONFIG, set_language
from src.database import (
    CatsRepo,
    ReturnDataCatDB,
)
from src.mode import is_remote_mode
from src.shiny_wrappers import uix
from src.server_ui.state import reload_trigger_config
from src.server_ui.context import SessionContext

_ = set_language(CONFIG["LANGUAGE"])

if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir


def register_cats(input, output, session, ctx: SessionContext):
    """Register Manage Cats / Add Cat tab handlers."""

    @output
    @render.ui
    @reactive.event(ctx.reload_trigger_cats, reload_trigger_config, ignore_none=True)
    def ui_manage_cats():
        ui_cards = []
        df_cats = CatsRepo.db_get_cats(
            CONFIG["KITTYHACK_DATABASE_PATH"], ReturnDataCatDB.all
        )
        if not df_cats.empty:
            for __, data_row in df_cats.iterrows():
                # --- Picture ---
                if data_row["cat_image"]:
                    try:
                        decoded_picture = base64.b64encode(
                            data_row["cat_image"]
                        ).decode("utf-8")
                    except:
                        decoded_picture = None
                else:
                    decoded_picture = None
                img_html = (
                    f'<div style="text-align: center;"><img style="max-width: 400px !important;" '
                    f'src="data:image/jpeg;base64,{decoded_picture}" /></div>'
                    if decoded_picture
                    else '<div class="placeholder-image"><strong>'
                    + _("No picture found!")
                    + "</strong></div>"
                )

                entry_mode_per_cat = (
                    CONFIG["ALLOWED_TO_ENTER"].value == "configure_per_cat"
                )
                exit_mode_per_cat = (
                    CONFIG["ALLOWED_TO_EXIT"].value == "configure_per_cat"
                )
                entry_style = (
                    "padding-bottom: 20px;"
                    if entry_mode_per_cat
                    else "pointer-events: none; opacity: 0.6;"
                )
                exit_style = (
                    "padding-bottom: 20px;"
                    if exit_mode_per_cat
                    else "pointer-events: none; opacity: 0.6;"
                )
                prey_style = (
                    "padding-bottom: 20px;"
                    if CONFIG["MOUSE_CHECK_ENABLED"]
                    else "pointer-events: none; opacity: 0.6;"
                )

                settings_rows = []

                # Cat specific settings header
                settings_rows.append(
                    [
                        ui.row(
                            ui.column(
                                12,
                                ui.div(
                                    ui.markdown(_("##### Cat-specific settings")),
                                    style_="text-align: center;",
                                ),
                            )
                        ),
                        ui.br(),
                    ]
                )

                # Prey detection
                settings_rows.append(
                    ui.row(
                        ui.column(
                            12,
                            ui.div(
                                ui.input_switch(
                                    id=f"mng_cat_prey_{data_row['id']}",
                                    label=_("Enable Prey detection"),
                                    value=bool(
                                        int(data_row.get("enable_prey_detection", 1))
                                    ),
                                ),
                                style_=prey_style,
                            ),
                        )
                    )
                )
                if not CONFIG["MOUSE_CHECK_ENABLED"]:
                    settings_rows.append(
                        ui.row(
                            ui.column(
                                12,
                                ui.markdown(
                                    _(
                                        "**Disabled:** Global prey detection is turned off in the `CONFIGURATION` section. Enable `Detect prey` to use per-cat settings."
                                    )
                                ),
                                style_="color: grey;",
                            ),
                            style_="padding-bottom: 20px;",
                        )
                    )

                    # Allow entry switch
                settings_rows.append(
                    ui.row(
                        ui.column(
                            12,
                            ui.div(
                                ui.input_switch(
                                    id=f"mng_cat_allow_entry_{data_row['id']}",
                                    label=_("Allow entry"),
                                    value=bool(int(data_row.get("allow_entry", 1))),
                                ),
                                style_=entry_style,
                            ),
                        )
                    )
                )
                if not entry_mode_per_cat:
                    settings_rows.append(
                        ui.row(
                            ui.column(
                                12,
                                ui.markdown(
                                    _(
                                        "**Disabled:** This feature is only available in `Individual configuration per cat` mode for entry."
                                    )
                                ),
                                style_="color: grey;",
                            ),
                            style_="padding-bottom: 20px;",
                        )
                    )

                    # Allow exit switch
                settings_rows.append(
                    ui.row(
                        ui.column(
                            12,
                            ui.div(
                                ui.input_switch(
                                    id=f"mng_cat_allow_exit_{data_row['id']}",
                                    label=_("Allow exit"),
                                    value=bool(int(data_row.get("allow_exit", 1))),
                                ),
                                style_=exit_style,
                            ),
                        )
                    )
                )
                # Show warning if per-cat exit mode is active but no RFID assigned
                if exit_mode_per_cat and not data_row.get("rfid"):
                    settings_rows.append(
                        ui.row(
                            ui.column(
                                12,
                                ui.markdown(
                                    f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                                    + _(
                                        "This cat has no RFID configured. The individual exit per cat works only for cats with a RFID chip!"
                                    )
                                ),
                                style_="color:#b94a48;",
                            ),
                            style_="padding-bottom: 20px;",
                        )
                    )
                if not exit_mode_per_cat:
                    settings_rows.append(
                        ui.row(
                            ui.column(
                                12,
                                ui.markdown(
                                    _(
                                        "**Disabled:** This feature is only available in `Individual configuration per cat` mode for exit."
                                    )
                                ),
                                style_="color: grey;",
                            ),
                            style_="padding-bottom: 20px;",
                        )
                    )

                settings_section = ui.div(
                    ui.div(
                        *settings_rows,
                        class_="cat-settings-container",
                    ),
                    class_="align-left",
                )

                # --- Assemble card ---
                ui_cards.append(
                    ui.card(
                        ui.card_header(
                            ui.div(
                                ui.column(
                                    12,
                                    ui.input_text(
                                        id=f"mng_cat_name_{data_row['id']}",
                                        label=_("Name"),
                                        value=data_row["name"],
                                        width="100%",
                                    ),
                                ),
                                ui.br(),
                                ui.column(
                                    12,
                                    ui.input_text(
                                        id=f"mng_cat_rfid_{data_row['id']}",
                                        label=_("RFID"),
                                        value=data_row["rfid"],
                                        width="100%",
                                    ),
                                ),
                                ui.column(
                                    12,
                                    ui.div(
                                        id=f"mng_cat_rfid_status_{data_row['id']}",
                                        class_="rfid-status rfid-empty",
                                    ),
                                ),
                                ui.column(
                                    12,
                                    ui.help_text(
                                        _(
                                            "NOTE: This is NOT the number which stands in the booklet of your vet! You must use the the ID, which is read by the Kittyflap. It is 16 characters long and consists of numbers (0-9) and letters (A-F)."
                                        )
                                    ),
                                ),
                                ui.column(
                                    12,
                                    ui.help_text(
                                        _(
                                            "If you have entered the RFID correctly here, the name of the cat will be displayed in the [PICTURES] section."
                                        )
                                    ),
                                ),
                                ui.br(),
                                settings_section,
                                ui.br(),
                                ui.column(
                                    12,
                                    uix.input_file(
                                        id=f"mng_cat_pic_{data_row['id']}",
                                        label=_("Change Picture"),
                                        accept=[".jpg", ".png"],
                                        width="100%",
                                    ),
                                ),
                            )
                        ),
                        ui.HTML(img_html),
                        ui.card_footer(
                            ui.div(
                                ui.input_action_button(
                                    id=f"mng_cat_del_btn_{data_row['id']}",
                                    label=_("Delete {}").format(data_row["name"]),
                                    icon=icon_svg("trash"),
                                    class_="btn-outline-danger",
                                ),
                                style_="padding-top: 20px; display: flex; justify-content: center;",
                            )
                        ),
                        full_screen=False,
                        class_="image-container",
                    )
                )
            return ui.div(
                ui.tags.div(
                    {
                        "id": "kh_manage_cats_i18n",
                        "style": "display:none;",
                        "data-msg-empty": _(
                            "No RFID entered. Cat identification only via camera (if enabled). See CONFIGURATION section for details."
                        ),
                        "data-msg-valid": _("Valid RFID"),
                        "data-msg-invalid": _(
                            "Invalid RFID. Must be exactly 16 hex characters (0-9, A-F)."
                        ),
                    }
                ),
                ui.output_ui("ui_add_new_cat"),
                ui.br(),
                ui.div(
                    *ui_cards,
                    id="manage_cats_container",
                    style_="display: flex; flex-direction: column; align-items: center; gap: 20px;",
                ),
                ui.panel_absolute(
                    ui.panel_well(
                        ui.input_action_button(
                            id="mng_cat_save_changes",
                            label=_("Save all changes"),
                            icon=icon_svg("floppy-disk"),
                        ),
                        class_="sticky-action-well",
                        style_="text-align: center;",
                    ),
                    draggable=False,
                    width="100%",
                    left="0px",
                    right="0px",
                    bottom="0px",
                    fixed=True,
                ),
            )
        else:
            return ui.div(
                ui.div(
                    ui.help_text(
                        _("No cats in the database yet. Add one below.")
                    ),
                    style_="text-align: center; margin-bottom: 1rem;",
                ),
                ui.output_ui("ui_add_new_cat"),
            )

    @reactive.Effect
    @reactive.event(input.mng_cat_save_changes)
    def manage_cat_save():
        df_cats = CatsRepo.db_get_cats(
            CONFIG["KITTYHACK_DATABASE_PATH"], ReturnDataCatDB.all_except_photos
        )
        updated_cats = []
        if not df_cats.empty:
            for index, data_row in df_cats.iterrows():
                db_id = data_row["id"]
                db_name = data_row["name"]
                db_rfid = data_row["rfid"]
                db_prey = bool(int(data_row.get("enable_prey_detection", 1)))
                db_allow_entry = bool(int(data_row.get("allow_entry", 1)))
                db_allow_exit = bool(int(data_row.get("allow_exit", 1)))

                card_name = input[f"mng_cat_name_{db_id}"]()
                card_rfid = input[f"mng_cat_rfid_{db_id}"]().strip().upper()
                card_prey = input[f"mng_cat_prey_{db_id}"]()
                # Only read new per-cat switches if the mode is per-cat; otherwise keep previous DB values
                if CONFIG["ALLOWED_TO_ENTER"].value == "configure_per_cat":
                    card_allow_entry = input[f"mng_cat_allow_entry_{db_id}"]()
                else:
                    try:
                        row = CatsRepo.db_get_cats(
                            CONFIG["KITTYHACK_DATABASE_PATH"],
                            ReturnDataCatDB.all_except_photos,
                        )
                        prev = row[row["id"] == db_id].iloc[0]
                        card_allow_entry = bool(int(prev.get("allow_entry", 1)))
                    except Exception:
                        card_allow_entry = True
                if CONFIG["ALLOWED_TO_EXIT"].value == "configure_per_cat":
                    card_allow_exit = input[f"mng_cat_allow_exit_{db_id}"]()
                else:
                    try:
                        row = CatsRepo.db_get_cats(
                            CONFIG["KITTYHACK_DATABASE_PATH"],
                            ReturnDataCatDB.all_except_photos,
                        )
                        prev = row[row["id"] == db_id].iloc[0]
                        card_allow_exit = bool(int(prev.get("allow_exit", 1)))
                    except Exception:
                        card_allow_exit = True
                # Get image path, if a file was uploaded
                card_pic: list[FileInfo] | None = input[f"mng_cat_pic_{db_id}"]()
                if card_pic is not None:
                    card_pic_path = card_pic[0]["datapath"]
                else:
                    card_pic_path = None

                    # Only update the cat data if the values have changed
                if (
                    (db_name != card_name)
                    or (db_rfid != card_rfid)
                    or (db_prey != card_prey)
                    or (db_allow_entry != card_allow_entry)
                    or (db_allow_exit != card_allow_exit)
                    or (card_pic_path is not None)
                ):
                    # Add the ID to the list of updated cats
                    updated_cats.append(db_id)

                    result = CatsRepo.db_update_cat_data_by_id(
                        CONFIG["KITTYHACK_DATABASE_PATH"],
                        db_id,
                        card_name,
                        card_rfid,
                        card_pic_path,
                        card_prey,
                        card_allow_entry,
                        card_allow_exit,
                    )
                    if result.success:
                        ui.notification_show(
                            _("Data for {} updated successfully.").format(
                                card_name
                            ),
                            duration=5,
                            type="message",
                        )
                    else:
                        ui.notification_show(
                            _("Failed to update cat details: {}").format(
                                result.message
                            ),
                            duration=10,
                            type="error",
                        )

            if not updated_cats:
                ui.notification_show(
                    _("No changes detected. Nothing to save."),
                    duration=5,
                    type="message",
                )
            else:
                ctx.reload_trigger_cats.set(ctx.reload_trigger_cats.get() + 1)

    @output
    @render.ui
    @reactive.event(ctx.reload_trigger_cats, reload_trigger_config, ignore_none=True)
    def ui_add_new_cat():
        ui_cards = []

        # Flags for per-cat modes
        entry_mode_per_cat = CONFIG["ALLOWED_TO_ENTER"].value == "configure_per_cat"
        exit_mode_per_cat = CONFIG["ALLOWED_TO_EXIT"].value == "configure_per_cat"
        entry_style = (
            "padding-bottom: 20px;"
            if entry_mode_per_cat
            else "pointer-events: none; opacity: 0.6;"
        )
        exit_style = (
            "padding-bottom: 20px;"
            if exit_mode_per_cat
            else "pointer-events: none; opacity: 0.6;"
        )
        prey_style = (
            "padding-bottom: 20px;"
            if CONFIG["MOUSE_CHECK_ENABLED"]
            else "pointer-events: none; opacity: 0.6;"
        )

        # Build settings section inline
        settings_rows_new = [
            ui.row(
                ui.column(
                    12,
                    ui.div(
                        ui.markdown(_("##### Cat-specific settings")),
                        style_="text-align: center;",
                    ),
                )
            ),
            ui.br(),
            ui.row(
                ui.column(
                    12,
                    ui.div(
                        ui.input_switch(
                            "add_new_cat_prey", _("Enable Prey detection"), True
                        ),
                        style_=prey_style,
                    ),
                )
            ),
        ]
        if not CONFIG["MOUSE_CHECK_ENABLED"]:
            settings_rows_new.append(
                ui.row(
                    ui.column(
                        12,
                        ui.markdown(
                            _(
                                "**Disabled:** Global prey detection is turned off in the `CONFIGURATION` section. Enable `Detect prey` to use per-cat settings."
                            )
                        ),
                        style_="color: grey;",
                    ),
                    style_="padding-bottom: 20px;",
                )
            )
        settings_rows_new.append(
            ui.row(
                ui.column(
                    12,
                    ui.div(
                        ui.input_switch(
                            "add_new_cat_allow_entry", _("Allow entry"), True
                        ),
                        style_=entry_style,
                    ),
                )
            )
        )
        if not entry_mode_per_cat:
            settings_rows_new.append(
                ui.row(
                    ui.column(
                        12,
                        ui.markdown(
                            _(
                                "**Disabled:** This feature is only available in `Individual configuration per cat` mode for entry."
                            )
                        ),
                        style_="color: grey;",
                    ),
                    style_="padding-bottom: 20px;",
                )
            )
        settings_rows_new.append(
            ui.row(
                ui.column(
                    12,
                    ui.div(
                        ui.input_switch(
                            "add_new_cat_allow_exit", _("Allow exit"), True
                        ),
                        style_=exit_style,
                    ),
                )
            )
        )
        if not exit_mode_per_cat:
            settings_rows_new.append(
                ui.row(
                    ui.column(
                        12,
                        ui.markdown(
                            _(
                                "**Disabled:** This feature is only available in `Individual configuration per cat` mode for exit."
                            )
                        ),
                        style_="color: grey;",
                    ),
                    style_="padding-bottom: 20px;",
                )
            )

        settings_rows_new.append(
            [
                ui.hr(),
                ui.row(
                    ui.column(
                        12,
                        ui.div(
                            ui.markdown(
                                _(
                                    "**Important:** Individual entry/exit switches only apply when "
                                    "the matching door mode is set to `Individual configuration per cat`."
                                )
                            ),
                            style_="text-align: center;",
                        ),
                    )
                ),
            ]
        )

        settings_section_new_cat = ui.div(
            ui.div(
                *settings_rows_new,
                class_="cat-settings-container",
            ),
            class_="align-left",
        )

        ui_cards.append(
            ui.card(
                ui.card_header(
                    ui.div(
                        ui.h5(_("Add new cat")),
                        ui.column(
                            12,
                            ui.input_text(
                                "add_new_cat_name",
                                label=_("Name"),
                                value="",
                                width="100%",
                            ),
                        ),
                        ui.br(),
                        ui.column(
                            12,
                            ui.input_text(
                                "add_new_cat_rfid",
                                label=_("RFID"),
                                value="",
                                width="100%",
                            ),
                        ),
                        ui.column(12, ui.output_ui("add_new_cat_rfid_status")),
                        ui.br(),
                        ui.column(
                            12,
                            ui.help_text(
                                _(
                                    "You can find the RFID in the [PICTURES] section, if the chip of your cat was recognized by the Kittyflap. To read the RFID, just set the entrance mode to 'All Cats' and let pass your cat through the Kittyflap."
                                )
                            ),
                        ),
                        ui.column(
                            12,
                            ui.help_text(
                                _(
                                    "NOTE: This is NOT the number which stands in the booklet of your vet! You must use the the ID, which is read by the Kittyflap. It is 16 characters long and consists of numbers (0-9) and letters (A-F)."
                                )
                            ),
                        ),
                        ui.br(),
                        settings_section_new_cat,
                        ui.br(),
                        ui.column(
                            12,
                            uix.input_file(
                                "add_new_cat_pic",
                                label=_("Upload Picture"),
                                accept=".jpg",
                                width="100%",
                            ),
                        ),
                        ui.hr(),
                        ui.column(
                            12,
                            ui.input_action_button(
                                "add_new_cat_save",
                                label=_("Save"),
                                icon=icon_svg("floppy-disk"),
                            ),
                        ),
                    )
                ),
                full_screen=False,
                class_="image-container",
            )
        )
        return (ui.layout_column_wrap(*ui_cards, width="400px"),)

    @output
    @render.ui
    def add_new_cat_rfid_status():
        val = (input.add_new_cat_rfid() or "").strip()
        if val == "":
            return ui.div(
                _(
                    "No RFID entered. Cat identification only via camera (if enabled). See CONFIGURATION section for details."
                ),
                class_="rfid-status rfid-empty",
            )
        if re.fullmatch(r"[0-9A-Fa-f]{16}", val):
            return ui.div(_("Valid RFID"), class_="rfid-status rfid-valid")
        return ui.div(
            _("Invalid RFID. Must be exactly 16 hex characters (0-9, A-F)."),
            class_="rfid-status rfid-invalid",
        )

    @reactive.Effect
    @reactive.event(input.add_new_cat_save)
    def add_new_cat_save():
        cat_name = input.add_new_cat_name()
        cat_rfid = input.add_new_cat_rfid().strip().upper()
        cat_pic: list[FileInfo] | None = input.add_new_cat_pic()
        cat_prey = input.add_new_cat_prey()
        if CONFIG["ALLOWED_TO_ENTER"].value == "configure_per_cat":
            cat_allow_entry = input.add_new_cat_allow_entry()
        else:
            cat_allow_entry = True
        if CONFIG["ALLOWED_TO_EXIT"].value == "configure_per_cat":
            cat_allow_exit = input.add_new_cat_allow_exit()
        else:
            cat_allow_exit = True

            # Get image path, if a file was uploaded
        if cat_pic is not None:
            cat_pic_path = cat_pic[0]["datapath"]
        else:
            cat_pic_path = None

        result = CatsRepo.db_add_new_cat(
            CONFIG["KITTYHACK_DATABASE_PATH"],
            cat_name,
            cat_rfid,
            cat_pic_path,
            cat_prey,
            cat_allow_entry,
            cat_allow_exit,
        )
        if result.success:
            ui.notification_show(
                _("New cat {} added successfully.").format(cat_name),
                duration=5,
                type="message",
            )
            ui.update_text(id="add_new_cat_name", value="")
            ui.update_text(id="add_new_cat_rfid", value="")
            ui.update_switch(id="add_new_cat_prey", value=True)
            ui.update_switch(id="add_new_cat_allow_entry", value=True)
            ui.update_switch(id="add_new_cat_allow_exit", value=True)
            ctx.reload_trigger_cats.set(ctx.reload_trigger_cats.get() + 1)
        else:
            ui.notification_show(
                _("An error occurred while adding the new cat: {}").format(
                    result.message
                ),
                duration=10,
                type="error",
            )

    pending_cat_delete = reactive.Value(None)
    _cat_delete_registered_ids = set()

    def _register_cat_delete_button(db_id: int):
        @reactive.effect
        @reactive.event(input[f"mng_cat_del_btn_{db_id}"])
        def _ask_delete_cat():
            try:
                name = (input[f"mng_cat_name_{db_id}"]() or "").strip() or str(db_id)
            except Exception:
                name = str(db_id)
            pending_cat_delete.set({"id": int(db_id), "name": name})
            ui.modal_show(
                ui.modal(
                    _("Do you really want to delete {} from the database?").format(
                        name
                    ),
                    title=_("Delete cat"),
                    easy_close=False,
                    footer=ui.div(
                        ui.input_action_button(
                            "btn_modal_delete_cat_ok",
                            _("Delete"),
                            class_="btn-danger",
                        ),
                        ui.input_action_button("btn_modal_cancel", _("Cancel")),
                    ),
                )
            )

    @reactive.effect
    def _ensure_cat_delete_handlers():
        ctx.reload_trigger_cats.get()
        df_cats = CatsRepo.db_get_cats(
            CONFIG["KITTYHACK_DATABASE_PATH"], ReturnDataCatDB.all_except_photos
        )
        if df_cats.empty:
            return
        for db_id in df_cats["id"].tolist():
            db_id = int(db_id)
            if db_id in _cat_delete_registered_ids:
                continue
            _cat_delete_registered_ids.add(db_id)
            _register_cat_delete_button(db_id)

    @reactive.effect
    @reactive.event(input.btn_modal_delete_cat_ok)
    def on_modal_delete_cat_ok():
        pending = pending_cat_delete.get() or {}
        ui.modal_remove()
        pending_cat_delete.set(None)
        db_id = pending.get("id")
        db_name = pending.get("name") or str(db_id)
        if not db_id:
            return
        result = CatsRepo.db_delete_cat_by_id(
            CONFIG["KITTYHACK_DATABASE_PATH"], db_id
        )
        if result.success:
            ui.notification_show(
                _("{} deleted successfully from the database.").format(db_name),
                duration=5,
                type="message",
            )
            ctx.reload_trigger_cats.set(ctx.reload_trigger_cats.get() + 1)
        else:
            ui.notification_show(
                _("Failed to delete {} from the database: {}").format(
                    db_name, result.message
                ),
                duration=10,
                type="error",
            )
