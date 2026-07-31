"""Configuration tab and save handler."""

import os
from shiny import render, ui, reactive
import logging
from faicons import icon_svg
import re
from src.baseconfig import (
    CONFIG,
    AllowedToEnter,
    set_language,
    save_config,
    configure_logging,
    DEFAULT_CONFIG,
)
from src.helper import Versioning
from src.system import (
    DependencyInstaller,
    KittyhackUpdater,
)
from src.paths import kittyhack_root
from src.mode import is_remote_mode
from src.model import YoloModel
from src.backend import (
    restart_mqtt,
    update_mqtt_config,
    update_mqtt_language,
    reload_model_handler_runtime,
)
import src.startup as startup
from src.server_ui.state import (
    reload_trigger_config,
    live_view_refresh_nonce,
    live_view_aspect,
)
from src.server_ui.helpers import _disable_numeric_input, collapsible_section
from src.server_ui.context import SessionContext

_ = set_language(CONFIG["LANGUAGE"])

if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir


def register_configuration(input, output, session, ctx: SessionContext):
    """Register Configuration tab UI and save handlers."""

    @output
    @render.ui
    @reactive.event(reload_trigger_config, ignore_none=True)
    def ui_configuration():
        # Helper for collapsible info blocks (local to this tab)
        def info_toggle(id_suffix: str, button_label: str, body_md: str):
            # body_md should already be translated before passing in
            return ui.HTML(f"""
            <div class="kh-info-toggle" id="toggle_{id_suffix}">
            <button type="button"
                    class="btn btn-link p-0 info-toggle-btn"
                    data-bs-toggle="collapse"
                    data-bs-target="#toggle_{id_suffix}_body"
                    aria-expanded="false"
                    style="text-decoration:none;">
                <span class="toggle-chevron" style="display:inline-block; transition:transform .2s;">&#9654;</span>
                {button_label}
            </button>
            <div id="toggle_{id_suffix}_body" class="collapse info-toggle-body" style="margin-top:6px;">
                <div class="kh-info-box">
                {ui.markdown(body_md)}
                </div>
            </div>
            </div>
            """)

            # Build a dictionary with TFLite model versions from the tflite folder

        tflite_models = {}
        try:
            for folder in os.listdir("./tflite"):
                if os.path.isdir(os.path.join("./tflite", folder)):
                    tflite_models[folder] = folder.replace("_", " ").title()
        except Exception as e:
            logging.error(f"Failed to read TFLite model versions: {e}")

            # Create a dict from the yolo_models list, where the key is the "unique_id" and the value is the "full_display_name"
        yolo_models = {
            model["unique_id"]: model["full_display_name"]
            for model in YoloModel.get_model_list()
        }

        # Combine the tflite and yolo models into one list, so that the user can select between both models in the dropdown.
        # Prefix tflite model keys to avoid collision with yolo model unique_ids
        combined_models = {}
        for key, value in tflite_models.items():
            combined_models[f"tflite::{key}"] = value
        if tflite_models and yolo_models:
            # Insert a separator if both types exist
            combined_models["__separator__"] = "────────────"
        for unique_id, display_name in yolo_models.items():
            combined_models[f"yolo::{unique_id}"] = display_name

        hostname = KittyhackUpdater.get_hostname()

        lang = CONFIG.get("LANGUAGE", "en")
        if lang not in ("en", "de"):
            lang = "en"

        def logic_svg(name: str) -> str:
            return f"logic/{name}_{lang}.svg"

        remote_mode_doc_name = "remote-mode_de.md" if lang == "de" else "remote-mode.md"
        remote_mode_doc_path = os.path.join(
            kittyhack_root(), "doc", remote_mode_doc_name
        )
        try:
            with open(remote_mode_doc_path, "r", encoding="utf-8") as f:
                remote_mode_doc_markdown = f.read()
        except Exception as e:
            logging.warning(
                f"[CONFIG] Failed to load remote-mode documentation '{remote_mode_doc_path}': {e}"
            )
            remote_mode_doc_markdown = _(
                "The remote-mode documentation could not be loaded."
            )

        ui_config = ui.div(
            ui.div(
                # --- General settings ---
                ui.br(),
                collapsible_section(
                    "general_settings",
                    _("General settings"),
                    _("Basic language, timezone, and display options for Kittyhack."),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(
                                6,
                                ui.input_select(
                                    "txtLanguage",
                                    _("Language"),
                                    {"en": "English", "de": "Deutsch"},
                                    selected=CONFIG["LANGUAGE"],
                                ),
                            ),
                            ui.column(
                                6,
                                ui.input_text(
                                    "txtConfigTimezone",
                                    _("Timezone"),
                                    CONFIG["TIMEZONE"],
                                ),
                            ),
                            ui.column(6, ui.markdown("")),
                            ui.column(
                                6,
                                ui.HTML(
                                    '<span class="help-block">'
                                    + _("See")
                                    + ' <a href="https://en.wikipedia.org/wiki/List_of_tz_database_time_zones" target="_blank">Wikipedia</a> '
                                    + _("for valid timezone strings")
                                    + "</span>"
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                6,
                                ui.input_text(
                                    "txtConfigDateformat",
                                    _("Date format"),
                                    CONFIG["DATE_FORMAT"],
                                ),
                            ),
                            ui.column(
                                6,
                                ui.markdown(
                                    _("Valid placeholders: `yyyy`, `mm`, `dd`")
                                    + "\n\n"
                                    + _("Example:")
                                    + "\n"
                                    + "- `yyyy-mm-dd` "
                                    + _("for")
                                    + " `2025-02-28`\n"
                                    + "- `dd.mm.yyyy` "
                                    + _("for")
                                    + " `28.02.2025`"
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_switch(
                                    "btnPeriodicVersionCheck",
                                    _("Periodic version check"),
                                    CONFIG["PERIODIC_VERSION_CHECK"],
                                ),
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    _(
                                        "Automatically check for new versions of Kittyhack."
                                    )
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                6,
                                ui.input_select(
                                    "update_repository_mode",
                                    _("Update repository"),
                                    {"standard": _("Standard"), "custom": _("Custom")},
                                    selected=CONFIG.get(
                                        "UPDATE_REPOSITORY_MODE", "standard"
                                    ),
                                ),
                            ),
                            ui.column(
                                6,
                                ui.input_text(
                                    "update_repository",
                                    _("Custom repository"),
                                    value=CONFIG.get("UPDATE_REPOSITORY", ""),
                                    placeholder=_(
                                        "e.g. owner/repo@branch or owner:branch"
                                    ),
                                    width="100%",
                                ),
                                id="update_repository_container",
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    _(
                                        "Use **Standard** for official releases from `floppyFK/kittyhack`. "
                                        "Select **Custom** to test your own fork or a feature branch. "
                                        "Accepted formats:\n\n"
                                        "- `owner/repo` — latest release tag from that fork\n"
                                        "- `owner/repo@branch-or-tag` — track the specified ref\n"
                                        "- `owner:branch` — GitHub PR head-ref shorthand; "
                                        "you can copy it straight from a pull request header, "
                                        "the repo name defaults to `kittyhack`"
                                    )
                                ),
                                style_="color: grey;",
                                id="update_repository_help",
                            ),
                        ),
                        ui.hr(),
                        (
                            ui.row(
                                ui.column(
                                    12,
                                    ui.markdown(
                                        _(
                                            "WLAN settings are not available in remote-mode."
                                        )
                                    ),
                                    style_="color: grey;",
                                )
                            )
                            if is_remote_mode()
                            else ui.row(
                                ui.column(
                                    4,
                                    ui.input_slider(
                                        "sldWlanTxPower",
                                        _("WLAN TX power (in dBm)"),
                                        min=0,
                                        max=20,
                                        value=CONFIG["WLAN_TX_POWER"],
                                        step=1,
                                    ),
                                ),
                                ui.column(
                                    8,
                                    ui.markdown(
                                        _(
                                            "WARNING: You should keep the TX power as low as possible to avoid interference with the PIR Sensors! You should only increase this value, if you have problems with the WLAN connection."
                                        )
                                        + "\n\n"
                                        + "*("
                                        + _("Default value: {}").format(
                                            DEFAULT_CONFIG["Settings"]["wlan_tx_power"]
                                        )
                                        + ")*"
                                    ),
                                    style_="color: grey;",
                                ),
                            )
                        ),
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),
                # --- Camera settings ---
                collapsible_section(
                    "camera_settings",
                    _("Camera settings"),
                    _(
                        "Camera specific settings for the internal and external cameras."
                    ),
                    ui.div(
                        ui.column(
                            12,
                            info_toggle(
                                "camera_intro",
                                _("Show IP camera usage tips"),
                                _(
                                    "You can use an **external IP camera** instead of the internal camera to achieve better viewing angles and better night vision.\n\n"
                                    "**Important notes:**\n"
                                    "- Both Kittyflap and the IP camera need a stable WLAN (Ethernet recommended for camera).\n"
                                    "- Use a **static IP** for the camera.\n"
                                    "- Ensure a compatible stream (RTSP / HTTP MJPEG / RTMP / UDP / TCP).\n"
                                    "- Recommended max resolution 1280x720 @ ≤15fps (higher reduces performance).\n"
                                    "- Configure resolution in camera settings or use alternate stream URLs.\n\n"
                                    "If you experience interruptions, check WLAN signal and network configuration."
                                ),
                            ),
                        ),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_select(
                                    "camera_source",
                                    _("Camera source"),
                                    {
                                        "internal": _("Internal Camera"),
                                        "ip_camera": _("External IP Camera"),
                                    },
                                    selected=CONFIG["CAMERA_SOURCE"],
                                ),
                            ),
                            ui.column(
                                12,
                                ui.input_text(
                                    "ip_camera_url",
                                    _("External IP Camera URL"),
                                    value=CONFIG["IP_CAMERA_URL"],
                                    placeholder=_(
                                        "e.g. rtsp://user:pass@192.168.1.100:554/stream"
                                    ),
                                    width="100%",
                                ),
                                id="ip_camera_url_container",
                            ),
                        ),
                        ui.column(
                            12,
                            info_toggle(
                                "ip_camera_examples",
                                _("Show stream URL examples"),
                                _("**Examples of supported stream URLs:**")
                                + "\n"
                                + "- `rtsp://user:pass@192.168.1.100:554/stream1`  _(RTSP)_"
                                + "\n"
                                + "- `http://192.168.1.101:8080/video`  _(HTTP MJPEG)_"
                                + "\n"
                                + "- `rtmp://192.168.1.102/live/stream`  _(RTMP)_"
                                + "\n"
                                + "- `udp://@239.0.0.1:1234`  _(UDP multicast)_"
                                + "\n"
                                + "- `tcp://192.168.1.103:8554`  _(TCP stream)_",
                            ),
                            id="ip_camera_warning",
                        ),
                        ui.hr(),
                        ui.div(
                            ui.tags.button(
                                ui.tags.span(
                                    "\u25b6",
                                    class_="toggle-chevron",
                                    style_="display:inline-block; transition:transform .2s;",
                                ),
                                " ",
                                _("Advanced IP camera settings"),
                                type="button",
                                class_="btn btn-link p-0 info-toggle-btn",
                                style_="text-decoration:none;",
                                **{
                                    "data-bs-toggle": "collapse",
                                    "data-bs-target": "#ip_camera_pipeline_settings_body",
                                    "aria-expanded": "false",
                                    "aria-controls": "ip_camera_pipeline_settings_body",
                                },
                            ),
                            ui.div(
                                ui.div(
                                    ui.br(),
                                    ui.row(
                                        ui.column(
                                            12,
                                            ui.input_switch(
                                                "btnEnableIpCameraDecodeScalePipeline",
                                                _("Downscale IP camera stream"),
                                                CONFIG.get(
                                                    "ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE",
                                                    False,
                                                ),
                                            ),
                                        ),
                                        ui.column(
                                            12,
                                            ui.markdown(
                                                _(
                                                    "If enabled, the IP camera stream is downscaled before frames reach Kittyhack."
                                                )
                                                + "  \n"
                                                + _(
                                                    "This can reduce CPU load and improve inference FPS for high-resolution streams."
                                                )
                                            ),
                                            style_="color: grey;",
                                        ),
                                    ),
                                    ui.row(
                                        ui.column(
                                            12,
                                            ui.input_select(
                                                "ip_camera_target_resolution",
                                                _(
                                                    "Target resolution for IP camera stream"
                                                ),
                                                {
                                                    "480x320": "320p (480x320)",
                                                    "640x360": "360p (640x360)",
                                                    "640x480": "480p 4:3 (640x480)",
                                                    "854x480": "480p 16:9 (854x480)",
                                                    "960x540": "540p (960x540)",
                                                    "1024x576": "576p (1024x576)",
                                                    "1280x720": "720p (1280x720)",
                                                },
                                                selected=CONFIG.get(
                                                    "IP_CAMERA_TARGET_RESOLUTION",
                                                    "640x360",
                                                ),
                                                width="100%",
                                            ),
                                        ),
                                        ui.column(
                                            12,
                                            ui.markdown(
                                                _(
                                                    "Recommendation: choose `640x360` or `1280x720` for best performance/quality tradeoff."
                                                )
                                            ),
                                            style_="color: grey;",
                                        ),
                                    ),
                                    ui.row(
                                        ui.column(
                                            12,
                                            ui.input_select(
                                                "ip_camera_pipeline_fps_limit",
                                                _("IP camera FPS limit"),
                                                {
                                                    "5": "5 FPS",
                                                    "10": "10 FPS",
                                                    "15": "15 FPS",
                                                    "20": "20 FPS",
                                                    "25": "25 FPS",
                                                    "0": _("Unlimited"),
                                                },
                                                selected=str(
                                                    CONFIG.get(
                                                        "IP_CAMERA_PIPELINE_FPS_LIMIT",
                                                        10,
                                                    )
                                                ),
                                                width="100%",
                                            ),
                                        ),
                                        ui.column(
                                            12,
                                            ui.markdown(
                                                _(
                                                    "Default is `10 FPS`. Set to `Unlimited` to disable FPS limiting in the downscale pipeline (may cause higher CPU load)."
                                                )
                                            ),
                                            style_="color: grey;",
                                        ),
                                    ),
                                    (
                                        ui.row(
                                            ui.column(
                                                12,
                                                ui.input_select(
                                                    "ip_camera_hw_decode",
                                                    _("IP camera hardware decode"),
                                                    {
                                                        "auto": _("Auto (detect GPU)"),
                                                        "none": _("Software only"),
                                                        "cuda": _("NVIDIA CUDA"),
                                                        "vaapi": _("VAAPI (Intel/AMD)"),
                                                        "qsv": _("Intel Quick Sync"),
                                                    },
                                                    selected=str(
                                                        CONFIG.get(
                                                            "IP_CAMERA_HW_DECODE",
                                                            "auto",
                                                        )
                                                        or "auto"
                                                    ),
                                                    width="100%",
                                                ),
                                            ),
                                            ui.column(
                                                12,
                                                ui.markdown(
                                                    _(
                                                        "Hardware decode offloads H.264/H.265 decoding from the CPU when using the downscale pipeline. "
                                                        "Requires FFmpeg with the matching hardware support. Falls back to software decode automatically."
                                                    )
                                                ),
                                                style_="color: grey;",
                                            ),
                                        )
                                        if is_remote_mode()
                                        else ui.HTML("")
                                    ),
                                ),
                                id="ip_camera_pipeline_settings_body",
                                class_="collapse info-toggle-body",
                                style_="margin-top:6px;",
                            ),
                            id="ip_camera_pipeline_settings",
                            class_="kh-info-toggle",
                            style_="margin-top: 0.75rem;",
                        ),
                        ui.br(),
                        full_screen=False,
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),
                # --- Door control settings ---
                collapsible_section(
                    "door_control_settings",
                    _("Door control settings"),
                    _(
                        "Detection thresholds, model selection and other settings for the door control."
                    ),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_slider(
                                    "sldMinThreshold",
                                    _("Minimum detection threshold"),
                                    min=0,
                                    max=80,
                                    width="90%",
                                    value=CONFIG["MIN_THRESHOLD"],
                                ),
                            ),
                            ui.column(
                                12,
                                info_toggle(
                                    "min_threshold_info",
                                    _("Explain minimum detection threshold"),
                                    _(
                                        "Used to decide if an outside motion event is logged. "
                                        "At least one picture must exceed this probability or the "
                                        "event is discarded.\n*(Default value: {})*"
                                    ).format(
                                        DEFAULT_CONFIG["Settings"]["min_threshold"]
                                    ),
                                ),
                            ),
                        ),
                        ui.br(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_slider(
                                    "sldMouseThreshold",
                                    _("Mouse detection threshold"),
                                    min=0,
                                    max=100,
                                    width="90%",
                                    value=CONFIG["MOUSE_THRESHOLD"],
                                ),
                            ),
                            ui.column(
                                12,
                                info_toggle(
                                    "mouse_threshold_info",
                                    _("Explain mouse threshold"),
                                    _(
                                        "Kittyhack decides if a picture contains a mouse based on this value. "
                                        "If probability exceeds it, flap stays closed.\n"
                                        "*(Default value: {})*\n\n"
                                        "**Note:** Minimum is coupled to 'Minimum detection threshold'."
                                    ).format(
                                        DEFAULT_CONFIG["Settings"]["mouse_threshold"]
                                    ),
                                ),
                            ),
                        ),
                        ui.br(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_slider(
                                    "sldCatThreshold",
                                    _("Cat detection threshold"),
                                    min=0,
                                    max=100,
                                    width="90%",
                                    value=CONFIG["CAT_THRESHOLD"],
                                ),
                            ),
                            ui.column(
                                12,
                                info_toggle(
                                    "cat_threshold_info",
                                    _("Explain cat threshold"),
                                    _(
                                        "Kittyhack decides based on this value, if a picture contains your cat."
                                    )
                                    + "  \n"
                                    + _(
                                        "If the detected probability of your cat exceeds this value in a picture, and the setting `Use camera for cat detection` is enabled, the flap will be opened."
                                    )
                                    + "  \n"
                                    + "*("
                                    + _("Default value: {}").format(
                                        DEFAULT_CONFIG["Settings"]["cat_threshold"]
                                    )
                                    + ")*"
                                    + "  \n\n"
                                    + "**"
                                    + _(
                                        "Note: The minimum of this value is always coupled to the configured value of 'Minimum detection threshold' above."
                                    )
                                    + "**",
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_numeric(
                                    "numMinSecondsToAnalyze",
                                    _("Seconds before unlock decision"),
                                    float(
                                        CONFIG.get("MIN_SECONDS_TO_ANALYZE", 1.5) or 1.5
                                    ),
                                    min=0.1,
                                    step=0.1,
                                ),
                            ),
                            ui.column(
                                12,
                                info_toggle(
                                    "min_seconds_info",
                                    _("Explain unlock decision delay"),
                                    _(
                                        "Time in seconds after an outside motion trigger before Kittyhack may unlock."
                                    )
                                    + "  \n"
                                    + _(
                                        "Unlock is only possible if no prey was detected during this initial analysis window."
                                    )
                                    + "  \n\n"
                                    + _(
                                        "After unlocking: If a later picture reaches the mouse_threshold, the flap is locked again."
                                    )
                                    + "  \n\n"
                                    + "*("
                                    + _("Default value: {}").format(
                                        DEFAULT_CONFIG["Settings"][
                                            "min_seconds_to_analyze"
                                        ]
                                    )
                                    + ")*",
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_slider(
                                    "sldLockAfterPreyDetect",
                                    _("Lock duration after prey detection (in s)"),
                                    min=30,
                                    max=1800,
                                    step=5,
                                    width="90%",
                                    value=CONFIG["LOCK_DURATION_AFTER_PREY_DETECTION"],
                                ),
                            ),
                            ui.column(
                                12,
                                info_toggle(
                                    "lock_after_prey_info",
                                    _("Explain lock duration"),
                                    _(
                                        "The flap will remain closed for this time after a prey detection."
                                    ),
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_switch(
                                    "btnDetectPrey",
                                    _("Detect prey"),
                                    CONFIG["MOUSE_CHECK_ENABLED"],
                                ),
                            ),
                            ui.column(
                                12,
                                info_toggle(
                                    "detect_prey_info",
                                    _("Explain prey detection"),
                                    _(
                                        "If the prey detection is enabled and the mouse detection threshold "
                                        "is exceeded in a picture, the flap will remain closed.\n\n"
                                        "**NOTE:** This is the global setting. It can also be configured "
                                        "per cat in the `MANAGE CATS` section."
                                    ),
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_select(
                                    "selectedModel",
                                    _("Version of the object detection model"),
                                    combined_models,
                                    selected=(
                                        f"tflite::{CONFIG['TFLITE_MODEL_VERSION']}"
                                        if f"tflite::{CONFIG['TFLITE_MODEL_VERSION']}"
                                        in combined_models
                                        else (
                                            f"yolo::{CONFIG['YOLO_MODEL']}"
                                            if f"yolo::{CONFIG['YOLO_MODEL']}"
                                            in combined_models
                                            else next(iter(combined_models), "")
                                        )
                                    ),
                                    width="90%",
                                ),
                            ),
                            ui.column(
                                12,
                                info_toggle(
                                    "model_versions_info",
                                    _("Show model version explanation"),
                                    "- **"
                                    + _("Original Kittyflap Model v1:")
                                    + "** "
                                    + _(
                                        "Always tries to detect objects `Mouse` and `No Mouse`, even if there is no such object in the picture."
                                    )
                                    + "\n\n"
                                    + "- **"
                                    + _("Original Kittyflap Model v2:")
                                    + "** "
                                    + _(
                                        "Only tries to detect objects `Mouse` and `No Mouse` if there is a cat in the picture."
                                    )
                                    + "\n\n"
                                    + "- **"
                                    + _("Custom Models:")
                                    + "** "
                                    + _(
                                        "These are your own trained models, which you have created in the `AI TRAINING` section."
                                    )
                                    + "\n\n"
                                    + "> "
                                    + _(
                                        "If you change this setting, the Kittyflap must be restarted to apply the new model version."
                                    ),
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_switch(
                                    "btnUseCameraForCatDetection",
                                    _("Use camera for cat detection"),
                                    CONFIG["USE_CAMERA_FOR_CAT_DETECTION"],
                                    width="90%",
                                ),
                            ),
                            ui.column(
                                12,
                                info_toggle(
                                    "camera_cat_detection_info",
                                    _("Explain camera cat detection"),
                                    _(
                                        "If this setting is enabled, the camera will also be used for cat detection (in addition to the RFID reader)."
                                    )
                                    + "  \n\n"
                                    + _(
                                        "You can configure the required threshold for the cat detection with the slider `Cat detection threshold`."
                                    )
                                    + " "
                                    + _(
                                        "If the detection is successful, the inside direction will be opened."
                                    )
                                    + "\n\n"
                                    + _(
                                        "**NOTE:** This feature requires a custom trained model for your cat(s). It does not work with the default kittyflap models."
                                    )
                                    + "\n\n"
                                    + _(
                                        "This feature depends heavily on the quality of your model and sufficient lighting conditions."
                                    )
                                    + " "
                                    + _(
                                        "If one or both are not good, the detection may either fail or other - similiar looking cats may be detected as your cat."
                                    ),
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_switch(
                                    "btnUseCameraForMotionDetection",
                                    _("Use camera for motion detection"),
                                    CONFIG["USE_CAMERA_FOR_MOTION_DETECTION"],
                                    width="90%",
                                ),
                            ),
                            ui.column(
                                12,
                                info_toggle(
                                    "camera_motion_detection_info",
                                    _("Explain camera motion detection"),
                                    _(
                                        "Disables the outside PIR sensor and uses the camera for motion detection instead."
                                    )
                                    + "  \n\n"
                                    + _("**How it works:**")
                                    + "  \n"
                                    + _(
                                        "- In regular operation, Kittyhack waits for a trigger from the outside PIR sensor before starting camera analysis"
                                    )
                                    + "  \n"
                                    + _(
                                        "- With this feature enabled, the PIR sensor is disabled and the camera continuously analyzes images"
                                    )
                                    + "  \n"
                                    + _(
                                        "- When a cat is detected in the camera feed, it's treated as equivalent to a motion detection outside"
                                    )
                                    + "  \n\n"
                                    + _(
                                        "You can configure the required threshold for the cat detection with the slider `Cat detection threshold`."
                                    )
                                    + "  \n\n"
                                    + _(
                                        "This may be very helpful in areas where environmental factors (moving trees, people passing by) permanently cause false PIR triggers."
                                    )
                                    + "  \n\n"
                                    + _(
                                        "**NOTE:** This feature requires a custom trained model for your cat(s). It does not work with the default kittyflap models."
                                    )
                                    + "\n\n"
                                    + _(
                                        "This feature depends heavily on the quality of your model and sufficient lighting conditions."
                                    )
                                    + " "
                                    + _(
                                        "If one or both are not good, you may experience false triggers or your cat may not be detected correctly."
                                    ),
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_select(
                                    "txtAllowedToEnter",
                                    _("Open inside direction for:"),
                                    {
                                        AllowedToEnter.ALL.value: _(
                                            "All cats (unlock on every detected motion)"
                                        ),
                                        AllowedToEnter.ALL_RFIDS.value: _(
                                            "All cats with a RFID chip"
                                        ),
                                        AllowedToEnter.KNOWN.value: _(
                                            "Only registered cats"
                                        ),
                                        AllowedToEnter.NONE.value: _("No cats"),
                                        AllowedToEnter.CONFIGURE_PER_CAT.value: _(
                                            "Individual configuration per cat"
                                        ),
                                    },
                                    selected=str(CONFIG["ALLOWED_TO_ENTER"].value),
                                    width="90%",
                                ),
                            ),
                            ui.column(
                                12,
                                info_toggle(
                                    "allowed_to_enter_info",
                                    _("Explain entrance modes"),
                                    _(
                                        "- **All cats:** *Every* detected motion on the outside will unlock the flap."
                                    )
                                    + "  \n"
                                    + _(
                                        "- **All cats with a RFID chip:** Every successful RFID detection will unlock the flap."
                                    )
                                    + "  \n"
                                    + _(
                                        "- **Only registered cats:** Only the cats that are registered in the database will unlock the flap (either by RFID or by camera detection, if enabled)."
                                    )
                                    + "  \n"
                                    + _(
                                        "- **Individual configuration per cat:** Configure per cat if it is allowed to enter (in the `MANAGE CATS` section)."
                                    )
                                    + "  \n"
                                    + _(
                                        "- **No cats:** The inside direction will never be opened."
                                    ),
                                ),
                            ),
                        ),
                        ui.row(
                            ui.column(
                                12,
                                ui.div(
                                    # Precompute translations to avoid _() inside f-strings
                                    ui.HTML(
                                        (
                                            """
                                        <button id="btn_toggle_entry_logic"
                                                class="btn-default"
                                                style="margin-top:8px;"
                                                data-show-label="{show}"
                                                data-hide-label="{hide}">
                                            <span>{show}</span>
                                        </button>
                                        <div id="entry_logic_hint"
                                             style="display:none; font-size:0.75rem; margin-top:4px; color:#555;">
                                             {hint}
                                        </div>
                                    """
                                        ).format(
                                            show=_("Show decision logic"),
                                            hide=_("Hide decision logic"),
                                            hint=_(
                                                "Only the flowchart for the currently selected mode is shown."
                                            ),
                                        )
                                    ),
                                    ui.HTML(
                                        (
                                            """
                                        <div id="entry_logic_expand" class="logic-section" style="display:none; margin-top:10px;">
                                          <div id="entry_logic_images">
                                            <div class="logic-img-wrapper" data-mode="all">
                                              <img src="{src_all}" alt="Entry logic (ALL)"/>
                                            </div>
                                            <div class="logic-img-wrapper" data-mode="all_rfids">
                                              <img src="{src_all_rfids}" alt="Entry logic (ALL_RFIDS)"/>
                                            </div>
                                            <div class="logic-img-wrapper" data-mode="known">
                                              <img src="{src_known}" alt="Entry logic (KNOWN)"/>
                                            </div>
                                            <div class="logic-img-wrapper" data-mode="none">
                                              <img src="{src_none}" alt="Entry logic (NONE)"/>
                                            </div>
                                            <div class="logic-img-wrapper" data-mode="configure_per_cat">
                                              <img src="{src_cfg}" alt="Entry logic (CONFIGURE_PER_CAT)"/>
                                            </div>
                                          </div>
                                        </div>
                                    """
                                        ).format(
                                            src_all=logic_svg("entry_all"),
                                            src_all_rfids=logic_svg("entry_all_rfids"),
                                            src_known=logic_svg("entry_known"),
                                            src_none=logic_svg("entry_none"),
                                            src_cfg=logic_svg(
                                                "entry_configure_per_cat"
                                            ),
                                        )
                                    ),
                                    class_="d-flex flex-column align-items-center w-100",
                                ),
                            )
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_select(
                                    "btnAllowedToExit",
                                    _("Outside direction:"),
                                    {
                                        "allow": _("Allow exit"),
                                        "deny": _("Do not allow exit"),
                                        "configure_per_cat": _(
                                            "Individual configuration per cat"
                                        ),
                                    },
                                    selected=str(CONFIG["ALLOWED_TO_EXIT"].value),
                                    width="90%",
                                ),
                            ),
                            ui.column(
                                12,
                                info_toggle(
                                    "allowed_to_exit_info",
                                    _("Explain exit modes"),
                                    _(
                                        "- **Allow exit:** The outside direction is always possible. You can also configure time ranges below to restrict the exit times."
                                    )
                                    + "  \n"
                                    + _(
                                        "- **Do not allow exit:** The outside direction is always closed."
                                    )
                                    + "  \n"
                                    + _(
                                        "- **Individual configuration per cat:** Configure per cat if it is allowed to exit (in the `MANAGE CATS` section). The time ranges below are applied in addition."
                                    )
                                    + "  \n  "
                                    + _(
                                        "**NOTE:** All your cats must be registered **with a RFID chip** to use this mode. Cats without RFID can not go outside in this mode!"
                                    ),
                                ),
                            ),
                        ),
                        ui.row(
                            ui.column(
                                12,
                                ui.div(
                                    # Precompute translations to avoid _() inside f-strings
                                    ui.HTML(
                                        (
                                            """
                                        <button id="btn_toggle_exit_logic"
                                                class="btn-default"
                                                style="margin-top:8px;"
                                                data-show-label="{show}"
                                                data-hide-label="{hide}">
                                            <span>{show}</span>
                                        </button>
                                        <div id="exit_logic_hint"
                                             style="display:none; font-size:0.75rem; margin-top:4px; color:#555;">
                                             {hint}
                                        </div>
                                    """
                                        ).format(
                                            show=_("Show decision logic"),
                                            hide=_("Hide decision logic"),
                                            hint=_(
                                                "Only the flowchart for the currently selected mode is shown."
                                            ),
                                        )
                                    ),
                                    ui.HTML(
                                        (
                                            """
                                        <div id="exit_logic_expand" class="logic-section" style="display:none; margin-top:10px;">
                                          <div id="exit_logic_images">
                                            <div class="logic-img-wrapper" data-mode="allow">
                                              <img src="{src_allow}" alt="Exit logic (ALLOW)"/>
                                            </div>
                                            <div class="logic-img-wrapper" data-mode="deny">
                                              <img src="{src_deny}" alt="Exit logic (DENY)"/>
                                            </div>
                                            <div class="logic-img-wrapper" data-mode="configure_per_cat">
                                              <img src="{src_cfg}" alt="Exit logic (CONFIGURE_PER_CAT)"/>
                                            </div>
                                          </div>
                                        </div>
                                    """
                                        ).format(
                                            src_allow=logic_svg("exit_allow"),
                                            src_deny=logic_svg("exit_deny"),
                                            src_cfg=logic_svg("exit_configure_per_cat"),
                                        )
                                    ),
                                    class_="d-flex flex-column align-items-center w-100",
                                    style_="color: grey;",
                                ),
                            ),
                        ),
                        ui.br(),
                        ui.br(),
                        ui.div(
                            ui.row(
                                ui.column(
                                    12,
                                    info_toggle(
                                        "exit_time_ranges",
                                        _("Show exit time range rules"),
                                        _(
                                            "You can specify up to 3 time ranges, in which your cats are allowed to exit."
                                        )
                                        + "  \n\n"
                                        + _("Rules:")
                                        + "  \n"
                                        + "- "
                                        + _(
                                            "The time ranges are global and apply to all cats."
                                        )
                                        + "  \n"
                                        + "- "
                                        + _(
                                            "If no range is enabled, cats may exit at any time (subject to other settings)."
                                        )
                                        + "  \n"
                                        + "- "
                                        + _(
                                            "If any range is enabled, cats may exit only during the configured time windows."
                                        )
                                        + "  \n"
                                        + "- "
                                        + _(
                                            "Outside these windows, no cat may exit, even if the per‑cat setting in 'Manage Cats' allows exit."
                                        )
                                        + "  \n"
                                        + _(
                                            "See the decision logic flowchart above for details."
                                        )
                                        + "  \n\n"
                                        + _(
                                            "Enter times in 24h format HH:MM (e.g., 13:00)."
                                        )
                                        + "  \n"
                                        + _(
                                            "Example: If ranges are 10:00–18:00 and it is 20:00, no cat may exit, even if that cat is allowed per‑cat in 'Manage Cats'."
                                        ),
                                    ),
                                )
                            ),
                            ui.br(),
                            ui.row(
                                ui.column(
                                    12,
                                    ui.input_switch(
                                        "btnAllowedToExitRange1",
                                        "\n" + _("Time range 1"),
                                        CONFIG["ALLOWED_TO_EXIT_RANGE1"],
                                    ),
                                ),
                                ui.column(
                                    4,
                                    ui.input_text(
                                        "txtAllowedToExitRange1From",
                                        label=_("From"),
                                        placeholder="00:00",
                                        value=CONFIG["ALLOWED_TO_EXIT_RANGE1_FROM"],
                                    ),
                                ),
                                ui.column(
                                    4,
                                    ui.input_text(
                                        "txtAllowedToExitRange1To",
                                        label=_("To"),
                                        placeholder="00:00",
                                        value=CONFIG["ALLOWED_TO_EXIT_RANGE1_TO"],
                                    ),
                                ),
                            ),
                            ui.br(),
                            ui.row(
                                ui.column(
                                    12,
                                    ui.input_switch(
                                        "btnAllowedToExitRange2",
                                        _("Time range 2"),
                                        CONFIG["ALLOWED_TO_EXIT_RANGE2"],
                                    ),
                                ),
                                ui.column(
                                    4,
                                    ui.input_text(
                                        "txtAllowedToExitRange2From",
                                        label=_("From"),
                                        placeholder="00:00",
                                        value=CONFIG["ALLOWED_TO_EXIT_RANGE2_FROM"],
                                    ),
                                ),
                                ui.column(
                                    4,
                                    ui.input_text(
                                        "txtAllowedToExitRange2To",
                                        label=_("To"),
                                        placeholder="00:00",
                                        value=CONFIG["ALLOWED_TO_EXIT_RANGE2_TO"],
                                    ),
                                ),
                            ),
                            ui.br(),
                            ui.row(
                                ui.column(
                                    12,
                                    ui.input_switch(
                                        "btnAllowedToExitRange3",
                                        _("Time range 3"),
                                        CONFIG["ALLOWED_TO_EXIT_RANGE3"],
                                    ),
                                ),
                                ui.column(
                                    4,
                                    ui.input_text(
                                        "txtAllowedToExitRange3From",
                                        label=_("From"),
                                        placeholder="00:00",
                                        value=CONFIG["ALLOWED_TO_EXIT_RANGE3_FROM"],
                                    ),
                                ),
                                ui.column(
                                    4,
                                    ui.input_text(
                                        "txtAllowedToExitRange3To",
                                        label=_("To"),
                                        placeholder="00:00",
                                        value=CONFIG["ALLOWED_TO_EXIT_RANGE3_TO"],
                                    ),
                                ),
                            ),
                            id_="allowed_to_exit_ranges",
                        ),
                        ui.hr(),
                        # TODO: Outside PIR shall not yet be configurable. Need to redesign the camera control, otherwise we will have no cat pictures at high PIR thresholds.
                        # ui.column(12, ui.input_slider("sldPirOutsideThreshold", _("Sensitivity of the motion sensor on the outside"), min=0.1, max=6, step=0.1, value=CONFIG['PIR_OUTSIDE_THRESHOLD'])),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_slider(
                                    "sldPirInsideThreshold",
                                    _(
                                        "Reaction speed (in s) of the motion sensor on the inside"
                                    ),
                                    min=0.1,
                                    max=6,
                                    step=0.1,
                                    value=CONFIG["PIR_INSIDE_THRESHOLD"],
                                    width="90%",
                                ),
                            ),
                            ui.column(
                                12,
                                info_toggle(
                                    "pir_inside_threshold_info",
                                    _("Explain inside PIR reaction speed"),
                                    _(
                                        "A low value means a fast reaction, but also a higher probability of false alarms. "
                                        "A high value means a slow reaction, but also a lower probability of false alarms."
                                    )
                                    + "  \n"
                                    + _(
                                        "The default setting should be a good value for most cases."
                                    )
                                    + "  \n"
                                    + "*("
                                    + _("Default value: {}").format(
                                        DEFAULT_CONFIG["Settings"][
                                            "pir_inside_threshold"
                                        ]
                                    )
                                    + ")*",
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_switch(
                                    "btnImmediateLockAfterPassage",
                                    _("Immediate locking after passage"),
                                    CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"],
                                    width="90%",
                                ),
                            ),
                            ui.column(
                                12,
                                info_toggle(
                                    "immediate_lock_after_passage_info",
                                    _("About this feature"),
                                    _(
                                        "When enabled, the flap locks again and the motion event is finalized as soon as motion is detected on the opposite side of the flap (cat has crossed)."
                                    )
                                    + "  \n\n"
                                    + _(
                                        "This feature is useful if your cat is very fast and the flap should not remain open for too long. Will help prevent multiple cats from using the flap in quick succession."
                                    )
                                    + "  \n\n"
                                    + _(
                                        "If your cat is shy or just needs several attempts to cross, you should not enable this feature. Otherwise, the flap could be accidentally locked while your cat is still trying to cross."
                                    ),
                                ),
                            ),
                        ),
                        full_screen=False,
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),
                # --- Live view settings ---
                collapsible_section(
                    "live_view_settings",
                    _("Live view settings"),
                    _("Update interval for the live camera view in the WebUI."),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(
                                4,
                                ui.input_select(
                                    "numLiveViewUpdateInterval",
                                    _("Live-View update interval:"),
                                    {
                                        _("Refresh the live view every..."): {
                                            0.1: "100ms",
                                            0.2: "200ms",
                                            0.5: "500ms",
                                            1.0: "1s",
                                            2.0: "2s",
                                            3.0: "3s",
                                            5.0: "5s",
                                            10.0: "10s",
                                        },
                                    },
                                    selected=CONFIG["LIVE_VIEW_REFRESH_INTERVAL"],
                                ),
                            ),
                            ui.column(
                                8,
                                ui.markdown(
                                    _(
                                        "NOTE: A high refresh rate could slow down the performance, especially if several users are connected at the same time. Values below 1s require a fast and stable WLAN connection."
                                    )
                                    + "  \n"
                                    + _(
                                        "This setting affects only the view in the WebUI and has no impact on the detection process."
                                    )
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        ui.br(),
                        full_screen=False,
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),
                # --- Pictures view settings ---
                collapsible_section(
                    "pictures_view_settings",
                    _("Pictures view settings"),
                    _(
                        "Configuration of the pictures view in the WebUI, maximum number of pictures in the database and limits of pictures per event."
                    ),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(
                                4,
                                ui.input_numeric(
                                    "numMaxPhotosCount",
                                    _(
                                        "Maximum number of photos to retain in the database"
                                    ),
                                    CONFIG["MAX_PHOTOS_COUNT"],
                                    min=100,
                                ),
                            ),
                            ui.column(
                                8,
                                ui.markdown(
                                    _(
                                        "The oldest pictures will be deleted if the number of pictures exceeds this value."
                                    )
                                    + "  \n"
                                    + _(
                                        "The maximum number of pictures depends on the type of the Raspberry Pi, since some kittyflaps are equipped with 16GB and some with 32GB."
                                    )
                                    + "  \n"
                                    + _(
                                        "As a rule of thumb, you can calculate with 200MB per 1000 pictures. You can check the free disk space in the `INFO` section."
                                    )
                                    + "  \n"
                                    + "*("
                                    + _("Default value: {}").format(
                                        DEFAULT_CONFIG["Settings"]["max_photos_count"]
                                    )
                                    + ")*"
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                4,
                                ui.input_numeric(
                                    "numMaxPicturesPerEventWithRfid",
                                    _("Maximal pictures per event with RFID"),
                                    CONFIG["MAX_PICTURES_PER_EVENT_WITH_RFID"],
                                    min=0,
                                ),
                            ),
                            ui.column(
                                8,
                                ui.markdown(
                                    _(
                                        "Maximal number of pictures that will be stored to the database for a motion event, if a cat with a RFID chip is detected."
                                    )
                                    + "  \n"
                                    + _(
                                        "NOTE: The internal prey detection will still be active for all pictures of this event. This number just limits the number of pictures that will be stored to the database."
                                    )
                                    + "  \n"
                                    + _(
                                        "If you set this to 0, no event will be logged to the database. Too many pictures can slow down the performance drastically."
                                    )
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                4,
                                ui.input_numeric(
                                    "numMaxPicturesPerEventWithoutRfid",
                                    _("Maximal pictures per event without RFID"),
                                    CONFIG["MAX_PICTURES_PER_EVENT_WITHOUT_RFID"],
                                    min=0,
                                ),
                            ),
                            ui.column(
                                8,
                                ui.markdown(
                                    _(
                                        "Maximal number of pictures that will be stored to the database for a motion event, if a motion event without a detected RFID occurs."
                                    )
                                    + "  \n"
                                    + _(
                                        "NOTE: The internal prey detection will still be active for all pictures of this event. This number just limits the number of pictures that will be stored to the database."
                                    )
                                    + " \n"
                                    + _(
                                        "If you set this to 0, no event will be logged to the database. Too many pictures can slow down the performance drastically."
                                    )
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                4,
                                ui.input_numeric(
                                    "numElementsPerPage",
                                    _("Maximum pictures per page"),
                                    CONFIG["ELEMENTS_PER_PAGE"],
                                    min=1,
                                ),
                            ),
                            ui.column(
                                8,
                                ui.markdown(
                                    _(
                                        "This setting applies only to the `PICTURES` section in the ungrouped view mode."
                                    )
                                    + "\n\n"
                                    + _(
                                        "NOTE: Too many pictures per page could slow down the performance drastically!"
                                    )
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        ui.br(),
                        full_screen=False,
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),
                # --- Home Assistant / MQTT settings ---
                collapsible_section(
                    "home_assistant_settings",
                    _("Home Assistant configuration"),
                    _("Configure MQTT integration for Home Assistant."),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_switch(
                                    "btnMqttEnabled",
                                    _("Enable MQTT"),
                                    CONFIG.get("MQTT_ENABLED", False),
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                8,
                                ui.input_text(
                                    "txtMqttBrokerAddress",
                                    _("MQTT Broker Address"),
                                    value=CONFIG.get("MQTT_BROKER_ADDRESS", ""),
                                    placeholder=_("e.g. 192.168.1.10"),
                                    width="100%",
                                ),
                            ),
                            ui.column(
                                4,
                                ui.input_numeric(
                                    "numMqttBrokerPort",
                                    _("MQTT Broker Port"),
                                    value=CONFIG.get("MQTT_BROKER_PORT", 1883),
                                    min=1,
                                    max=65535,
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                6,
                                ui.input_text(
                                    "txtMqttUsername",
                                    _("MQTT Username"),
                                    value=CONFIG.get("MQTT_USERNAME", ""),
                                    placeholder=_("MQTT username"),
                                    width="100%",
                                ),
                            ),
                            ui.column(
                                6,
                                ui.input_password(
                                    "txtMqttPassword",
                                    _("MQTT Password"),
                                    value=CONFIG.get("MQTT_PASSWORD", ""),
                                    placeholder=_("MQTT password"),
                                    width="100%",
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_select(
                                    id="mqtt_image_publish_interval",
                                    label=_("Camera update interval"),
                                    choices={
                                        2: "2s",
                                        3: "3s",
                                        5: "5s",
                                        10: "10s",
                                        20: "20s",
                                        30: "30s",
                                        60: "60s",
                                    },
                                    selected=CONFIG["MQTT_IMAGE_PUBLISH_INTERVAL"],
                                ),
                                ui.help_text(
                                    _(
                                        "The interval in seconds between publishing camera images to the MQTT broker."
                                    )
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.h5(_("Home Assistant Card Example")),
                                ui.markdown(
                                    _(
                                        "Copy this configuration to create a dashboard card in Home Assistant:"
                                    )
                                ),
                                ui.markdown(
                                    "```yaml\n"
                                    + "# "
                                    + _("Home Assistant Dashboard Card")
                                    + "\n"
                                    + "type: vertical-stack\n"
                                    + "title: "
                                    + _("KittyHack Cat Flap")
                                    + "\n"
                                    + "cards:\n"
                                    + "  - show_state: false\n"
                                    + "    show_name: false\n"
                                    + "    camera_view: live\n"
                                    + "    fit_mode: cover\n"
                                    + "    type: picture-entity\n"
                                    + "    entity: camera.{}_camera\n".format(
                                        CONFIG["MQTT_DEVICE_ID"]
                                    )
                                    + "  - type: tile\n"
                                    + "    entity: sensor.{}_events\n".format(
                                        CONFIG["MQTT_DEVICE_ID"]
                                    )
                                    + "    features_position: bottom\n"
                                    + "    vertical: false\n"
                                    + "    name: "
                                    + _("Last Event")
                                    + "\n"
                                    + "    show_entity_picture: false\n"
                                    + "    hide_state: false\n"
                                    + "    state_content:\n"
                                    + "      - event\n"
                                    + "      - detected_cat\n"
                                    + "  - type: entities\n"
                                    + "    show_header_toggle: false\n"
                                    + "    entities:\n"
                                    + "      - entity: lock.{}_inside_lock\n".format(
                                        CONFIG["MQTT_DEVICE_ID"]
                                    )
                                    + "        name: "
                                    + _("Inside")
                                    + "\n"
                                    + '        icon: ""\n'
                                    + "        secondary_info: none\n"
                                    + "      - entity: binary_sensor.{}_outside_lock\n".format(
                                        CONFIG["MQTT_DEVICE_ID"]
                                    )
                                    + "        name: "
                                    + _("Outside")
                                    + "\n"
                                    + '        icon: ""\n'
                                    + "    state_color: false\n"
                                    + "    title: "
                                    + _("Magnetic Locks")
                                    + "\n"
                                    + "  - type: entities\n"
                                    + "    title: "
                                    + _("Configuration")
                                    + "\n"
                                    + "    show_header_toggle: false\n"
                                    + "    entities:\n"
                                    + "      - entity: select.{}_allow_enter\n".format(
                                        CONFIG["MQTT_DEVICE_ID"]
                                    )
                                    + "        name: "
                                    + _("Open entrance for...")
                                    + "\n"
                                    + '        icon: ""\n'
                                    + "        secondary_info: none\n"
                                    + "      - entity: select.{}_allow_exit\n".format(
                                        CONFIG["MQTT_DEVICE_ID"]
                                    )
                                    + "        name: "
                                    + _("Allow cats to exit")
                                    + "\n"
                                    + '        icon: ""\n'
                                    + "        secondary_info: none\n"
                                    + "    state_color: false\n"
                                    + "  - type: entities\n"
                                    + "    entities:\n"
                                    + "      - entity: binary_sensor.{}_motion_outside\n".format(
                                        CONFIG["MQTT_DEVICE_ID"]
                                    )
                                    + "        name: "
                                    + _("Motion outside")
                                    + "\n"
                                    + "        secondary_info: last-changed\n"
                                    + "      - entity: binary_sensor.{}_motion_inside\n".format(
                                        CONFIG["MQTT_DEVICE_ID"]
                                    )
                                    + "        name: "
                                    + _("Motion inside")
                                    + "\n"
                                    + "        secondary_info: last-changed\n"
                                    + "      - entity: binary_sensor.{}_prey_detected\n".format(
                                        CONFIG["MQTT_DEVICE_ID"]
                                    )
                                    + "        name: "
                                    + _("Prey detected")
                                    + "\n"
                                    + "        secondary_info: last-changed\n"
                                    + "    title: "
                                    + _("Status")
                                    + "\n"
                                    + "    state_color: false\n"
                                    + "    show_header_toggle: false\n"
                                    + "```"
                                ),
                            ),
                        ),
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),
                # --- Advanced settings ---
                collapsible_section(
                    "advanced_settings",
                    _("Advanced settings"),
                    _(
                        "Advanced configuration options for hostname, logging, and performance."
                    ),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_text(
                                    "txtHostname",
                                    label=_("Hostname"),
                                    placeholder="",
                                    value=hostname,
                                    width="100%",
                                ),
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    _(
                                        "The hostname of the Kittyflap. You can change it to any unique name in your network for an easier access (mDNS / Avahi must be enabled in your router)."
                                    )
                                    + "  \n"
                                    + _(
                                        "Allowed characters: `a-z`, `A-Z`, `0-9` and `-`."
                                    )
                                    + "  \n"
                                    + "> "
                                    + _(
                                        "NOTE: This setting requires a restart of the kittyflap to take effect."
                                    )
                                    + "\n\n"
                                    + _(
                                        "You can access the Kittyflap via the hostname in your browser:"
                                    )
                                ),
                                ui.output_text_verbatim("hostname_preview"),
                                style_="color: grey;",
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                4,
                                ui.input_select(
                                    "txtLoglevel",
                                    "Loglevel",
                                    {
                                        "DEBUG": "DEBUG",
                                        "INFO": "INFO",
                                        "WARN": "WARN",
                                        "ERROR": "ERROR",
                                        "CRITICAL": "CRITICAL",
                                    },
                                    selected=CONFIG["LOGLEVEL"],
                                ),
                            ),
                            ui.column(
                                8,
                                ui.markdown(
                                    _(
                                        "`INFO` is the default log level and should be used in normal operation. `DEBUG` should only be used if it is really necessary!"
                                    )
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        (
                            ui.TagList(
                                ui.hr(),
                                ui.row(
                                    ui.column(
                                        12,
                                        ui.input_switch(
                                            "btnUseAllCoresForImageProcessing",
                                            _("Use all CPU cores for image processing"),
                                            CONFIG[
                                                "USE_ALL_CORES_FOR_IMAGE_PROCESSING"
                                            ],
                                        ),
                                    ),
                                    ui.column(
                                        12,
                                        ui.markdown(
                                            _(
                                                "If this is enabled, all CPU cores will be used for image processing. This results in a faster analysis of the pictures, and therefore a maybe a bit faster prey detection."
                                            )
                                        ),
                                        style="color: grey;",
                                    ),
                                    ui.column(
                                        12,
                                        ui.markdown(
                                            f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                                            + _(
                                                "**WARNING**: It is NOT recommended to enable this feature! Several users have reported that this option causes reboots or system freezes."
                                            )
                                            + _(
                                                "If you encounter the same issue, it's strongly recommended to disable this setting."
                                            )
                                        ),
                                        style_="color: #e74a3b; padding: 10px; border: 1px solid #e74a3b; border-radius: 5px; margin: 20px; width: 90%;",
                                    ),
                                    ui.column(
                                        12,
                                        ui.markdown(
                                            "> "
                                            + _(
                                                "NOTE: This setting requires a restart of the kittyflap to take effect."
                                            )
                                        ),
                                        style_="color: grey;",
                                    ),
                                ),
                                ui.hr(),
                            )
                            if not is_remote_mode()
                            else ui.hr()
                        ),
                        ui.row(
                            ui.column(
                                12,
                                (
                                    lambda _inp: (
                                        _inp
                                        if is_remote_mode()
                                        else _disable_numeric_input(_inp)
                                    )
                                )(
                                    ui.input_numeric(
                                        "numRemoteInferenceMaxFps",
                                        _("Remote inference FPS limit"),
                                        float(
                                            CONFIG.get("REMOTE_INFERENCE_MAX_FPS", 10.0)
                                            or 10.0
                                        ),
                                        min=1,
                                        max=60,
                                        step=1,
                                        width="100%",
                                    )
                                ),
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    (
                                        _(
                                            "Limits the model inference loop to reduce CPU load in remote-mode."
                                        )
                                        + "\n\n> "
                                        + (
                                            _(
                                                "This setting is only configurable in remote-mode."
                                            )
                                            if not is_remote_mode()
                                            else _("Default: 10 FPS")
                                        )
                                    )
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        ui.hr(),
                        (
                            ui.row(
                                ui.column(
                                    12,
                                    ui.input_numeric(
                                        "numRemoteWaitAfterRebootTimeout",
                                        _("Wait for remote after reboot (seconds)"),
                                        float(
                                            CONFIG.get(
                                                "REMOTE_WAIT_AFTER_REBOOT_TIMEOUT", 30.0
                                            )
                                            or 30.0
                                        ),
                                        min=5,
                                        max=600,
                                        step=1,
                                        width="100%",
                                    ),
                                ),
                                ui.column(
                                    12,
                                    ui.markdown(
                                        _(
                                            "If the Kittyflap has been controlled remotely before, it will wait this long for a remote-control takeover after reboot before starting Kittyhack locally."
                                        )
                                    ),
                                    style_="color: grey;",
                                ),
                            )
                            if not is_remote_mode()
                            else ui.HTML(""),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_switch(
                                    "btnRestartIpCameraStreamOnFailure",
                                    _("IP camera watchdog"),
                                    CONFIG["RESTART_IP_CAMERA_STREAM_ON_FAILURE"],
                                ),
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    _(
                                        "If enabled, the stream of an external camera will be automatically restarted if corrupted frames are detected."
                                    )
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        ui.hr(),
                        (
                            ui.row(
                                ui.column(
                                    12,
                                    ui.markdown(
                                        _(
                                            "WLAN watchdog is not available in remote-mode."
                                        )
                                    ),
                                    style_="color: grey;",
                                )
                            )
                            if is_remote_mode()
                            else ui.row(
                                ui.column(
                                    12,
                                    ui.input_switch(
                                        "btnWlanWatchdogEnabled",
                                        _("WLAN watchdog"),
                                        CONFIG["WLAN_WATCHDOG_ENABLED"],
                                    ),
                                ),
                                ui.column(
                                    12,
                                    ui.markdown(
                                        _(
                                            "If enabled, the WLAN connection will be monitored and automatically reconnected on failure. If this also fails, the Kittyflap will automatically restart."
                                        ),
                                    ),
                                    style_="color: grey;",
                                ),
                            )
                        ),
                        ui.hr(),
                        (
                            ui.row(
                                ui.column(
                                    12,
                                    ui.input_select(
                                        "inference_device",
                                        _("Inference device (Experimental!)"),
                                        {
                                            "cpu": _("CPU (default)"),
                                            "cuda:0": _("NVIDIA GPU (CUDA)"),
                                            "intel:gpu": _("Intel GPU (OpenVINO)"),
                                        },
                                        selected=(
                                            "intel:gpu"
                                            if str(
                                                CONFIG.get("INFERENCE_DEVICE", "cpu")
                                                or "cpu"
                                            )
                                            .strip()
                                            .lower()
                                            == "gpu"
                                            else CONFIG.get("INFERENCE_DEVICE", "cpu")
                                        ),
                                    ),
                                ),
                                ui.column(
                                    12,
                                    info_toggle(
                                        "inference_device_info",
                                        _("Show inference device info"),
                                        "- **"
                                        + _("CPU (default):")
                                        + "** "
                                        + _(
                                            "Uses the NCNN model format, optimised for ARM/x86 without a dedicated GPU. Recommended for the Kittyflap hardware."
                                        )
                                        + "\n\n"
                                        + "- **"
                                        + _("NVIDIA GPU (CUDA):")
                                        + "** "
                                        + _(
                                            "Uses the PyTorch `.pt` model with CUDA. Requires a CUDA-capable NVIDIA GPU and PyTorch built with CUDA support."
                                        )
                                        + "\n\n"
                                        + "- **"
                                        + _("Intel GPU (OpenVINO):")
                                        + "** "
                                        + _(
                                            "Uses an exported OpenVINO model via the `openvino` runtime. Suitable for Intel Arc and Intel integrated GPUs."
                                        )
                                        + "\n\n"
                                        + "> "
                                        + _(
                                            "GPU inference only works with custom YOLO models, not with the original Kittyflap TFLite models."
                                        )
                                        + "\n\n"
                                        + "> "
                                        + _(
                                            "Note: This is an experimental feature. While it can significantly speed up inference and reduce CPU load, it may also cause instability on unsupported hardware or with incompatible model formats."
                                        ),
                                    ),
                                ),
                            )
                            if is_remote_mode()
                            else ui.row(
                                ui.column(
                                    12,
                                    ui.markdown(
                                        "> "
                                        + _(
                                            "Inference device is fixed to **CPU** on Kittyflap hardware. GPU inference is only available in remote-mode."
                                        )
                                    ),
                                    style_="color: grey;",
                                ),
                            )
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_switch(
                                    "btnDisableRfidReader",
                                    _("Disable RFID reader"),
                                    CONFIG["DISABLE_RFID_READER"],
                                ),
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    _(
                                        "If this option is enabled, the RFID reader will NOT be powered when motion is detected. "
                                        "Use only for troubleshooting hardware defects or undervoltage reboots. "
                                        "You must rely on 'Open inside direction for: All cats' or camera-based cat detection instead."
                                    )
                                    + "\n\n> "
                                    + _("Default: Disabled")
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        ui.br(),
                        full_screen=False,
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),
                # --- Remote-mode documentation ---
                collapsible_section(
                    "remote_mode_documentation",
                    _("Remote-mode guide"),
                    _("Shows the remote-mode documentation."),
                    ui.div(
                        ui.tags.style(
                            """
                            #remote_mode_documentation_body .kh-remote-doc img {
                                max-width: 100% !important;
                                height: auto !important;
                                display: block;
                                margin: 0.5rem auto;
                            }
                            #remote_mode_documentation_body .kh-remote-doc svg {
                                max-width: 100% !important;
                                height: auto !important;
                            }
                            #remote_mode_documentation_body .kh-remote-doc {
                                overflow-x: auto;
                            }
                            """
                        ),
                        ui.br(),
                        ui.row(
                            ui.column(
                                12,
                                ui.div(
                                    ui.markdown(remote_mode_doc_markdown),
                                    class_="kh-remote-doc",
                                ),
                            ),
                        ),
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),
                ui.br(),
                ui.br(),
                ui.br(),
                ui.br(),
                ui.br(),
                ui.panel_absolute(
                    ui.panel_well(
                        ui.input_action_button(
                            id="bSaveKittyhackConfig",
                            label=_("Save all changes"),
                            icon=icon_svg("floppy-disk"),
                        ),
                        class_="sticky-action-well",
                    ),
                    draggable=False,
                    width="100%",
                    left="0px",
                    right="0px",
                    bottom="0px",
                    fixed=True,
                ),
                id="config_tab_container",
            ),
        )
        return ui_config

    @render.text
    def hostname_preview():
        return "http://" + input.txtHostname() + ".local"

    @reactive.effect
    def update_mouse_threshold_limit():
        # You can update the value, min, max, and step.
        ui.update_slider(
            "sldMouseThreshold",
            value=max(input.sldMouseThreshold(), input.sldMinThreshold()),
            min=input.sldMinThreshold(),
        )
        ui.update_slider(
            "sldCatThreshold",
            value=max(input.sldCatThreshold(), input.sldMinThreshold()),
            min=input.sldMinThreshold(),
        )

    @reactive.Effect
    @reactive.event(input.bSaveKittyhackConfig)
    def on_save_kittyhack_config():
        global _
        global model_handler

        # Capture the update-repository values BEFORE they are overwritten further
        # down. A switch here (e.g. Standard -> Custom, or changing the target fork)
        # almost always implies a different code base — we prompt the user to run
        # an update immediately after save.
        _prev_update_mode = (
            str(CONFIG.get("UPDATE_REPOSITORY_MODE") or "standard").strip().lower()
        )
        _prev_update_repo = str(CONFIG.get("UPDATE_REPOSITORY") or "").strip()

        camera_settings_changed = (
            CONFIG.get("CAMERA_SOURCE") != input.camera_source()
            or CONFIG.get("IP_CAMERA_URL") != input.ip_camera_url()
            or CONFIG.get("ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE", False)
            != input.btnEnableIpCameraDecodeScalePipeline()
            or CONFIG.get("IP_CAMERA_TARGET_RESOLUTION", "640x360")
            != input.ip_camera_target_resolution()
            or int(CONFIG.get("IP_CAMERA_PIPELINE_FPS_LIMIT", 10) or 10)
            != int(input.ip_camera_pipeline_fps_limit())
            or (
                is_remote_mode()
                and str(CONFIG.get("IP_CAMERA_HW_DECODE", "auto") or "auto")
                != str(input.ip_camera_hw_decode() or "auto")
            )
        )

        # Check the time ranges for allowed to exit
        def validate_time_format(field_id):
            # Get the value from the input field
            value = input[field_id]()
            # Check if value exists and doesn't match the time format
            if value and not re.match(r"^\d{2}:\d{2}$", value):
                ui.notification_show(
                    _("Allowed to exit time ranges: ")
                    + _("Invalid time format. Please use HH:MM format.")
                    + "\n"
                    + _("Changes were not saved."),
                    duration=10,
                    type="error",
                )
                return False
                # Check if the time is in the valid range
            try:
                hour, minute = map(int, value.split(":"))
                if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                    ui.notification_show(
                        _("Allowed to exit time ranges: ")
                        + _("Time must be between 00:00 and 23:59.")
                        + "\n"
                        + _("Changes were not saved."),
                        duration=10,
                        type="error",
                    )
                    return False
            except ValueError:
                ui.notification_show(
                    _("Allowed to exit time ranges: ")
                    + _("Invalid time value.")
                    + "\n"
                    + _("Changes were not saved."),
                    duration=10,
                    type="error",
                )
                return False
            return True

            # Validate all time input fields

        time_fields = [
            "txtAllowedToExitRange1From",
            "txtAllowedToExitRange1To",
            "txtAllowedToExitRange2From",
            "txtAllowedToExitRange2To",
            "txtAllowedToExitRange3From",
            "txtAllowedToExitRange3To",
        ]

        for field_id in time_fields:
            valid = validate_time_format(field_id)
            if not valid:
                return

                # Validate + normalize custom update repository spec when "custom" mode is selected.
                # The user may type several equivalent forms (owner/repo, owner/repo@ref,
                # owner:branch with GitHub PR shorthand). We store the canonical form.
        normalized_update_repo: str | None = None
        if input.update_repository_mode() == "custom":
            normalized_update_repo = Versioning.normalize_repo_spec(
                input.update_repository() or ""
            )
            if normalized_update_repo is None:
                ui.notification_show(
                    _("Update repository: ")
                    + _(
                        "Invalid format. Use 'owner/repo', 'owner/repo@branch-or-tag', or 'owner:branch'."
                    )
                    + "\n"
                    + _("Changes were not saved."),
                    duration=10,
                    type="error",
                )
                return

                # Existence check against GitHub. Skipped when the spec is unchanged so
                # a transient network glitch cannot block unrelated settings saves on
                # an already-validated spec.
            spec_changed = (
                _prev_update_mode != "custom"
                or _prev_update_repo != normalized_update_repo
            )
            if spec_changed:
                ok, reason, detail = Versioning.check_custom_update_repo_reachable(
                    normalized_update_repo
                )
                if not ok:
                    if reason == "repo_not_found":
                        msg = _("Repository '{}' was not found on GitHub.").format(
                            detail
                        )
                    elif reason == "ref_not_found":
                        msg = _("Branch or tag '{}' was not found on GitHub.").format(
                            detail
                        )
                    elif reason == "network_error":
                        msg = _(
                            "Could not reach GitHub to verify the custom repository: {}"
                        ).format(detail)
                    else:
                        msg = _("Custom update repository could not be validated.")
                    ui.notification_show(
                        _("Update repository: ")
                        + msg
                        + "\n"
                        + _("Changes were not saved."),
                        duration=12,
                        type="error",
                    )
                    return

                    # Check for a changed hostname
        hostname_changed = input.txtHostname() != KittyhackUpdater.get_hostname()
        if hostname_changed:
            # Check if the hostname is valid
            if not re.match(r"^[a-zA-Z0-9-]+$", input.txtHostname()):
                ui.notification_show(
                    _("Hostname: ")
                    + _(
                        "Invalid hostname. Only letters, numbers and hyphens are allowed."
                    )
                    + "\n"
                    + _("Changes were not saved."),
                    duration=10,
                    type="error",
                )
                return
                # Hostname is valid, set it
            KittyhackUpdater.set_hostname(input.txtHostname())

        mqtt_settings_changed = (
            CONFIG["MQTT_ENABLED"] != input.btnMqttEnabled()
            or CONFIG["MQTT_BROKER_ADDRESS"] != input.txtMqttBrokerAddress()
            or CONFIG["MQTT_BROKER_PORT"] != int(input.numMqttBrokerPort())
            or CONFIG["MQTT_USERNAME"] != input.txtMqttUsername()
            or CONFIG["MQTT_PASSWORD"] != input.txtMqttPassword()
            or CONFIG["MQTT_IMAGE_PUBLISH_INTERVAL"]
            != float(input.mqtt_image_publish_interval())
        )

        if input.camera_source() == "ip_camera":
            # Accept RTSP, HTTP(S), RTMP, UDP, TCP, and file URLs
            if not re.match(
                r"^(rtsp|http|https|rtmp|udp|tcp|file)://",
                input.ip_camera_url(),
                re.IGNORECASE,
            ):
                ui.notification_show(
                    _("IP Camera URL: ")
                    + _(
                        "Invalid stream URL format. Please use a valid URL starting with rtsp://, http://, https://, rtmp://, udp://, tcp://, or file://"
                    )
                    + "\n"
                    + _("Changes were not saved."),
                    duration=10,
                    type="error",
                )
                return

        if input.btnEnableIpCameraDecodeScalePipeline():
            ffmpeg_ok = DependencyInstaller.ensure_ffmpeg_installed()
            if not ffmpeg_ok:
                ui.notification_show(
                    _(
                        "FFmpeg is required for the decode+scale pipeline but could not be installed automatically."
                    )
                    + "\n"
                    + _(
                        "Please install `ffmpeg` manually and try again. Changes were not saved."
                    ),
                    duration=12,
                    type="error",
                )
                return

                # override the variable with the data from the configuration page
        language_changed = CONFIG["LANGUAGE"] != input.txtLanguage()
        rfid_state_changed = (
            CONFIG["DISABLE_RFID_READER"] != input.btnDisableRfidReader()
        )
        if (
            input.selectedModel().startswith("tflite::")
            and input.selectedModel() != f"tflite::{CONFIG['TFLITE_MODEL_VERSION']}"
        ):
            selected_model_changed = True
        elif (
            input.selectedModel().startswith("yolo::")
            and input.selectedModel() != f"yolo::{CONFIG['YOLO_MODEL']}"
        ):
            selected_model_changed = True
        else:
            selected_model_changed = False
        if is_remote_mode():
            img_processing_cores_changed = False
        else:
            img_processing_cores_changed = (
                CONFIG["USE_ALL_CORES_FOR_IMAGE_PROCESSING"]
                != input.btnUseAllCoresForImageProcessing()
            )

        inference_device_changed = (
            is_remote_mode()
            and CONFIG.get("INFERENCE_DEVICE", "cpu") != input.inference_device()
        )

        # Update the configuration dictionary with the new values
        CONFIG["LANGUAGE"] = input.txtLanguage()
        CONFIG["TIMEZONE"] = input.txtConfigTimezone()
        CONFIG["DATE_FORMAT"] = input.txtConfigDateformat()
        CONFIG["MOUSE_THRESHOLD"] = float(input.sldMouseThreshold())
        CONFIG["MIN_THRESHOLD"] = float(input.sldMinThreshold())
        try:
            CONFIG["MIN_SECONDS_TO_ANALYZE"] = max(
                0.1, round(float(input.numMinSecondsToAnalyze()), 1)
            )
        except Exception:
            CONFIG["MIN_SECONDS_TO_ANALYZE"] = float(
                DEFAULT_CONFIG["Settings"]["min_seconds_to_analyze"]
            )
        CONFIG["ELEMENTS_PER_PAGE"] = int(input.numElementsPerPage())
        CONFIG["MAX_PHOTOS_COUNT"] = int(input.numMaxPhotosCount())
        CONFIG["LOGLEVEL"] = input.txtLoglevel()
        CONFIG["MOUSE_CHECK_ENABLED"] = input.btnDetectPrey()

        # Always update the model configuration and ensure, that only one of the two is set
        # Check if the selected model is a TFLite model or a YOLO model
        if input.selectedModel().startswith("yolo::"):
            CONFIG["YOLO_MODEL"] = input.selectedModel().replace("yolo::", "")
            CONFIG["TFLITE_MODEL_VERSION"] = ""
        elif input.selectedModel().startswith("tflite::"):
            CONFIG["TFLITE_MODEL_VERSION"] = input.selectedModel().replace(
                "tflite::", ""
            )
            CONFIG["YOLO_MODEL"] = ""

        CONFIG["INFERENCE_DEVICE"] = (
            input.inference_device() if is_remote_mode() else "cpu"
        )

        if is_remote_mode() and str(
            input.inference_device() or ""
        ).strip().lower().startswith("intel:"):
            openvino_ok = DependencyInstaller.ensure_openvino_installed()
            if not openvino_ok:
                ui.notification_show(
                    _("OpenVINO: ")
                    + _(
                        "The `openvino` package could not be installed automatically. "
                        "Please run `pip install openvino` manually and try again. "
                        "Changes were not saved."
                    ),
                    duration=None,
                    type="error",
                )
                return

        CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = input.btnUseCameraForCatDetection()
        CONFIG["CAT_THRESHOLD"] = float(input.sldCatThreshold())
        CONFIG["USE_CAMERA_FOR_MOTION_DETECTION"] = (
            input.btnUseCameraForMotionDetection()
        )
        CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter(input.txtAllowedToEnter())
        CONFIG["LIVE_VIEW_REFRESH_INTERVAL"] = float(input.numLiveViewUpdateInterval())
        from src.baseconfig import AllowedToExit as ATE

        CONFIG["ALLOWED_TO_EXIT"] = ATE(input.btnAllowedToExit())
        CONFIG["PERIODIC_VERSION_CHECK"] = input.btnPeriodicVersionCheck()
        CONFIG["UPDATE_REPOSITORY_MODE"] = input.update_repository_mode()
        # Use the normalized value computed during validation above, or keep the
        # raw input when mode is 'standard' (UPDATE_REPOSITORY is ignored there).
        CONFIG["UPDATE_REPOSITORY"] = (
            normalized_update_repo
            if normalized_update_repo is not None
            else (input.update_repository() or "").strip()
        )
        # TODO: Outside PIR shall not yet be configurable. Need to redesign the camera control, otherwise we will have no cat pictures at high PIR thresholds.
        # CONFIG['PIR_OUTSIDE_THRESHOLD'] = 10-int(input.sldPirOutsideThreshold())
        CONFIG["PIR_INSIDE_THRESHOLD"] = float(input.sldPirInsideThreshold())
        CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"] = input.btnImmediateLockAfterPassage()
        if not is_remote_mode():
            CONFIG["WLAN_TX_POWER"] = int(input.sldWlanTxPower())
        CONFIG["LOCK_DURATION_AFTER_PREY_DETECTION"] = int(
            input.sldLockAfterPreyDetect()
        )
        CONFIG["MAX_PICTURES_PER_EVENT_WITH_RFID"] = int(
            input.numMaxPicturesPerEventWithRfid()
        )
        CONFIG["MAX_PICTURES_PER_EVENT_WITHOUT_RFID"] = int(
            input.numMaxPicturesPerEventWithoutRfid()
        )
        if is_remote_mode():
            CONFIG["USE_ALL_CORES_FOR_IMAGE_PROCESSING"] = True
        else:
            CONFIG["USE_ALL_CORES_FOR_IMAGE_PROCESSING"] = (
                input.btnUseAllCoresForImageProcessing()
            )
        CONFIG["ALLOWED_TO_EXIT_RANGE1"] = input.btnAllowedToExitRange1()
        CONFIG["ALLOWED_TO_EXIT_RANGE1_FROM"] = input.txtAllowedToExitRange1From()
        CONFIG["ALLOWED_TO_EXIT_RANGE1_TO"] = input.txtAllowedToExitRange1To()
        CONFIG["ALLOWED_TO_EXIT_RANGE2"] = input.btnAllowedToExitRange2()
        CONFIG["ALLOWED_TO_EXIT_RANGE2_FROM"] = input.txtAllowedToExitRange2From()
        CONFIG["ALLOWED_TO_EXIT_RANGE2_TO"] = input.txtAllowedToExitRange2To()
        CONFIG["ALLOWED_TO_EXIT_RANGE3"] = input.btnAllowedToExitRange3()
        CONFIG["ALLOWED_TO_EXIT_RANGE3_FROM"] = input.txtAllowedToExitRange3From()
        CONFIG["ALLOWED_TO_EXIT_RANGE3_TO"] = input.txtAllowedToExitRange3To()
        CONFIG["CAMERA_SOURCE"] = input.camera_source()
        CONFIG["IP_CAMERA_URL"] = input.ip_camera_url()
        CONFIG["ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE"] = (
            input.btnEnableIpCameraDecodeScalePipeline()
        )
        CONFIG["IP_CAMERA_TARGET_RESOLUTION"] = input.ip_camera_target_resolution()
        try:
            CONFIG["IP_CAMERA_PIPELINE_FPS_LIMIT"] = int(
                input.ip_camera_pipeline_fps_limit()
            )
        except Exception:
            CONFIG["IP_CAMERA_PIPELINE_FPS_LIMIT"] = 10
        try:
            # Only available in remote-mode UI; target-mode defaults to 'auto'
            CONFIG["IP_CAMERA_HW_DECODE"] = (
                str(input.ip_camera_hw_decode() or "auto").strip().lower()
            )
        except Exception:
            CONFIG["IP_CAMERA_HW_DECODE"] = "auto"
        if CONFIG["IP_CAMERA_HW_DECODE"] not in {
            "auto",
            "none",
            "cuda",
            "vaapi",
            "qsv",
        }:
            CONFIG["IP_CAMERA_HW_DECODE"] = "auto"

        if camera_settings_changed:
            # Ensure live view reacts immediately (do not keep showing an old frame).
            try:
                live_view_aspect.set((4, 3))
            except Exception:
                pass

            live_view_image = getattr(ctx, "live_view_image", None)
            if live_view_image is not None and getattr(
                live_view_image, "_is_running", False
            ):
                # Defer forced refresh until the current render is done to avoid
                # overlapping recalculations (client progress-state errors).
                live_view_image._refresh_after_run = True
            else:
                live_view_refresh_nonce.set(live_view_refresh_nonce.get() + 1)
        CONFIG["MQTT_ENABLED"] = input.btnMqttEnabled()
        CONFIG["MQTT_BROKER_ADDRESS"] = input.txtMqttBrokerAddress()
        CONFIG["MQTT_BROKER_PORT"] = int(input.numMqttBrokerPort())
        CONFIG["MQTT_USERNAME"] = input.txtMqttUsername()
        CONFIG["MQTT_PASSWORD"] = input.txtMqttPassword()
        CONFIG["MQTT_IMAGE_PUBLISH_INTERVAL"] = float(
            input.mqtt_image_publish_interval()
        )
        CONFIG["RESTART_IP_CAMERA_STREAM_ON_FAILURE"] = (
            input.btnRestartIpCameraStreamOnFailure()
        )
        if not is_remote_mode():
            CONFIG["WLAN_WATCHDOG_ENABLED"] = input.btnWlanWatchdogEnabled()
        else:
            CONFIG["WLAN_WATCHDOG_ENABLED"] = False
        CONFIG["DISABLE_RFID_READER"] = input.btnDisableRfidReader()

        if not is_remote_mode():
            try:
                CONFIG["REMOTE_WAIT_AFTER_REBOOT_TIMEOUT"] = float(
                    input.numRemoteWaitAfterRebootTimeout()
                )
            except Exception:
                CONFIG["REMOTE_WAIT_AFTER_REBOOT_TIMEOUT"] = float(
                    CONFIG.get(
                        "REMOTE_WAIT_AFTER_REBOOT_TIMEOUT",
                        DEFAULT_CONFIG["Settings"]["remote_wait_after_reboot_timeout"],
                    )
                    or DEFAULT_CONFIG["Settings"]["remote_wait_after_reboot_timeout"]
                )
                # Keep it within a sane operational range.
            CONFIG["REMOTE_WAIT_AFTER_REBOOT_TIMEOUT"] = max(
                5.0, min(600.0, float(CONFIG["REMOTE_WAIT_AFTER_REBOOT_TIMEOUT"]))
            )

        if is_remote_mode():
            try:
                CONFIG["REMOTE_INFERENCE_MAX_FPS"] = float(
                    input.numRemoteInferenceMaxFps()
                )
            except Exception:
                CONFIG["REMOTE_INFERENCE_MAX_FPS"] = float(
                    CONFIG.get("REMOTE_INFERENCE_MAX_FPS", 10.0) or 10.0
                )

                # Update the log level
        configure_logging(input.txtLoglevel())

        # Save the configuration to the config file
        _ = set_language(CONFIG["LANGUAGE"])

        # Check for invalid combinations of settings
        if (
            input.selectedModel().startswith("tflite::")
            and input.btnUseCameraForMotionDetection()
        ):
            CONFIG["USE_CAMERA_FOR_MOTION_DETECTION"] = False
            ui.notification_show(
                _("Invalid configuration: ")
                + _(
                    "You cannot use the camera for motion detection in combination with an original Kittyflap model. You need a custom trained model for this."
                )
                + "\n"
                + _('The setting "{}" was disabled.').format(
                    _("Use camera for motion detection")
                ),
                duration=None,
                type="warning",
            )
        if (
            input.selectedModel().startswith("tflite::")
            and input.btnUseCameraForCatDetection()
        ):
            CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = False
            ui.notification_show(
                _("Invalid configuration: ")
                + _(
                    "You cannot use the camera for cat detection in combination with an original Kittyflap model. You need a custom trained model for this."
                )
                + "\n"
                + _('The setting "{}" was disabled.').format(
                    _("Use camera for cat detection")
                ),
                duration=None,
                type="warning",
            )

        if save_config():
            ui.notification_show(
                _("Kittyhack configuration updated successfully."),
                duration=5,
                type="message",
            )
            model_reload_failed = False

            if selected_model_changed or inference_device_changed:
                try:
                    reload_ok, active_model_handler = reload_model_handler_runtime()
                    if reload_ok:
                        model_handler = active_model_handler
                        ui.notification_show(
                            _("Model change applied."),
                            duration=6,
                            type="message",
                        )
                    else:
                        model_reload_failed = True
                        ui.notification_show(
                            _(
                                "Model change could not be applied live. A reboot is still required."
                            ),
                            duration=10,
                            type="warning",
                        )
                except Exception as e:
                    model_reload_failed = True
                    logging.error(f"Failed to apply model change live: {e}")

            if language_changed:
                ui.notification_show(
                    _(
                        "Please restart the kittyflap in the [SYSTEM] section, to apply the new language."
                    ),
                    duration=30,
                    type="message",
                )
                update_mqtt_language()

            restart_modal_shown = False
            if (
                (
                    (selected_model_changed or inference_device_changed)
                    and model_reload_failed
                )
                or hostname_changed
                or img_processing_cores_changed
                or rfid_state_changed
            ):
                ui.modal_remove()
                ui.modal_show(
                    ui.modal(
                        _(
                            "A restart is required to apply the changes. Do you want to reboot the kittyflap now?"
                        ),
                        title=_("Restart required"),
                        easy_close=True,
                        footer=ui.div(
                            ui.input_action_button("btn_modal_reboot_ok", _("Reboot")),
                            ui.input_action_button("btn_modal_cancel", _("Cancel")),
                        ),
                    )
                )
                restart_modal_shown = True

                # Offer to run the update right away when the update source changed.
                # Usually the new source has a different code base than what's installed,
                # so the user's next natural step is to update. We only prompt when no
                # other restart-modal is already up to avoid stacking.
            update_repo_changed = (
                _prev_update_mode
                != str(CONFIG.get("UPDATE_REPOSITORY_MODE") or "standard")
                .strip()
                .lower()
                or _prev_update_repo
                != str(CONFIG.get("UPDATE_REPOSITORY") or "").strip()
            )
            if (
                update_repo_changed
                and startup.git_repo_available
                and not restart_modal_shown
            ):
                ui.modal_remove()
                ui.modal_show(
                    ui.modal(
                        ui.markdown(
                            _(
                                "The update repository was changed. The new source most likely has a different code base than what is currently installed."
                            )
                            + "\n\n"
                            + _(
                                "Do you want to run an update now to switch to the new source?"
                            )
                        ),
                        title=_("Update source changed"),
                        easy_close=False,
                        footer=ui.div(
                            ui.input_action_button(
                                "btn_modal_update_repo_now",
                                _("Update now"),
                                class_="btn-primary",
                            ),
                            ui.input_action_button("btn_modal_cancel", _("Later")),
                        ),
                    )
                )

            if mqtt_settings_changed:
                logging.info("MQTT settings changed. Restarting MQTT client...")
                success = restart_mqtt()
                if not success:
                    ui.notification_show(
                        _("Failed to restart MQTT client. Check the logs for details."),
                        duration=10,
                        type="error",
                    )
            else:
                # Just update the door configuration in MQTT
                update_mqtt_config("ALLOWED_TO_ENTER")
                update_mqtt_config("ALLOWED_TO_EXIT")

                # Sync live view input
            ui.update_select(
                "quick_allowed_to_enter", selected=str(CONFIG["ALLOWED_TO_ENTER"].value)
            )
            ui.update_select(
                "quick_allowed_to_exit", selected=str(CONFIG["ALLOWED_TO_EXIT"].value)
            )
            # Trigger UI components that depend on these settings to re-render
            reload_trigger_config.set(reload_trigger_config.get() + 1)

        else:
            ui.notification_show(
                _("Failed to save the Kittyhack configuration."),
                duration=10,
                type="error",
            )
