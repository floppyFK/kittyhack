"""YOLO model manage/activate Shiny modules."""

from shiny import ui, reactive, module
import logging
from faicons import icon_svg
from src.baseconfig import CONFIG, set_language, update_single_config_parameter
from src.mode import is_remote_mode
from src.model import YoloModel
from src.backend import reload_model_handler_runtime
from src.server_ui.state import reload_trigger_ai, reload_trigger_config

_ = set_language(CONFIG["LANGUAGE"])

if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir

from src.server_ui.state import reload_trigger_ai, reload_trigger_config


@module.ui
def btn_yolo_modify():
    """UI fragment: pencil button to open the YOLO model edit modal."""
    return ui.input_action_button(
        id=f"btn_yolo_modify",
        label="",
        icon=icon_svg("pencil", margin_left="0", margin_right="0"),
        class_="btn-icon-square btn-vertical-margin",
    )


@module.ui
def btn_yolo_activate():
    """UI fragment: check button to activate a YOLO model."""
    return ui.input_action_button(
        id=f"btn_yolo_activate",
        label="",
        icon=icon_svg("check", margin_left="0", margin_right="0"),
        class_="btn-icon-square btn-outline-secondary btn-vertical-margin",
    )


@module.server
def manage_yolo_model_server(input, output, session, unique_id: str):
    """Server logic for renaming or deleting a YOLO model by ``unique_id``."""

    @reactive.effect
    @reactive.event(input.btn_yolo_modify)
    def on_modify_yolo_model():
        # We need to read the available models again here to get the metadata
        available_models = YoloModel.get_model_list()
        model = next(
            (mdl for mdl in available_models if mdl["unique_id"] == unique_id), None
        )
        if model is None:
            logging.error(f"YOLO model with unique_id {unique_id} not found.")
            return
        model_in_use = model["unique_id"] == CONFIG["YOLO_MODEL"]
        if model_in_use:
            additional_note = ui.div(
                _("This model is currently in use. You can not delete it."),
                class_="alert alert-info",
                style_="margin-bottom: 0px; margin-top: 10px; padding: 10px",
            )
        else:
            additional_note = ""

        m = ui.modal(
            ui.div(
                ui.input_text(
                    "txtModelName", _("Name"), model["display_name"], width="100%"
                ),
                style_="width: 100%;",
            ),
            ui.div(
                ui.input_text(
                    "txtModelUniqueID", _("Unique ID"), model["unique_id"], width="100%"
                ),
                class_="disabled-wrapper",
                style_="width: 100%;",
            ),
            ui.hr(),
            ui.markdown(_("Model created at: {}").format(model["creation_date"])),
            additional_note,
            title=_("Change model configuration"),
            easy_close=False,
            footer=ui.div(
                ui.input_action_button(
                    id="btn_model_save",
                    label=_("Save"),
                    class_="btn-vertical-margin btn-narrow",
                ),
                ui.input_action_button(
                    id="btn_modal_cancel",
                    label=_("Cancel"),
                    class_="btn-vertical-margin btn-narrow",
                ),
                ui.input_action_button(
                    id=f"btn_model_delete",
                    label=_("Delete"),
                    icon=icon_svg("trash"),
                    class_=f"btn-vertical-margin btn-narrow btn-outline-danger {'disabled-wrapper' if model_in_use else ''}",
                ),
            ),
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_model_save)
    def model_save():
        unique_id = input.txtModelUniqueID()
        model_name = input.txtModelName()
        logging.info(
            f"Updating Model configuration for Unique ID {unique_id}: Name={model_name}"
        )
        ui.modal_remove()
        success = YoloModel.rename_model(unique_id, model_name)
        if success:
            ui.notification_show(
                _("Model configuration {} updated successfully.").format(model_name),
                duration=5,
                type="message",
            )
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)
            reload_trigger_config.set(reload_trigger_config.get() + 1)
        else:
            ui.notification_show(
                _("Failed to update model configuration {}").format(model_name),
                duration=10,
                type="error",
            )
        ui.modal_remove()

    @reactive.effect
    @reactive.event(input.btn_modal_cancel)
    def modal_cancel():
        ui.modal_remove()

    # Ask the user for confirmation before deleting the model
    @reactive.effect
    @reactive.event(input.btn_model_delete)
    def model_delete():
        # First remove the previous modal correctly to avoid multiple modals
        unique_id = input.txtModelUniqueID()
        model_name = input.txtModelName()
        ui.modal_remove()

        m = ui.modal(
            _("Do you really want to delete this model?"),
            title=_("Delete Model"),
            easy_close=False,
            footer=ui.div(
                ui.div(
                    ui.input_action_button("btn_modal_delete_model_ok", _("OK")),
                    ui.input_action_button("btn_modal_cancel", _("Cancel")),
                ),
                ui.div(
                    # Helper input to pass the unique_id to the delete function
                    ui.input_text("txtModelName", "", model_name),
                    ui.input_text("txtModelUniqueID", "", unique_id),
                    style_="display:none;",
                ),
            ),
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_modal_delete_model_ok)
    def modal_delete_model_ok():
        unique_id = input.txtModelUniqueID()
        success = YoloModel.delete_model(unique_id)
        if success:
            ui.notification_show(
                _("Model {} deleted successfully.").format(input.txtModelName()),
                duration=5,
                type="message",
            )
            ui.modal_remove()
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)
            reload_trigger_config.set(reload_trigger_config.get() + 1)
        else:
            ui.notification_show(
                _("Failed to delete Model {}").format(input.txtModelName()),
                duration=10,
                type="error",
            )
        ui.modal_remove()


@module.server
def activate_yolo_model_server(input, output, session, unique_id: str):
    """Server logic for selecting ``unique_id`` as the active YOLO model."""

    @reactive.effect
    @reactive.event(input.btn_yolo_activate)
    def on_activate_yolo_model():
        global model_handler

        if not (unique_id or "").strip():
            ui.notification_show(
                _("This model cannot be selected because it has no unique ID."),
                duration=8,
                type="error",
            )
            return

        if CONFIG.get("YOLO_MODEL") == unique_id and not CONFIG.get(
            "TFLITE_MODEL_VERSION"
        ):
            ui.notification_show(
                _("This model is already active."), duration=5, type="message"
            )
            return

        model_path = YoloModel.get_model_path(unique_id)
        if not model_path:
            ui.notification_show(
                _("The selected model was not found on disk."), duration=8, type="error"
            )
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)
            reload_trigger_config.set(reload_trigger_config.get() + 1)
            return

        CONFIG["YOLO_MODEL"] = unique_id
        CONFIG["TFLITE_MODEL_VERSION"] = ""
        update_single_config_parameter("YOLO_MODEL")
        update_single_config_parameter("TFLITE_MODEL_VERSION")

        reload_ok, active_model_handler = reload_model_handler_runtime()
        if reload_ok:
            model_handler = active_model_handler
            ui.notification_show(
                _("Model switched successfully."), duration=6, type="message"
            )
        else:
            ui.notification_show(
                _(
                    "Model was selected, but live reload failed. Please reboot to apply the change."
                ),
                duration=10,
                type="warning",
            )

        reload_trigger_ai.set(reload_trigger_ai.get() + 1)
        reload_trigger_config.set(reload_trigger_config.get() + 1)
