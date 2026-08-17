"""AI training and Label Studio handlers."""

import os
import time as tm
import tempfile
from src.clock import monotonic_time
from shiny import render, ui, reactive
import logging
from faicons import icon_svg
import re
import hashlib
import asyncio
from src.baseconfig import CONFIG, set_language, update_single_config_parameter
from src.helper import (
    SystemInfo,
    is_valid_uuid4,
)
from src.system import (
    LabelStudioInstall,
    ServiceOps,
)
from src.mode import is_remote_mode
from src.labelstudio_api import (
    get_labelstudio_projects_list,
    export_labelstudio_project_as_zip,
    get_labelstudio_project_task_summary,
)
from src.model import YoloModel, RemoteModelTrainer
from src.shiny_wrappers import uix
from src.server_ui.state import (
    reload_trigger_ai,
    reload_trigger_config,
    ls_project_choices,
)
from src.server_ui.helpers import _disable_input, collapsible_section
from src.server_ui.context import SessionContext

_ = set_language(CONFIG["LANGUAGE"])

if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir

from src.server_ui.yolo_modules import (
    btn_yolo_modify,
    btn_yolo_activate,
    manage_yolo_model_server,
    activate_yolo_model_server,
)


def register_ai_training(input, output, session, ctx: SessionContext):
    """Register AI Training / Label Studio tab handlers."""

    @output
    @render.ui
    @reactive.event(reload_trigger_ai, ignore_none=True)
    def ui_ai_training():

        # Check labelstudio
        if CONFIG["LABELSTUDIO_VERSION"] is not None:
            labelstudio_latest_version = (
                LabelStudioInstall.get_labelstudio_latest_version()
            )
            labelstudio_latest_version_display = labelstudio_latest_version or _(
                "unknown"
            )

            # Check if labelstudio is running
            if LabelStudioInstall.get_labelstudio_status() == True:
                ui_labelstudio = ui.div(
                    ui.div(
                        ui.input_task_button(
                            "btn_labelstudio_stop",
                            _("Stop Label Studio"),
                            icon=icon_svg("stop"),
                        ),
                        ui.HTML(
                            ' <a href="http://{}:8080" target="_blank" class="btn btn-default">{}</a>'.format(
                                SystemInfo.get_current_ip(), _("Open Label Studio")
                            )
                        ),
                        class_="d-flex gap-2 justify-content-center flex-wrap",
                    ),
                    ui.help_text(
                        _("Label Studio Version: {}").format(
                            CONFIG["LABELSTUDIO_VERSION"]
                        )
                        + " · "
                        + _("Latest: {}").format(labelstudio_latest_version_display)
                    ),
                    style_="text-align:center;",
                )
            else:
                ui_labelstudio = ui.div(
                    ui.input_task_button(
                        "btn_labelstudio_start",
                        _("Start Label Studio"),
                        icon=icon_svg("play"),
                    ),
                    ui.br(),
                    ui.help_text(
                        _("Label Studio is not running."), style_="margin-top:0.5rem;"
                    ),
                    ui.help_text(
                        _("Version: {}").format(CONFIG["LABELSTUDIO_VERSION"])
                        + " · "
                        + _("Latest: {}").format(labelstudio_latest_version_display)
                    ),
                    style_="text-align:center;",
                )

            if (
                labelstudio_latest_version is not None
                and labelstudio_latest_version != CONFIG["LABELSTUDIO_VERSION"]
            ):
                ui_labelstudio = (
                    ui_labelstudio,
                    ui.div(
                        ui.hr(),
                        ui.input_task_button(
                            "btn_labelstudio_update",
                            _("Update Label Studio"),
                            icon=icon_svg("circle-up"),
                            class_="btn-primary",
                        ),
                        ui.help_text(
                            CONFIG["LABELSTUDIO_VERSION"]
                            + " → "
                            + labelstudio_latest_version_display,
                            style_="margin-top:0.5rem;",
                        ),
                        style_="text-align:center;",
                    ),
                )

            ui_labelstudio = (
                ui_labelstudio,
                ui.hr(),
                ui.div(
                    ui.markdown(_("#### API Token")),
                    ui.help_text(
                        _("Required to access Label Studio projects from this page.")
                    ),
                    ui.br(),
                    ui.div(
                        ui.tags.button(
                            ui.tags.span(
                                "\u25b6",
                                class_="toggle-chevron",
                                style_="display:inline-block; transition:transform .2s;",
                            ),
                            " ",
                            _("How to create an API token"),
                            type="button",
                            class_="btn btn-link p-0 info-toggle-btn",
                            style_="text-decoration:none;",
                            **{
                                "data-bs-toggle": "collapse",
                                "data-bs-target": "#labelstudio_token_help_body",
                                "aria-expanded": "false",
                                "aria-controls": "labelstudio_token_help_body",
                            },
                        ),
                        ui.div(
                            ui.br(),
                            ui.markdown(
                                _("**Option 1: Legacy Token (preferred)**")
                                + "  \n"
                                + _(
                                    "1. Open Label Studio in your browser (button above)"
                                )
                                + "  \n"
                                + _(
                                    "2. In Label Studio, go to 'burger menu' → Organization"
                                )
                                + "  \n"
                                + _(
                                    "3. Click on 'API Tokens Settings' and enable 'Legacy Tokens'"
                                )
                                + "  \n"
                                + _("4. Click your user icon → Account & Settings")
                                + "  \n"
                                + _(
                                    "5. Find 'Legacy Token' section and copy the 'Access Token'"
                                )
                                + "  \n"
                                + _(
                                    "6. Important: the 'Copy token' button in Label Studio does NOT work. Please copy the token manually."
                                )
                                + "  \n"
                                + _("7. Paste it below and click Save")
                                + "  \n"
                                + "  \n"
                                + _("**Option 2: Personal Access Token**")
                                + "  \n"
                                + _(
                                    "1. Open Label Studio in your browser (button above)"
                                )
                                + "  \n"
                                + _(
                                    "2. Click on your user icon (top right) → Account & Settings"
                                )
                                + "  \n"
                                + _("3. Scroll to 'Personal Access Tokens' section")
                                + "  \n"
                                + _("4. Click 'Create new token'")
                                + "  \n"
                                + _(
                                    "5. Important: the 'Copy token' button in Label Studio does NOT work. Please copy the token manually."
                                )
                                + "  \n"
                                + _("6. Paste it below and click Save")
                            ),
                            id="labelstudio_token_help_body",
                            class_="collapse",
                            style_="text-align: left; padding: 1rem; background-color: var(--bs-body-bg); border: 1px solid var(--bs-border-color); border-radius: 0.25rem; margin-top: 0.5rem;",
                        ),
                    ),
                    ui.br(),
                    ui.div(
                        ui.input_password(
                            "labelstudio_api_token",
                            _("Label Studio API Token"),
                            value=CONFIG.get("LABELSTUDIO_API_TOKEN", ""),
                            placeholder=_(
                                "Paste Personal Access Token or Legacy Token here"
                            ),
                            width="100%",
                        ),
                        ui.br(),
                        ui.input_action_button(
                            "btn_save_labelstudio_token",
                            _("Save Token"),
                            class_="btn-primary",
                        ),
                        style_="max-width:600px; margin:0 auto;",
                    ),
                    style_="text-align:center;",
                ),
                ui.hr(),
                ui.div(
                    ui.markdown(_("#### Project")),
                    ui.output_ui("ui_labelstudio_project_selector"),
                    style_="text-align:center;",
                ),
                ui.hr(),
                ui.div(
                    ui.input_task_button(
                        "btn_labelstudio_remove",
                        _("Remove Label Studio"),
                        icon=icon_svg("trash"),
                        class_="btn-danger btn-sm",
                    ),
                    ui.br(),
                    ui.help_text(
                        _("Your project data will not be deleted."),
                        style_="margin-top:0.5rem;",
                    ),
                    style_="text-align:center;",
                ),
            )

            # If labelstudio is not installed, show the install button
        else:
            ui_labelstudio = ui.div(
                ui.markdown(
                    _(
                        "**Option 1** – Install on the Kittyflap (easy, limited resources)"
                    )
                    + "  \n"
                    + _(
                        "**Option 2** – Install on your own computer ([labelstud.io](https://labelstud.io/))"
                    )
                    + "  \n\n"
                    + _(
                        "> Some Kittyflaps have only 1 GB RAM. Stop Label Studio when not in use."
                    )
                ),
                ui.hr(),
                ui.div(
                    ui.input_task_button(
                        "btn_labelstudio_install",
                        _("Install Label Studio on the Kittyflap"),
                    ),
                    ui.help_text(
                        _(
                            "This will take 5–10 minutes. The Kittyflap may not be reachable during installation."
                        ),
                        style_="margin-top:0.5rem;",
                    ),
                    style_="text-align:center;",
                ),
            )

            # Check if a model training is in progress
        training_status = RemoteModelTrainer.check_model_training_result(
            show_in_progress=True, return_pretty_status=True
        )

        # Show user notifications, if they are any
        if getattr(ctx, "show_user_notifications", None):
            ctx.show_user_notifications()

            # URLs for different languages
        wiki_url = {
            "de": "https://github.com/floppyFK/kittyhack/wiki/%5BDE%5D-Kittyhack-v2.0-%E2%80%90-Eigene-KI%E2%80%90Modelle-trainieren",
            "en": "https://github.com/floppyFK/kittyhack/wiki/%5BEN%5D-Kittyhack-v2.0-%E2%80%90-Train-own-AI%E2%80%90Models",
        }.get(
            CONFIG["LANGUAGE"],
            "https://github.com/floppyFK/kittyhack/wiki/%5BEN%5D-Kittyhack-v2.0-%E2%80%90-Train-own-AI%E2%80%90Models",
        )

        # --- Server maintenance check when no training is in progress ---
        server_status = None
        training_in_progress = is_valid_uuid4(CONFIG["MODEL_TRAINING"])
        if not training_in_progress:
            server_status = RemoteModelTrainer.get_server_status()

            # Build Model Training section content depending on maintenance state or active training
        if training_in_progress:
            # Already in progress: show existing status UI only
            training_content = ui.div(
                ui.markdown(
                    _("A model training is currently in progress.")
                    + (
                        _(
                            "You will be notified by email when the training is finished."
                        )
                        if CONFIG["EMAIL"]
                        else ""
                    )
                ),
                ui.markdown(_("Current status: {}").format(training_status)),
                ui.br(),
                ui.br(),
                ui.input_task_button(
                    "btn_reload_model_training_status",
                    _("Reload Model Training Status"),
                    class_="btn-primary",
                ),
                ui.hr(),
                ui.markdown(
                    _("Your individual Training ID:")
                    + "\n\n"
                    + f"`{CONFIG['MODEL_TRAINING']}`"
                ),
                ui.help_text(_("(use this ID for support requests)")),
                ui.br(),
                ui.br(),
                ui.input_task_button(
                    "btn_cancel_model_training",
                    _("Cancel Model Training"),
                    class_="btn-danger",
                ),
                id="model_training_status",
                style_="text-align: center;",
            )
        else:
            # No training in progress: check maintenance or timeout/error
            is_maintenance = bool(
                server_status and server_status.get("maintenance", False)
            )
            maintenance_msg = (server_status or {}).get("message", "")
            if server_status is None:
                # Timeout or request failure: inform user
                training_content = ui.div(
                    ui.markdown(
                        f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                        + _(
                            "Unable to reach the model training server right now. Please check your network or try again later."
                        )
                    ),
                    ui.br(),
                    ui.markdown(
                        _(
                            "If the issue persists, the server may be temporarily unavailable."
                        )
                    ),
                    style_="text-align: center;",
                )
            elif is_maintenance:
                # Show maintenance notice and do not render upload inputs
                msg = _(
                    "The model training server is currently in maintenance. Please try again later."
                )
                if maintenance_msg:
                    msg += "\n\n" + maintenance_msg
                training_content = ui.div(
                    ui.markdown(f"{icon_svg('wrench', margin_left='-0.1em')} {msg}"),
                    style_="text-align: center;",
                )
            else:
                # Show the original upload form
                model_training_base_model_input = ui.input_select(
                    "model_training_base_model",
                    _("YOLOv8 model"),
                    {
                        "n": "YOLOv8n",
                        "s": "YOLOv8s",
                        "m": "YOLOv8m",
                        "l": "YOLOv8l",
                        "x": "YOLOv8x",
                    },
                    selected="n",
                    width="90%",
                )
                model_training_image_size_input = ui.input_select(
                    "model_training_image_size",
                    _("Image size"),
                    {str(s): str(s) for s in YoloModel.get_supported_image_sizes()},
                    selected="320",
                    width="90%",
                )

                if not is_remote_mode():
                    model_training_base_model_input = _disable_input(
                        model_training_base_model_input
                    )
                    model_training_image_size_input = _disable_input(
                        model_training_image_size_input
                    )

                    # Build training data source options
                api_token = CONFIG.get("LABELSTUDIO_API_TOKEN", "").strip()
                saved_project = CONFIG.get("LABELSTUDIO_PROJECT", "").strip()
                project_title = (
                    CONFIG.get("LABELSTUDIO_PROJECT_TITLE", "").strip() or saved_project
                )
                labelstudio_source_label = _("Export from Label Studio project")
                if project_title:
                    labelstudio_source_label += f" ({project_title})"

                ls_training_source_options = {
                    "__labelstudio__": labelstudio_source_label,
                    "__manual__": _("Upload project zip manually"),
                }
                default_training_source = (
                    "__labelstudio__"
                    if (
                        api_token
                        and saved_project
                        and LabelStudioInstall.get_labelstudio_status()
                    )
                    else "__manual__"
                )

                training_content = ui.div(
                    ui.div(
                        ui.div(
                            ui.markdown(_("### Training Data")),
                            ui.input_select(
                                "model_training_labelstudio_project",
                                _("Training data source"),
                                ls_training_source_options,
                                selected=default_training_source,
                                width="90%",
                            ),
                            ui.output_ui("ui_model_training_project_note"),
                            ui.output_ui("ui_model_training_data_upload"),
                            ui.input_text(
                                "model_name",
                                _("Model Name (optional)"),
                                placeholder=_("Enter a name for your model"),
                                width="90%",
                            ),
                            ui.input_text(
                                "user_name",
                                _("Username (optional)"),
                                value=CONFIG["USER_NAME"],
                                placeholder=_("Enter your name"),
                                width="90%",
                            ),
                            ui.input_text(
                                "email_notification",
                                _("Email for Notification (optional)"),
                                value=CONFIG["EMAIL"],
                                placeholder=_("Enter your email address"),
                                width="90%",
                            ),
                            ui.help_text(
                                _(
                                    "If you provide an email address, you will be notified when the model training is finished."
                                )
                            ),
                            (
                                ui.div(
                                    ui.tags.button(
                                        ui.tags.span(
                                            "\u25b6",
                                            class_="toggle-chevron",
                                            style_="display:inline-block; transition:transform .2s;",
                                        ),
                                        " ",
                                        _("Advanced options"),
                                        type="button",
                                        class_="btn btn-link p-0 info-toggle-btn",
                                        style_="text-decoration:none;",
                                        **{
                                            "data-bs-toggle": "collapse",
                                            "data-bs-target": "#model_training_advanced_options_body",
                                            "aria-expanded": "false",
                                            "aria-controls": "model_training_advanced_options_body",
                                        },
                                    ),
                                    ui.div(
                                        ui.div(
                                            ui.br(),
                                            model_training_base_model_input,
                                            ui.help_text(
                                                _(
                                                    "Base model (YOLOv8n/s/m/l/x): larger variants are usually more accurate, but they run slower and need more CPU/RAM. "
                                                    "Choose smaller variants (e.g. 'n') if your device struggles to keep up in real-time."
                                                )
                                            ),
                                            model_training_image_size_input,
                                            ui.help_text(
                                                _(
                                                    "Image size (imgsz): higher values can improve detection of small objects, but increase computation and can reduce FPS."
                                                )
                                            ),
                                            ui.help_text(
                                                _(
                                                    "Defaults: YOLOv8n and image size 320."
                                                )
                                            ),
                                            class_="kh-info-box",
                                        ),
                                        id="model_training_advanced_options_body",
                                        class_="collapse info-toggle-body",
                                        style_="margin-top:6px;",
                                    ),
                                    class_="kh-info-toggle",
                                    style_="width: 90%; text-align: left; margin: 0.75rem auto 0 auto;",
                                )
                                if is_remote_mode()
                                else ui.div(
                                    ui.br(),
                                    ui.hr(),
                                    ui.help_text(
                                        _(
                                            "Advanced model options are only available if you run Kittyhack on a separate server (remote mode)."
                                        )
                                    ),
                                    style_="text-align: center;",
                                ),
                            ),
                            ui.br(),
                            ui.hr(),
                            ui.br(),
                            ui.input_task_button(
                                "submit_model_training",
                                _("Submit Model for Training"),
                                class_="btn-primary",
                            ),
                            ui.tags.script(
                                ui.HTML("""
                                    (function() {
                                        var btn = document.getElementById('submit_model_training');
                                        if (!btn) return;
                                        btn.disabled = true;
                                        var tid = setInterval(function() {
                                            if (!document.body.contains(btn)) { clearInterval(tid); return; }

                                            var srcSelect = document.querySelector('select[id*="model_training_labelstudio_project"]');
                                            var src = srcSelect ? srcSelect.value : '';

                                            if (src === '__labelstudio__') {
                                                // LS project: enable if no warning note is visible
                                                var note = document.getElementById('ls_project_task_warning');
                                                btn.disabled = !!note;
                                            } else {
                                                // Manual upload: enable when file upload is complete
                                                var bar = document.querySelector('#model_training_data_progress .progress-bar');
                                                var fileUploaded = bar && bar.textContent.trim().toLowerCase() === 'upload complete';
                                                btn.disabled = !fileUploaded;
                                            }
                                        }, 300);
                                    })();
                                """)
                            ),
                            id="model_training_form",
                            style_="display: flex; flex-direction: column; align-items: center; justify-content: center; width: 100%;",
                        ),
                        style_="text-align: center; width: 100%;",
                    ),
                )

        ui_ai_training = ui.div(
            ui.div(
                ui.br(),
                # Compact intro (replaces the old Description card)
                ui.div(
                    ui.markdown(
                        _(
                            "Train your own AI model: label images with Label Studio, then submit them for training."
                        )
                        + " "
                        + _("Read the instructions before starting:")
                    ),
                    ui.HTML(
                        f'<a href="{wiki_url}" target="_blank" class="btn btn-sm btn-outline-secondary" style="margin-top:0.25rem; white-space: normal; max-width: 100%; display: inline-flex; align-items: center; justify-content: center; flex-wrap: wrap; text-align: center;">'
                        f'<i class="fa fa-clipboard-list" style="margin-right:5px;"></i>'
                        + _("Instructions for training your own model")
                        + "</a>"
                    ),
                    style_="text-align:center; margin-bottom:1.5rem;",
                ),
                collapsible_section(
                    "ai_labelstudio",
                    "Label Studio",
                    (
                        _(
                            "Manage Label Studio, configure API access, and select a project."
                        )
                        if CONFIG["LABELSTUDIO_VERSION"] is not None
                        else _("Install Label Studio to start labeling your images.")
                    ),
                    ui.div(
                        ui_labelstudio,
                        class_="generic-container align-left",
                        style_="padding-left:1rem !important; padding-right:1rem !important;",
                    ),
                ),
                collapsible_section(
                    "ai_model_training",
                    _("Model Training"),
                    (
                        _("A model training is in progress.")
                        if training_in_progress
                        else _("Upload labeled data and submit for model training.")
                    ),
                    ui.div(
                        training_content,
                        class_="generic-container align-left",
                        style_="padding-left:1rem !important; padding-right:1rem !important;",
                    ),
                ),
                collapsible_section(
                    "ai_model_management",
                    _("Model Management"),
                    _("Rename, activate, or remove your trained models."),
                    ui.div(
                        ui.output_ui("manage_yolo_models_table"),
                        class_="generic-container align-left",
                        style_="padding-left:1rem !important; padding-right:1rem !important;",
                    ),
                ),
                class_="generic-container align-left",
                style_="padding-left:1rem !important; padding-right:1rem !important;",
            ),
            ui.br(),
            ui.br(),
            ui.br(),
        )
        return ui_ai_training

    @output
    @render.ui
    def manage_yolo_models_table():
        # Keep the table reasonably fresh while the AI tab is open.
        # Do not wrap this output in @reactive.event: that isolates invalidate_later().
        reload_trigger_ai.get()
        reactive.invalidate_later(60)

        try:
            available_models = YoloModel.get_model_list() or []
            if not available_models:
                return ui.div(
                    _("Nothing here yet. Please train a model first."),
                    class_="text-muted small",
                )

            rows = []
            for model in available_models:
                unique_btn_id = hashlib.md5(os.urandom(16)).hexdigest()
                unique_model_id = model.get("unique_id")
                if not unique_model_id:
                    continue

                is_active_model = (CONFIG.get("YOLO_MODEL") == unique_model_id) and (
                    not (CONFIG.get("TFLITE_MODEL_VERSION") or "").strip()
                )

                fps = model.get("effective_fps", None)
                fps_text = "—"
                try:
                    if fps is not None:
                        fps_text = f"{float(fps):.1f}"
                except Exception:
                    fps_text = "—"

                model_image_size = model.get("model_image_size", 320) or 320
                yolo_variant = (
                    model.get("yolo_variant") or "yolov8n.pt"
                ).strip() or "yolov8n.pt"

                variant_letter = "N"
                variant_title = _("YOLO v8 Nano")
                variant_match = re.search(r"yolov\d+\s*([nslmx])", yolo_variant.lower())
                if variant_match:
                    v = variant_match.group(1)
                    variant_map = {
                        "n": ("N", _("YOLO v8 Nano")),
                        "s": ("S", _("YOLO v8 Small")),
                        "m": ("M", _("YOLO v8 Medium")),
                        "l": ("L", _("YOLO v8 Large")),
                        "x": ("X", _("YOLO v8 Extra Large")),
                    }
                    variant_letter, variant_title = variant_map.get(
                        v, (v.upper(), _("YOLO v8 model"))
                    )

                def build_badges(id_suffix: str):
                    _badges = [
                        ui.tooltip(
                            ui.span(variant_letter, class_="badge text-bg-secondary"),
                            variant_title,
                            id=f"tooltip_yolo_variant_{unique_btn_id}_{id_suffix}",
                            options={"trigger": "hover"},
                        ),
                        ui.tooltip(
                            ui.span(
                                str(int(model_image_size)),
                                class_="badge text-bg-secondary",
                            ),
                            _("Model image size: {}px").format(int(model_image_size)),
                            id=f"tooltip_yolo_img_size_{unique_btn_id}_{id_suffix}",
                            options={"trigger": "hover"},
                        ),
                        ui.tooltip(
                            ui.span(
                                f"{fps_text} FPS", class_="badge text-bg-secondary"
                            ),
                            _("Average FPS with this model"),
                            id=f"tooltip_yolo_fps_{unique_btn_id}_{id_suffix}",
                            options={"trigger": "hover"},
                        ),
                    ]
                    if is_active_model:
                        _badges.insert(
                            0,
                            ui.tooltip(
                                ui.span(_("Active"), class_="badge text-bg-success"),
                                _("Currently active model"),
                                id=f"tooltip_yolo_active_{unique_btn_id}_{id_suffix}",
                                options={"trigger": "hover"},
                            ),
                        )
                    return _badges

                badges_desktop = build_badges("desktop")
                badges_mobile = build_badges("mobile")

                actions = ui.div(
                    ui.tooltip(
                        btn_yolo_activate(f"btn_yolo_activate_{unique_btn_id}"),
                        _("Use this model now"),
                        id=f"tooltip_yolo_activate_{unique_btn_id}",
                        options={"trigger": "hover"},
                    ),
                    ui.tooltip(
                        btn_yolo_modify(f"btn_yolo_modify_{unique_btn_id}"),
                        _("Modify or delete this model"),
                        id=f"tooltip_yolo_modify_{unique_btn_id}",
                        options={"trigger": "hover"},
                    ),
                    class_="d-flex gap-1 justify-content-end",
                )

                # Register event listeners for the buttons
                activate_yolo_model_server(
                    f"btn_yolo_activate_{unique_btn_id}", unique_model_id
                )
                manage_yolo_model_server(
                    f"btn_yolo_modify_{unique_btn_id}", unique_model_id
                )

                name_cell = ui.div(
                    ui.div(
                        ui.span(model.get("display_name", ""), class_="kh-model-name"),
                        class_="d-flex align-items-start gap-2",
                    ),
                    ui.div(model.get("creation_date", ""), class_="kh-model-sub"),
                    ui.div(
                        *badges_mobile, class_="d-flex d-sm-none flex-wrap gap-1 mt-1"
                    ),
                )

                specs_cell = ui.div(
                    ui.div(
                        *badges_desktop,
                        class_="d-none d-sm-flex flex-wrap gap-1 justify-content-center",
                    ),
                )

                rows.append(
                    ui.tags.tr(
                        ui.tags.td(name_cell),
                        ui.tags.td(
                            specs_cell, class_="kh-model-specs kh-model-specs-col"
                        ),
                        ui.tags.td(actions, class_="kh-model-actions"),
                    )
                )

            table = ui.tags.table(
                ui.tags.tbody(*rows),
                class_="table table-sm align-middle table_models_overview",
            )

            return ui.div(table, class_="table-responsive")

        except Exception as e:
            logging.error(f"Failed to build YOLO model management table: {e}")
            return ui.div(
                _("Nothing here yet. Please train a model first."),
                class_="text-muted small",
            )

    @reactive.Effect
    @reactive.event(input.btn_reload_model_training_status)
    def on_reload_model_training_status():
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)
        reload_trigger_config.set(reload_trigger_config.get() + 1)

    @reactive.Effect
    @reactive.event(input.btn_cancel_model_training)
    def on_cancel_model_training():
        # Check if a model training is in progress
        if not is_valid_uuid4(CONFIG["MODEL_TRAINING"]):
            ui.notification_show(
                _("No model training in progress."), duration=15, type="error"
            )
            RemoteModelTrainer._clear_training_job()
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)
            return

            # Show confirmation dialog
        m = ui.modal(
            _("Do you really want to cancel the model training?"),
            title=_("Cancel Model Training"),
            easy_close=True,
            footer=ui.div(
                ui.input_action_button("btn_modal_cancel_training_ok", _("OK")),
                ui.input_action_button("btn_modal_cancel", _("Cancel")),
            ),
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_modal_cancel_training_ok)
    def modal_cancel_training():
        ui.modal_remove()
        # Cancel the model training
        RemoteModelTrainer.cancel_model_training(CONFIG["MODEL_TRAINING"])
        RemoteModelTrainer._clear_training_job()
        ui.notification_show(
            _("Model training cancelled."), duration=15, type="message"
        )
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    @output
    @render.ui
    def ui_model_training_data_upload():
        """Show/hide file upload based on training data source selection."""
        try:
            selected = input.model_training_labelstudio_project()
            if selected == "__manual__" or selected is None:
                return ui.div(
                    uix.input_file(
                        "model_training_data",
                        _("Upload Label-Studio Training Data (ZIP file)"),
                        accept=".zip",
                        multiple=False,
                        width="90%",
                    ),
                    ui.br(),
                    style_="width: 100%; max-width: 720px; margin: 0 auto; display: flex; flex-direction: column; align-items: center;",
                )
            else:
                return ui.div()
        except Exception:
            return ui.div(
                uix.input_file(
                    "model_training_data",
                    _("Upload Label-Studio Training Data (ZIP file)"),
                    accept=".zip",
                    multiple=False,
                    width="90%",
                ),
                ui.br(),
                style_="width: 100%; max-width: 720px; margin: 0 auto; display: flex; flex-direction: column; align-items: center;",
            )

    @output
    @render.ui
    def ui_model_training_project_note():
        """Show a warning if the selected LS project has no/incomplete labeled pictures."""
        # Read reload_trigger_ai so this output re-executes when the AI tab reloads
        # (e.g. after saving a new project selection).
        reload_trigger_ai.get()

        try:
            selected = input.model_training_labelstudio_project()
        except Exception:
            return ui.div()

        if selected != "__labelstudio__":
            return ui.div()

        api_token = CONFIG.get("LABELSTUDIO_API_TOKEN", "").strip()
        if not api_token:
            return ui.div(
                ui.div(
                    ui.div(
                        icon_svg("circle-info", margin_left="-0.1em"),
                        " ",
                        _(
                            "No Label Studio API token configured. If you want to train a model directly from a Label Studio project, please add your API token in the Label Studio card above."
                        ),
                        style_="margin-bottom: 0;",
                    ),
                    id="ls_project_task_warning",
                    class_="alert alert-info",
                    style_="text-align: left; margin-top: 0.5rem;",
                ),
            )

        saved_project = CONFIG.get("LABELSTUDIO_PROJECT", "").strip()
        if not saved_project:
            return ui.div(
                ui.div(
                    ui.div(
                        icon_svg("triangle-exclamation", margin_left="-0.1em"),
                        " ",
                        _(
                            "No Label Studio project selected. Please select a project in the Label Studio card above."
                        ),
                        style_="margin-bottom: 0;",
                    ),
                    id="ls_project_task_warning",
                    class_="alert alert-warning",
                    style_="text-align: left; margin-top: 0.5rem;",
                ),
            )

        if not LabelStudioInstall.get_labelstudio_status():
            return ui.div(
                ui.div(
                    ui.div(
                        icon_svg("triangle-exclamation", margin_left="-0.1em"),
                        " ",
                        _(
                            "Label Studio is not running. Please start it in the Label Studio card above."
                        ),
                        style_="margin-bottom: 0;",
                    ),
                    id="ls_project_task_warning",
                    class_="alert alert-warning",
                    style_="text-align: left; margin-top: 0.5rem;",
                ),
            )

        try:
            project_id = int(saved_project)
        except (ValueError, TypeError):
            return ui.div()

        summary = get_labelstudio_project_task_summary(project_id)
        if summary is None:
            return ui.div(
                ui.div(
                    ui.div(
                        icon_svg("triangle-exclamation", margin_left="-0.1em"),
                        " ",
                        _(
                            "Could not retrieve picture information for the selected project."
                        ),
                        style_="margin-bottom: 0;",
                    ),
                    id="ls_project_task_warning",
                    class_="alert alert-warning",
                    style_="text-align: left; margin-top: 0.5rem;",
                ),
            )

        if summary["total_tasks"] == 0:
            return ui.div(
                ui.div(
                    ui.div(
                        icon_svg("triangle-exclamation", margin_left="-0.1em"),
                        " ",
                        _(
                            "The selected project has no pictures. Please add and label images in Label Studio first."
                        ),
                        style_="margin-bottom: 0;",
                    ),
                    id="ls_project_task_warning",
                    class_="alert alert-warning",
                    style_="text-align: left; margin-top: 0.5rem;",
                ),
            )

        if not summary["ready"]:
            return ui.div(
                ui.div(
                    ui.div(
                        icon_svg("triangle-exclamation", margin_left="-0.1em"),
                        " ",
                        _(
                            "The selected project has {unlabeled} of {total} pictures without labels. Please complete all labels before training."
                        ).format(
                            unlabeled=summary["unannotated_tasks"],
                            total=summary["total_tasks"],
                        ),
                        style_="margin-bottom: 0;",
                    ),
                    id="ls_project_task_warning",
                    class_="alert alert-warning",
                    style_="text-align: left; margin-top: 0.5rem;",
                ),
            )

            # All good
        return ui.div(
            ui.div(
                ui.div(
                    icon_svg("circle-check", margin_left="-0.1em"),
                    " ",
                    _("Project ready: {labeled} labeled pictures.").format(
                        labeled=summary["annotated_tasks"],
                    ),
                    style_="margin-bottom: 0;",
                ),
                class_="alert alert-success",
                style_="text-align: left; margin-top: 0.5rem;",
            ),
        )

    @reactive.Effect
    @reactive.event(input.submit_model_training)
    def on_submit_model_training():
        # Check if a Label Studio project was selected or a file was uploaded
        labelstudio_project_id = None
        zip_file_path = None

        try:
            source = input.model_training_labelstudio_project()
            if source == "__labelstudio__":
                saved_project = CONFIG.get("LABELSTUDIO_PROJECT", "").strip()
                if saved_project:
                    try:
                        labelstudio_project_id = int(saved_project)
                    except (ValueError, TypeError):
                        labelstudio_project_id = None
        except Exception:
            labelstudio_project_id = None

        if labelstudio_project_id:
            # User selected a Label Studio project
            logging.info(
                f"[MODEL_TRAINING] User selected Label Studio project {labelstudio_project_id}"
            )

            # Export the project as YOLO format
            try:
                with ui.Progress(min=1, max=3) as p:
                    p.set(message=_("Exporting Label Studio project..."), value=1)

                    # Create a temporary file for the export
                    temp_dir = tempfile.gettempdir()
                    zip_file_path = os.path.join(
                        temp_dir,
                        f"labelstudio_project_{labelstudio_project_id}_{int(tm.time())}.zip",
                    )

                    p.set(message=_("Downloading project data..."), value=2)
                    if not export_labelstudio_project_as_zip(
                        labelstudio_project_id, zip_file_path
                    ):
                        ui.notification_show(
                            _(
                                "Failed to export Label Studio project. Please ensure the project is properly configured and exported as 'YOLO with Images'."
                            ),
                            duration=15,
                            type="error",
                        )
                        # Clean up temp file if it was created
                        try:
                            if os.path.exists(zip_file_path):
                                os.remove(zip_file_path)
                        except Exception:
                            pass
                        return

                    p.set(message=_("Project exported successfully"), value=3)
                    logging.info(
                        f"[MODEL_TRAINING] Successfully exported Label Studio project to {zip_file_path}"
                    )
            except Exception as e:
                logging.error(
                    f"[MODEL_TRAINING] Error exporting Label Studio project: {e}"
                )
                ui.notification_show(
                    _("Error exporting Label Studio project: {}").format(str(e)),
                    duration=15,
                    type="error",
                )
                return
        else:
            # User uploaded a file
            if input.model_training_data() is None:
                ui.notification_show(
                    _(
                        "Please either select a Label Studio project or upload a ZIP file with the training data."
                    ),
                    duration=10,
                    type="error",
                )
                return

                # Check if the file is a ZIP file
            if not input.model_training_data()[0]["name"].endswith(".zip"):
                ui.notification_show(
                    _(
                        "The uploaded file is not a ZIP file. Please upload a valid ZIP file."
                    ),
                    duration=10,
                    type="error",
                )
                return

                # Get the uploaded file path
            zip_file_path = input.model_training_data()[0]["datapath"]
            logging.info(f"[MODEL_TRAINING] User uploaded ZIP file: {zip_file_path}")

        model_name = input.model_name()
        email_notification = input.email_notification()
        user_name = input.user_name()

        # Advanced model parameters are only available in remote-mode.
        # In target-mode, the select inputs are not rendered in the DOM,
        # so accessing them would raise a SilentException.
        if is_remote_mode():
            model_variant = (
                str(input.model_training_base_model() or "n").strip().lower()
            )
            image_size_raw = str(input.model_training_image_size() or "320").strip()
        else:
            model_variant = "n"
            image_size_raw = "320"

        if model_variant not in {"n", "s", "m", "l", "x"}:
            ui.notification_show(
                _("Invalid YOLOv8 model selection."), duration=10, type="error"
            )
            return

        try:
            image_size_candidate = int(image_size_raw)
        except Exception:
            ui.notification_show(
                _("Invalid image size selection."), duration=10, type="error"
            )
            return

        supported_sizes = set(YoloModel.get_supported_image_sizes())
        if image_size_candidate not in supported_sizes:
            ui.notification_show(
                _("Invalid image size selection."), duration=10, type="error"
            )
            return

        image_size = int(image_size_candidate)

        if email_notification:
            # Validate the email address
            if not re.match(r"[^@]+@[^@]+\.[^@]+", email_notification):
                ui.notification_show(
                    _("Please enter a valid email address."), duration=10, type="error"
                )
                return
            if email_notification != CONFIG["EMAIL"]:
                # Update the email address in the config
                CONFIG["EMAIL"] = email_notification
                update_single_config_parameter("EMAIL")

        if user_name and (user_name != CONFIG["USER_NAME"]):
            CONFIG["USER_NAME"] = user_name
            update_single_config_parameter("USER_NAME")

        logging.info(
            f"Enqueued model training: Model Name: '{model_name}', Email: '{email_notification}', "
            f"ZIP file: '{zip_file_path}', YOLOv8 variant: '{model_variant}', image size: {image_size}"
        )

        # Start the model training process
        result = RemoteModelTrainer.enqueue_model_training(
            zip_file_path,
            model_name,
            user_name,
            email_notification,
            yolo_model_variant=model_variant,
            image_size=image_size,
        )
        if is_valid_uuid4(result):
            # Update the config with the training details
            CONFIG["MODEL_TRAINING"] = result
            update_single_config_parameter("MODEL_TRAINING")
        elif result == "client_outdated":
            ui.notification_show(
                _(
                    "This Kittyhack version can no longer submit training jobs. Please update Kittyhack."
                ),
                duration=15,
                type="error",
            )
            return
        elif result == "invalid_file":
            ui.notification_show(
                _(
                    "The uploaded file is not a valid Label Studio training data ZIP file."
                ),
                duration=15,
                type="error",
            )
            return
        elif result in ["destination_unreachable", "destination_not_found"]:
            ui.notification_show(
                _(
                    "The destination for the model training is unreachable. Please check your network connection or try again later."
                ),
                duration=15,
                type="error",
            )
            return
        else:
            ui.notification_show(
                _("An error occurred while starting the model training: {}").format(
                    result
                ),
                duration=15,
                type="error",
            )
            return

        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

        # --- Label Studio installation and update ---

    @reactive.Effect
    @reactive.event(input.btn_labelstudio_install)
    def btn_labelstudio_install():
        with ui.Progress(min=1, max=5) as p:
            # Check available disk space
            if SystemInfo.get_free_disk_space() < 1500:
                ui.notification_show(
                    _(
                        "Insufficient disk space. At least 1.5GB of free space is required to install Label Studio. Please free up some space and try again."
                    ),
                    duration=15,
                    type="error",
                )
                return

            def update_progress(step, message, detail):
                p.set(step, message=message, detail=detail)

                # Start the installation with progress updates

            success = LabelStudioInstall.install_labelstudio(
                progress_callback=update_progress
            )

            if success:
                ui.notification_show(
                    _("Label Studio installed successfully! You can start it now."),
                    duration=15,
                    type="message",
                )
                CONFIG["LABELSTUDIO_VERSION"] = (
                    LabelStudioInstall.get_labelstudio_installed_version()
                )
            else:
                ui.notification_show(
                    _(
                        "Label Studio installation failed. Please check the logs for details."
                    ),
                    duration=None,
                    type="error",
                )

        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    @reactive.Effect
    @reactive.event(input.btn_labelstudio_update)
    def btn_labelstudio_update():
        with ui.Progress(min=1, max=2) as p:

            def update_progress(step, message, detail):
                p.set(step, message=message, detail=detail)

                # Start the installation with progress updates

            success = LabelStudioInstall.update_labelstudio(
                progress_callback=update_progress
            )

            if success:
                ui.notification_show(
                    _("Label Studio updated successfully! You can start it now."),
                    duration=15,
                    type="message",
                )
                CONFIG["LABELSTUDIO_VERSION"] = (
                    LabelStudioInstall.get_labelstudio_installed_version()
                )
            else:
                ui.notification_show(
                    _("Label Studio update failed. Please check the logs for details."),
                    duration=None,
                    type="error",
                )

        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    @reactive.Effect
    @reactive.event(input.btn_labelstudio_remove)
    def btn_labelstudio_remove():
        m = ui.modal(
            title=_("Remove Label Studio"),
            easy_close=True,
            footer=ui.div(
                ui.input_task_button(
                    "btn_labelstudio_remove_ok", _("OK"), class_="btn-default"
                ),
                ui.input_action_button("btn_modal_cancel", _("Cancel")),
            ),
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_labelstudio_remove_ok)
    def btn_labelstudio_remove_ok():
        with ui.Progress(min=1, max=2) as p:
            p.set(1, message=_("Removing Label Studio"), detail=_("Please wait..."))
            success = LabelStudioInstall.remove_labelstudio()
            p.set(2)

            if success:
                ui.notification_show(
                    _("Label Studio removed successfully."), duration=5, type="message"
                )
                CONFIG["LABELSTUDIO_VERSION"] = None
            else:
                ui.notification_show(
                    _(
                        "Label Studio removal failed. Please check the logs for details."
                    ),
                    duration=None,
                    type="error",
                )
        ui.modal_remove()

        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    @reactive.Effect
    @reactive.event(input.btn_labelstudio_stop)
    def stop_labelstudio():
        ServiceOps.systemctl("stop", "labelstudio")
        ServiceOps.systemctl("disable", "labelstudio")

        # Wait up to 10 seconds for the process to stop
        max_wait_time = 10  # seconds
        start_time = monotonic_time()

        with ui.Progress(min=0, max=100) as p:
            p.set(message=_("Stopping Label Studio..."), detail=_("Please wait..."))

            while monotonic_time() - start_time < max_wait_time:
                if not LabelStudioInstall.get_labelstudio_status():
                    ui.notification_show(
                        _("Label Studio stopped successfully."),
                        duration=5,
                        type="message",
                    )
                    reload_trigger_ai.set(reload_trigger_ai.get() + 1)
                    return

                    # Update progress
                progress_percent = int(
                    ((monotonic_time() - start_time) / max_wait_time) * 100
                )
                p.set(progress_percent)
                tm.sleep(0.5)

                # If we get here, the process didn't stop within the timeout period
        ui.notification_show(
            _("Label Studio may not have stopped completely. Please check the logs."),
            duration=5,
            type="warning",
        )
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    @reactive.Effect
    @reactive.event(input.btn_labelstudio_start)
    def start_labelstudio():
        ServiceOps.systemctl("enable", "labelstudio")
        ServiceOps.systemctl("start", "labelstudio")

        # Wait up to 300 seconds for the process to start
        # The first start may take a lot longer, so we set a higher timeout
        max_wait_time = 300  # seconds
        start_time = monotonic_time()

        with ui.Progress(min=0, max=100) as p:
            p.set(
                message=_("Starting Label Studio..."),
                detail=_("Please wait...")
                + " "
                + _("(The first start of Label Studio may take several minutes!)"),
            )

            while monotonic_time() - start_time < max_wait_time:
                # Check if service is running and the web server is responding on port 8080
                if LabelStudioInstall.get_labelstudio_status():
                    if SystemInfo.is_port_open(8080):
                        ui.notification_show(
                            _("Label Studio started successfully."),
                            duration=5,
                            type="message",
                        )
                        reload_trigger_ai.set(reload_trigger_ai.get() + 1)
                        return

                        # Update progress
                progress_percent = int(
                    ((monotonic_time() - start_time) / max_wait_time) * 100
                )
                p.set(progress_percent)
                tm.sleep(0.5)

                # If we get here, the process didn't start within the timeout period
        ui.notification_show(
            _("Label Studio may not have started completely. Please check the logs."),
            duration=5,
            type="warning",
        )
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    @reactive.Effect
    @reactive.event(input.btn_save_labelstudio_token)
    def save_labelstudio_token():
        """Save the Label Studio API token to config.ini and reload the AI Training tab."""
        try:
            token = (input.labelstudio_api_token() or "").strip()
            CONFIG["LABELSTUDIO_API_TOKEN"] = token
            update_single_config_parameter("LABELSTUDIO_API_TOKEN")

            if token:
                ui.notification_show(
                    _("Label Studio API token saved successfully. Reloading…"),
                    duration=3,
                    type="message",
                )
            else:
                ui.notification_show(
                    _("Label Studio API token cleared. Reloading…"),
                    duration=3,
                    type="message",
                )

                # Trigger a reload of the AI Training tab so the project list is
                # re-fetched with the (possibly new) token.
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)
        except Exception as e:
            logging.error(f"[SERVER] Failed to save Label Studio API token: {e}")
            ui.notification_show(
                _("Failed to save token. Please check the logs."),
                duration=None,
                type="error",
            )

    @output
    @render.ui
    @reactive.event(reload_trigger_ai, ignore_none=True)
    async def ui_labelstudio_project_selector():
        """Render the Label Studio project dropdown with async fetching and spinner."""
        token = CONFIG.get("LABELSTUDIO_API_TOKEN", "").strip()
        if not token:
            return ui.div(
                ui.help_text(
                    _("Please configure an API token above to select a project.")
                ),
                style_="margin-top: 0.5rem;",
            )

        if not LabelStudioInstall.get_labelstudio_status():
            return ui.div(
                ui.help_text(
                    _(
                        "Label Studio is not running. Please start it to select a project."
                    )
                ),
                style_="margin-top: 0.5rem;",
            )

            # Fetch projects (may take a few seconds – Shiny shows a spinner automatically for async outputs)
        projects = await asyncio.to_thread(get_labelstudio_projects_list, token=token)

        if projects is None:
            return ui.div(
                ui.div(
                    ui.markdown(
                        f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                        + _("Could not fetch projects. Please check your API token.")
                    ),
                    class_="alert alert-warning",
                    style_="text-align: left; margin-top: 0.5rem;",
                ),
            )

        if not projects:
            return ui.div(
                ui.help_text(_("No projects found in Label Studio.")),
                style_="margin-top: 0.5rem;",
            )

        project_choices = {"": _("— Select a project —")}
        for p in projects:
            project_choices[str(p["id"])] = p.get("title", f"Project {p['id']}")

            # Store the id->title map so the save handler can look up names
        ls_project_choices.set({k: v for k, v in project_choices.items() if k})

        saved_project = CONFIG.get("LABELSTUDIO_PROJECT", "").strip()
        selected = saved_project if saved_project in project_choices else ""

        return ui.div(
            ui.input_select(
                "labelstudio_project_select",
                _("Select a Label Studio project"),
                project_choices,
                selected=selected,
                width="90%",
            ),
            style_="max-width: 600px; margin: 0 auto; margin-top: 0.5rem;",
        )

    @reactive.Effect
    @reactive.event(input.labelstudio_project_select)
    def save_labelstudio_project():
        """Persist the selected Label Studio project to config.ini whenever the dropdown changes."""
        try:
            selected = (input.labelstudio_project_select() or "").strip()
            if selected == CONFIG.get("LABELSTUDIO_PROJECT", ""):
                return  # no change

            CONFIG["LABELSTUDIO_PROJECT"] = selected
            update_single_config_parameter("LABELSTUDIO_PROJECT")

            # Also persist the human-readable project title
            choices = ls_project_choices.get()
            title = choices.get(selected, "") if selected else ""
            CONFIG["LABELSTUDIO_PROJECT_TITLE"] = title
            update_single_config_parameter("LABELSTUDIO_PROJECT_TITLE")

            if selected:
                ui.notification_show(
                    _("Label Studio project saved."), duration=2, type="message"
                )

            reload_trigger_ai.set(reload_trigger_ai.get() + 1)
        except Exception as e:
            logging.error(f"[SERVER] Failed to save Label Studio project: {e}")
            ui.notification_show(
                _("Failed to save project selection. Please check the logs."),
                duration=None,
                type="error",
            )
