"""Configuration tab and save handler."""

import asyncio
import os
import time
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
    UserNotifications,
)
from src.helper import Versioning, SystemInfo
from src.system import (
    DependencyInstaller,
    KittyhackUpdater,
)
from src.paths import kittyhack_root
from src.mode import is_remote_mode
from src.model import YoloModel
from src.database import EventsRepo
from src.backend import (
    restart_mqtt,
    update_mqtt_config,
    update_mqtt_language,
    reload_model_handler_runtime,
)
from src.backend.decisions import (
    should_suggest_pir_second_factor,
    sync_original_model_notice,
    sync_pir_second_factor_suggestion,
    uses_original_detection_model,
)
from src.server_ui.state import (
    reload_trigger_config,
    live_view_refresh_nonce,
    live_view_aspect,
)
from src.server_ui.helpers import (
    _disable_numeric_input,
    _disable_switch,
    collapsible_section,
)
from src.server_ui.unlock_logic import entry_logic_ui, exit_logic_ui
from src.server_ui.context import SessionContext
import src.startup as startup

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
        def info_hint(id_suffix: str, aria_label: str, body_md: str):
            """Click-to-open (i) for a setting. Click, not hover, so it works on touch screens."""
            return ui.popover(
                ui.tags.button(
                    icon_svg("circle-info", margin_left="0", margin_right="0"),
                    type="button",
                    class_="kh-info-btn",
                    **{"aria-label": aria_label},
                ),
                ui.div(ui.markdown(body_md), class_="kh-info-popover-body"),
                id=f"info_{id_suffix}",
                placement="bottom",
                options={
                    "customClass": "kh-info-popover",
                    "container": "body",
                    "trigger": "click",
                    "fallbackPlacements": ["top", "bottom", "left", "right"],
                },
            )

        def field_with_info(control, id_suffix: str, aria_label: str, body_md: str):
            return ui.div(
                control,
                ui.div(
                    info_hint(id_suffix, aria_label, body_md),
                    class_="kh-field-info",
                ),
                class_="kh-field",
            )

        def _split_remote_mode_doc(markdown: str, doc_lang: str) -> tuple[str, str]:
            marker = "#### Diagramme" if doc_lang == "de" else "#### Diagrams"
            idx = markdown.find(marker)
            if idx < 0:
                return markdown, ""
            main = markdown[:idx].rstrip()
            if main.endswith("---"):
                main = main[:-3].rstrip()
            lines = markdown[idx:].splitlines()
            if lines and lines[0].startswith("#### "):
                lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
            return main, "\n".join(lines).strip()

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

        def config_mode(value) -> str:
            return str(getattr(value, "value", value))

        def logic_block(button_id, hint_id, section_id, container_id, panels):
            return ui.div(
                ui.tags.button(
                    ui.tags.span(_("Show decision logic")),
                    id=button_id,
                    type="button",
                    class_="btn btn-default kh-logic-btn",
                    data_show_label=_("Show decision logic"),
                    data_hide_label=_("Hide decision logic"),
                ),
                ui.div(
                    _("The steps follow the option selected above."),
                    id=hint_id,
                    class_="kh-logic-hint",
                    style_="display:none;",
                ),
                ui.div(
                    ui.div(*panels, id=container_id, class_="kh-flow-list"),
                    id=section_id,
                    class_="logic-section",
                    style_="display:none;",
                ),
                class_="d-flex flex-column align-items-center w-100",
            )

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
            remote_mode_doc_markdown = _("The remote mode guide could not be loaded.")
        remote_mode_doc_main, remote_mode_doc_diagrams = _split_remote_mode_doc(
            remote_mode_doc_markdown, lang
        )

        camera_based_entry = bool(
            CONFIG.get("USE_CAMERA_FOR_CAT_DETECTION")
            or CONFIG.get("USE_CAMERA_FOR_MOTION_DETECTION")
        )
        pir_for_camera_entry_switch = ui.input_switch(
            "btnRequireOutsidePirForCameraEntry",
            _("Additionally require the outside PIR for camera-based entry"),
            CONFIG.get("REQUIRE_OUTSIDE_PIR_FOR_CAMERA_ENTRY", False),
            width="90%",
        )
        if not camera_based_entry:
            pir_for_camera_entry_switch = _disable_switch(pir_for_camera_entry_switch)

        ui_config = ui.div(
            ui.div(
                # --- General settings ---
                ui.br(),
                collapsible_section(
                    "general_settings",
                    _("General"),
                    _("Choose the language, time zone, and display options for Kittyhack."),
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
                                    _("Check for updates automatically"),
                                    CONFIG["PERIODIC_VERSION_CHECK"],
                                ),
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    _(
                                        "Kittyhack will regularly check whether a new version is available."
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
                                    _("Update source"),
                                    {
                                        "standard": _("Standard"),
                                        "beta": _("Beta"),
                                        "custom": _("Custom"),
                                    },
                                    selected=CONFIG.get(
                                        "UPDATE_REPOSITORY_MODE", "standard"
                                    ),
                                ),
                            ),
                            ui.column(
                                6,
                                ui.input_text(
                                    "update_repository",
                                    _("Custom update source"),
                                    value=CONFIG.get("UPDATE_REPOSITORY", ""),
                                    placeholder=_(
                                        "For example: owner/repo@branch or owner:branch"
                                    ),
                                    width="100%",
                                ),
                                id="update_repository_container",
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    _(
                                        "Beta builds may be unstable. "
                                        "Back up your database before switching between **Standard** and **Beta**. "
                                        "Switching from beta back to the latest release may work, "
                                        "but it is not recommended."
                                    )
                                ),
                                style_=(
                                    "color: var(--bs-warning-text-emphasis, #997404);"
                                    + (
                                        ""
                                        if str(
                                            CONFIG.get(
                                                "UPDATE_REPOSITORY_MODE", "standard"
                                            )
                                            or "standard"
                                        )
                                        .strip()
                                        .lower()
                                        == "beta"
                                        else "display: none;"
                                    )
                                ),
                                id="update_repository_beta_warning",
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    _(
                                        "**Standard** uses the officially released versions."
                                    )
                                ),
                                style_=(
                                    "color: grey;"
                                    + (
                                        ""
                                        if str(
                                            CONFIG.get(
                                                "UPDATE_REPOSITORY_MODE", "standard"
                                            )
                                            or "standard"
                                        )
                                        .strip()
                                        .lower()
                                        == "standard"
                                        else "display: none;"
                                    )
                                ),
                                id="update_repository_help",
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    _(
                                        "Accepted custom formats:\n\n"
                                        "- `owner/repo` — latest stable release tag from that fork\n"
                                        "- `owner/repo@branch-or-tag` — track the specified ref\n"
                                        "- `owner:branch` — GitHub PR head-ref shorthand; "
                                        "you can copy it straight from a pull request header, "
                                        "the repo name defaults to `kittyhack`"
                                    )
                                ),
                                style_=(
                                    "color: grey;"
                                    + (
                                        ""
                                        if str(
                                            CONFIG.get(
                                                "UPDATE_REPOSITORY_MODE", "standard"
                                            )
                                            or "standard"
                                        )
                                        .strip()
                                        .lower()
                                        == "custom"
                                        else "display: none;"
                                    )
                                ),
                                id="update_repository_custom_help",
                            ),
                        ),
                        ui.hr(),
                        (
                            ui.row(
                                ui.column(
                                    12,
                                    ui.markdown(
                                        _(
                                            "WLAN settings are not available in remote mode."
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
                                        _("WLAN transmit power (dBm)"),
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
                                            "Keep the transmit power as low as possible to avoid interference with the motion sensors. Increase it only if the WLAN connection is unreliable."
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
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                ui.input_action_button(
                                    "btn_reset_muted_notifications",
                                    _("Show silenced notifications again"),
                                    class_="btn-outline-secondary kh-wrap-btn",
                                ),
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    _(
                                        "Some warnings can be hidden with **Do not notify again**. "
                                        "This restores all of them."
                                    )
                                    + "\n\n"
                                    + _("Currently silenced: {n}").format(
                                        n=len(UserNotifications.get_muted_ids())
                                    )
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),
                # --- Camera settings ---
                collapsible_section(
                    "camera_settings",
                    _("Camera"),
                    _("Choose and configure the camera Kittyhack uses."),
                    ui.div(
                        ui.row(
                            ui.column(
                                12,
                                field_with_info(
                                    ui.input_select(
                                        "camera_source",
                                        _("Camera source"),
                                        {
                                            "internal": _("Kittyflap camera"),
                                            "ip_camera": _("External IP camera"),
                                        },
                                        selected=CONFIG["CAMERA_SOURCE"],
                                    ),
                                    "camera_intro",
                                    _("Show IP camera tips"),
                                    _(
                                        "You can use an **external IP camera** instead of the Kittyflap camera for a better viewing angle or clearer night vision.\n\n"
                                        "- The Kittyflap and IP camera both need a stable network connection. Ethernet is recommended for the camera.\n"
                                        "- Use a **static IP** for the camera.\n"
                                        "- Ensure a compatible stream (RTSP / HTTP MJPEG / RTMP / UDP / TCP).\n"
                                        "- A maximum of 1280x720 at 15 FPS is recommended. Higher settings may reduce performance.\n"
                                        "- Set the resolution in the camera settings or use another stream URL.\n\n"
                                        "If the stream is interrupted, check the WLAN signal and network settings."
                                    ),
                                ),
                            ),
                            ui.column(
                                12,
                                field_with_info(
                                    ui.input_text(
                                        "ip_camera_url",
                                        _("External IP camera URL"),
                                        value=CONFIG["IP_CAMERA_URL"],
                                        placeholder=_(
                                            "e.g. rtsp://user:pass@192.168.1.100:554/stream"
                                        ),
                                        width="100%",
                                    ),
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
                                id="ip_camera_url_container",
                            ),
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
                                                _("Reduce IP camera resolution"),
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
                                                    "Kittyhack can reduce the IP camera resolution before processing each picture."
                                                )
                                                + "  \n"
                                                + _(
                                                    "This may lower CPU use and process high-resolution streams more smoothly."
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
                                                    "For a good balance of speed and image quality, choose `640x360` or `1280x720`."
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
                                                _("IP camera frame rate limit"),
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
                                                    "The default is `10 FPS`. Choose `Unlimited` to remove the limit; this may increase CPU use."
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
                    _("Flap"),
                    _("Choose when the Kittyflap unlocks and how it responds to cats and prey."),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(
                                12,
                                field_with_info(
                                    ui.input_slider(
                                        "sldMinThreshold",
                                        _("Minimum detection threshold"),
                                        min=0,
                                        max=80,
                                        width="90%",
                                        value=CONFIG["MIN_THRESHOLD"],
                                    ),
                                    "min_threshold_info",
                                    _("Explain the minimum detection threshold"),
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
                                field_with_info(
                                    ui.input_slider(
                                        "sldMouseThreshold",
                                        _("Threshold for prey detection"),
                                        min=0,
                                        max=100,
                                        width="90%",
                                        value=CONFIG["MOUSE_THRESHOLD"],
                                    ),
                                    "mouse_threshold_info",
                                    _("Explain the prey detection threshold"),
                                    _(
                                        "Kittyhack uses this value to decide whether a picture contains prey. "
                                        "If the detected probability is above this threshold, the flap stays locked.\n"
                                        "*(Default value: {})*\n\n"
                                        "The minimum follows the **Minimum detection threshold**."
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
                                field_with_info(
                                    ui.input_slider(
                                        "sldCatThreshold",
                                        _("Threshold for cat detection"),
                                        min=0,
                                        max=100,
                                        width="90%",
                                        value=CONFIG["CAT_THRESHOLD"],
                                    ),
                                    "cat_threshold_info",
                                    _("Explain the cat detection threshold"),
                                    _(
                                        "Kittyhack uses this value to decide whether a picture contains your cat."
                                    )
                                    + "  \n"
                                    + _(
                                        "When the detected probability is above this threshold and **Use camera for cat detection** is enabled, the flap unlocks."
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
                                        "The minimum follows the **Minimum detection threshold** above."
                                    )
                                    + "**",
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                field_with_info(
                                    ui.input_numeric(
                                        "numMinSecondsToAnalyze",
                                        _("Wait before deciding to unlock (seconds)"),
                                        float(
                                            CONFIG.get("MIN_SECONDS_TO_ANALYZE", 1.5) or 1.5
                                        ),
                                        min=0.1,
                                        step=0.1,
                                    ),
                                    "min_seconds_info",
                                    _("Explain the unlock delay"),
                                    _(
                                        "After motion is detected outside, Kittyhack waits this long before it may unlock."
                                    )
                                    + "  \n"
                                    + _(
                                        "The flap unlocks only if no prey is detected during this time."
                                    )
                                    + "  \n\n"
                                    + _(
                                        "After unlocking, the flap locks again if a later picture reaches the prey detection threshold."
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
                                field_with_info(
                                    ui.input_slider(
                                        "sldLockAfterPreyDetect",
                                        _("Keep locked after detecting prey (seconds)"),
                                        min=30,
                                        max=1800,
                                        step=5,
                                        width="90%",
                                        value=CONFIG["LOCK_DURATION_AFTER_PREY_DETECTION"],
                                    ),
                                    "lock_after_prey_info",
                                    _("Explain the lock duration"),
                                    _(
                                        "The flap stays locked for this long after Kittyhack detects prey."
                                    ),
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                field_with_info(
                                    ui.input_switch(
                                        "btnDetectPrey",
                                        _("Detect prey"),
                                        CONFIG["MOUSE_CHECK_ENABLED"],
                                    ),
                                    "detect_prey_info",
                                    _("Explain prey detection"),
                                    _(
                                        "When prey detection is enabled and a picture exceeds the prey detection threshold, the flap stays locked.\n\n"
                                        "This setting applies to all cats. You can also choose it for each cat under **Cats**."
                                    ),
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                field_with_info(
                                    ui.input_select(
                                        "selectedModel",
                                        _("Detection model"),
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
                                    "model_versions_info",
                                    _("Show model details"),
                                    "- **"
                                    + _("Original Kittyflap model v1:")
                                    + "** "
                                    + _(
                                        "Always tries to detect objects `Mouse` and `No Mouse`, even if there is no such object in the picture."
                                    )
                                    + "\n\n"
                                    + "- **"
                                    + _("Original Kittyflap model v2:")
                                    + "** "
                                    + _(
                                        "Only tries to detect objects `Mouse` and `No Mouse` if there is a cat in the picture."
                                    )
                                    + "\n\n"
                                    + "- **"
                                    + _("Custom models:")
                                    + "** "
                                    + _(
                                        "These are your own trained models, which you have created in the **AI Training** section."
                                    )
                                    + "\n\n"
                                    + "> "
                                    + _(
                                        "Restart the Kittyflap after changing the model."
                                    ),
                                ),
                            ),
                            ui.p(
                                _(
                                    "The original models (v1 and v2) are strongly discouraged: they are extremely inaccurate, and not all Kittyhack features work with them. A model you train yourself is much more accurate."
                                ),
                                id="original_model_warning",
                                class_="kh-setting-warning",
                                style_=(
                                    None
                                    if uses_original_detection_model(
                                        CONFIG.get("TFLITE_MODEL_VERSION")
                                    )
                                    else "display: none;"
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                field_with_info(
                                    ui.input_switch(
                                        "btnUseCameraForCatDetection",
                                        _("Use camera for cat detection"),
                                        CONFIG["USE_CAMERA_FOR_CAT_DETECTION"],
                                        width="90%",
                                    ),
                                    "camera_cat_detection_info",
                                    _("Explain camera cat detection"),
                                    _(
                                        "The camera can identify your cat alongside the RFID reader."
                                    )
                                    + "  \n\n"
                                    + _(
                                        "Set the threshold with **Threshold for cat detection**."
                                    )
                                    + " "
                                    + _(
                                        "When Kittyhack identifies your cat, it unlocks the flap for entry."
                                    )
                                    + "\n\n"
                                    + _(
                                        "This feature needs a custom model trained for your cats. It does not work with the original Kittyflap models."
                                    )
                                    + "\n\n"
                                    + _(
                                        "Reliable identification needs a good model and enough light."
                                    )
                                    + " "
                                    + _(
                                        "Otherwise, Kittyhack may miss your cat or identify a similar-looking cat as yours."
                                    ),
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                field_with_info(
                                    ui.input_switch(
                                        "btnUseCameraForMotionDetection",
                                        _("Use camera for motion detection"),
                                        CONFIG["USE_CAMERA_FOR_MOTION_DETECTION"],
                                        width="90%",
                                    ),
                                    "camera_motion_detection_info",
                                    _("Explain camera motion detection"),
                                    _(
                                        "Kittyhack can use the camera instead of the outside motion sensor."
                                    )
                                    + "  \n\n"
                                    + _("**How it works:**")
                                    + "  \n"
                                    + _(
                                        "- Normally, Kittyhack waits for the outside motion sensor before checking camera pictures."
                                    )
                                    + "  \n"
                                    + _(
                                        "- When enabled, Kittyhack continuously checks camera pictures instead."
                                    )
                                    + "  \n"
                                    + _(
                                        "- A cat in the camera picture is treated like motion outside."
                                    )
                                    + "  \n\n"
                                    + _(
                                        "Set the threshold with **Threshold for cat detection**."
                                    )
                                    + "  \n\n"
                                    + _(
                                        "This can help when moving trees or people often trigger the outside motion sensor."
                                    )
                                    + "  \n\n"
                                    + _(
                                        "This feature needs a custom model trained for your cats. It does not work with the original Kittyflap models."
                                    )
                                    + "\n\n"
                                    + _(
                                        "Reliable detection needs a good model and enough light."
                                    )
                                    + " "
                                    + _(
                                        "Otherwise, Kittyhack may trigger unexpectedly or miss your cat."
                                    )
                                    + "\n\n"
                                    + _(
                                        "To protect the lock magnets, camera detection alone keeps the inside unlocked for no more than 60 seconds unless a cat passes through."
                                    )
                                    + " "
                                    + _(
                                        "A detected RFID chip or a new outside motion trigger can unlock the inside again. "
                                        "If it stays unlocked for another 60 seconds, Kittyhack pauses camera unlocking for 60 seconds "
                                        "(RFID and manual unlock still work during the cooldown)."
                                    ),
                                ),
                            ),
                        ),
                        ui.hr(),
                        ui.div(
                            ui.row(
                                ui.column(
                                    12,
                                    field_with_info(
                                        pir_for_camera_entry_switch,
                                        "require_pir_for_camera_entry_info",
                                        _("Explain outside motion confirmation"),
                                        _(
                                            "When enabled, Kittyhack unlocks the inside for camera entry only while the outside motion sensor is active."
                                        )
                                        + "  \n\n"
                                        + _(
                                            "Use this if the camera sometimes detects a cat when none is at the flap, for example because of sun glare."
                                        )
                                        + "  \n\n"
                                        + _(
                                            "Leave this off if the camera alone should be enough to unlock the inside."
                                        )
                                        + "  \n\n"
                                        + _(
                                            "Kittyhack always asks for a new confirmation after the inside has been unlocked for 60 seconds without a cat passing through. If this happens twice in a row, camera unlocking pauses for 60 seconds."
                                        ),
                                    ),
                                    ui.p(
                                        _(
                                            "This switch is available only when camera cat detection or camera motion detection is turned on."
                                        ),
                                        id="require_pir_for_camera_entry_disabled_hint",
                                        class_="kh-inline-hint",
                                        style_=None if not camera_based_entry else "display: none;",
                                    ),
                                ),
                            ),
                            id="require_pir_for_camera_entry_container",
                            class_="" if camera_based_entry else "kh-control-disabled",
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                field_with_info(
                                    ui.input_select(
                                        "txtAllowedToEnter",
                                        _("Unlock inside for"),
                                        {
                                            AllowedToEnter.ALL.value: _(
                                                "All cats (unlock for any detected motion)"
                                            ),
                                            AllowedToEnter.ALL_RFIDS.value: _(
                                                "All cats with an RFID chip"
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
                                    "allowed_to_enter_info",
                                    _("Explain entry options"),
                                    _(
                                        "- **All cats:** Any detected motion outside unlocks the flap."
                                    )
                                    + "  \n"
                                    + _(
                                        "- **All cats with an RFID chip:** Any detected RFID chip unlocks the flap."
                                    )
                                    + "  \n"
                                    + _(
                                        "- **Only registered cats:** Only cats registered in Kittyhack unlock the flap, by RFID or camera identification when enabled."
                                    )
                                    + "  \n"
                                    + _(
                                        "- **Individual settings per cat:** Choose which cats may enter under **Cats**."
                                    )
                                    + "  \n"
                                    + _(
                                        "- **No cats:** The inside stays locked."
                                    ),
                                ),
                            ),
                        ),
                        ui.row(
                            ui.column(
                                12,
                                logic_block(
                                    "btn_toggle_entry_logic",
                                    "entry_logic_hint",
                                    "entry_logic_expand",
                                    "entry_logic_flows",
                                    entry_logic_ui(
                                        _,
                                        active=config_mode(CONFIG["ALLOWED_TO_ENTER"]),
                                    ),
                                ),
                            )
                        ),
                        ui.hr(),
                        ui.row(
                            ui.column(
                                12,
                                field_with_info(
                                    ui.input_select(
                                        "btnAllowedToExit",
                                        _("Outside"),
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
                                    "allowed_to_exit_info",
                                    _("Explain exit options"),
                                    _(
                                        "- **Allow exit:** Cats may leave at any time unless you set time ranges below."
                                    )
                                    + "  \n"
                                    + _(
                                        "- **Do not allow exit:** The outside stays locked."
                                    )
                                    + "  \n"
                                    + _(
                                        "- **Individual settings per cat:** Choose which cats may leave under **Cats**. The time ranges below also apply."
                                    )
                                    + "  \n  "
                                    + _(
                                        "To use this option, register every cat with an **RFID chip**. Cats without RFID cannot leave."
                                    ),
                                ),
                            ),
                        ),
                        ui.row(
                            ui.column(
                                12,
                                logic_block(
                                    "btn_toggle_exit_logic",
                                    "exit_logic_hint",
                                    "exit_logic_expand",
                                    "exit_logic_flows",
                                    exit_logic_ui(
                                        _,
                                        active=config_mode(CONFIG["ALLOWED_TO_EXIT"]),
                                    ),
                                ),
                            ),
                        ),
                        ui.br(),
                        ui.br(),
                        ui.div(
                            ui.div(
                                info_hint(
                                    "exit_time_ranges",
                                    _("Show exit time rules"),
                                    _(
                                        "You can set up to three time ranges when your cats may leave."
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
                                        "Outside these windows, no cat may exit, even if the per‑cat setting under 'Cats' allows exit."
                                    )
                                    + "  \n"
                                    + _(
                                        "See the decision steps above for details."
                                    )
                                    + "  \n\n"
                                    + _(
                                        "Enter times in 24-hour format, such as 13:00."
                                    )
                                    + "  \n"
                                    + _(
                                        "Example: If ranges are 10:00–18:00 and it is 20:00, no cat may exit, even if that cat is allowed under 'Cats'."
                                    ),
                                ),
                                class_="kh-field-info",
                            ),
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
                            class_="kh-field",
                        ),
                        ui.hr(),
                        # TODO: Outside PIR shall not yet be configurable. Need to redesign the camera control, otherwise we will have no cat pictures at high PIR thresholds.
                        # ui.column(12, ui.input_slider("sldPirOutsideThreshold", _("Sensitivity of the motion sensor on the outside"), min=0.1, max=6, step=0.1, value=CONFIG['PIR_OUTSIDE_THRESHOLD'])),
                        ui.row(
                            ui.column(
                                12,
                                field_with_info(
                                    ui.input_slider(
                                        "sldPirInsideThreshold",
                                        _("Inside motion sensor delay (seconds)"),
                                        min=0.1,
                                        max=6,
                                        step=0.1,
                                        value=CONFIG["PIR_INSIDE_THRESHOLD"],
                                        width="90%",
                                    ),
                                    "pir_inside_threshold_info",
                                    _("Explain the inside motion sensor delay"),
                                    _(
                                        "A lower value reacts faster but may trigger by mistake more often. "
                                        "A higher value reacts more slowly but is less likely to trigger by mistake."
                                    )
                                    + "  \n"
                                    + _(
                                        "The default works well for most homes."
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
                                field_with_info(
                                    ui.input_switch(
                                        "btnImmediateLockAfterPassage",
                                        _("Lock immediately after passage"),
                                        CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"],
                                        width="90%",
                                    ),
                                    "immediate_lock_after_passage_info",
                                    _("Explain immediate locking"),
                                    _(
                                        "The flap locks and finishes the event as soon as motion is detected on the other side, showing that the cat has passed through."
                                    )
                                    + "  \n\n"
                                    + _(
                                        "This can help with fast cats and makes it harder for several cats to use the flap one after another."
                                    )
                                    + "  \n\n"
                                    + _(
                                        "Leave this off if your cat is cautious or needs several attempts. The flap could otherwise lock while your cat is still trying to pass."
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
                    _("Live view"),
                    _("Choose how often the live camera picture refreshes."),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(
                                4,
                                ui.input_select(
                                    "numLiveViewUpdateInterval",
                                    _("Live view refresh interval"),
                                    {
                                        _("Refresh the live view every"): {
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
                                        "Frequent updates may slow Kittyhack, especially when several people are connected. Intervals below one second need a fast, stable WLAN connection."
                                    )
                                    + "  \n"
                                    + _(
                                        "This changes only the live view and does not affect detection."
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
                    _("Pictures"),
                    _(
                        "Choose how many pictures Kittyhack keeps and shows for each event."
                    ),
                    ui.div(
                        ui.br(),
                        ui.row(
                            ui.column(
                                4,
                                ui.input_numeric(
                                    "numMaxPhotosCount",
                                    _(
                                        "Maximum pictures to keep"
                                    ),
                                    CONFIG["MAX_PHOTOS_COUNT"],
                                    min=100,
                                ),
                            ),
                            ui.column(
                                8,
                                ui.markdown(
                                    _(
                                        "When the limit is reached, Kittyhack deletes the oldest pictures."
                                    )
                                    + "  \n"
                                    + _(
                                        "The suitable limit depends on the Kittyflap storage size."
                                    )
                                    + "  \n"
                                    + _(
                                        "As a guide, 1,000 pictures use about 200 MB. Check the available space under **Info**."
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
                                    _("Maximum pictures per event with RFID"),
                                    CONFIG["MAX_PICTURES_PER_EVENT_WITH_RFID"],
                                    min=0,
                                ),
                            ),
                            ui.column(
                                8,
                                ui.markdown(
                                    _(
                                        "Maximum number of pictures kept for an event when an RFID chip is detected."
                                    )
                                    + "  \n"
                                    + _(
                                        "Prey detection still checks every picture in the event. This setting only limits how many pictures are kept."
                                    )
                                    + "  \n"
                                    + _(
                                        "Set this to 0 to keep no event. Keeping many pictures may slow Kittyhack."
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
                                    _("Maximum pictures per event without RFID"),
                                    CONFIG["MAX_PICTURES_PER_EVENT_WITHOUT_RFID"],
                                    min=0,
                                ),
                            ),
                            ui.column(
                                8,
                                ui.markdown(
                                    _(
                                        "Maximum number of pictures kept for an event when no RFID chip is detected."
                                    )
                                    + "  \n"
                                    + _(
                                        "Prey detection still checks every picture in the event. This setting only limits how many pictures are kept."
                                    )
                                    + " \n"
                                    + _(
                                        "Set this to 0 to keep no event. Keeping many pictures may slow Kittyhack."
                                    )
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        ui.hr(),
                        ui.br(),
                        full_screen=False,
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),
                # --- Home Assistant / MQTT settings ---
                collapsible_section(
                    "home_assistant_settings",
                    _("Home Assistant"),
                    _("Connect Kittyhack to Home Assistant with MQTT."),
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
                                    _("MQTT broker address"),
                                    value=CONFIG.get("MQTT_BROKER_ADDRESS", ""),
                                    placeholder=_("e.g. 192.168.1.10"),
                                    width="100%",
                                ),
                            ),
                            ui.column(
                                4,
                                ui.input_numeric(
                                    "numMqttBrokerPort",
                                    _("MQTT broker port"),
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
                                    _("MQTT username"),
                                    value=CONFIG.get("MQTT_USERNAME", ""),
                                    placeholder=_("MQTT username"),
                                    width="100%",
                                ),
                            ),
                            ui.column(
                                6,
                                ui.input_password(
                                    "txtMqttPassword",
                                    _("MQTT password"),
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
                                ui.h5(_("Home Assistant card example")),
                                ui.markdown(
                                    _(
                                        "Copy this configuration to add a dashboard card in Home Assistant:"
                                    )
                                ),
                                ui.markdown(
                                    "```yaml\n"
                                    + "# "
                                    + _("Home Assistant dashboard card")
                                    + "\n"
                                    + "type: vertical-stack\n"
                                    + "title: "
                                    + _("Kittyhack cat flap")
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
                                    + _("Last event")
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
                                    + _("Flap locks")
                                    + "\n"
                                    + "  - type: entities\n"
                                    + "    title: "
                                    + _("Settings")
                                    + "\n"
                                    + "    show_header_toggle: false\n"
                                    + "    entities:\n"
                                    + "      - entity: select.{}_allow_enter\n".format(
                                        CONFIG["MQTT_DEVICE_ID"]
                                    )
                                    + "        name: "
                                    + _("Unlock inside for")
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
                    _("Advanced"),
                    _("Configure network identity, logging, and performance."),
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
                                        "The hostname identifies your Kittyflap on the network. Choose a unique name for easier access. Your router must support mDNS or Avahi."
                                    )
                                    + "  \n"
                                    + _(
                                        "Allowed characters: `a-z`, `A-Z`, `0-9` and `-`."
                                    )
                                    + "  \n"
                                    + "> "
                                    + _(
                                        "Restart the Kittyflap after changing the hostname."
                                    )
                                    + "\n\n"
                                    + _(
                                        "Open the Kittyflap in your browser at:"
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
                                        "`INFO` is the default log level for everyday use. Use `DEBUG` only while troubleshooting."
                                    )
                                ),
                                style_="color: grey;",
                            ),
                        ),
                        ui.hr(),
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
                                            "Limits how often the detection model runs to reduce CPU use in remote mode."
                                        )
                                        + "\n\n> "
                                        + (
                                            _(
                                                "You can change this setting only in remote mode."
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
                                            "After a restart, a previously remote-controlled Kittyflap waits this long for the remote device before starting Kittyhack locally."
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
                                    _("Restart interrupted IP camera streams"),
                                    CONFIG["RESTART_IP_CAMERA_STREAM_ON_FAILURE"],
                                ),
                            ),
                            ui.column(
                                12,
                                ui.markdown(
                                    _(
                                        "Kittyhack restarts the external camera stream when it detects damaged pictures."
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
                                            "WLAN monitoring is not available in remote mode."
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
                                        _("Monitor the WLAN connection"),
                                        CONFIG["WLAN_WATCHDOG_ENABLED"],
                                    ),
                                ),
                                ui.column(
                                    12,
                                    ui.markdown(
                                        _(
                                            "Kittyhack reconnects the WLAN if the connection fails. If it cannot reconnect, the Kittyflap restarts."
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
                                    field_with_info(
                                        ui.input_select(
                                            "inference_device",
                                            _("Detection device (experimental)"),
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
                                        "inference_device_info",
                                        _("Show detection device details"),
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
                                            "This is experimental. It can make detection faster and reduce CPU use, but unsupported hardware or model formats may be unstable."
                                        )
                                        + "\n\n"
                                        + _(
                                            "Saving a different device installs the packages it needs. NVIDIA CUDA downloads a large PyTorch build. Intel GPU uses OpenVINO and exports the model once if that file is not there yet."
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
                                            "The Kittyflap uses the **CPU** for detection. GPU detection is available only in remote mode."
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
                                        "When enabled, the RFID reader stays off after motion is detected. "
                                        "Use this only to troubleshoot hardware faults or restarts caused by low voltage. "
                                        "Choose **Unlock inside for: All cats** or use camera cat detection instead."
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
                    _("Remote mode"),
                    _("Learn how remote mode works and how to set it up."),
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
                                    ui.markdown(remote_mode_doc_main),
                                    class_="kh-remote-doc",
                                ),
                                ui.div(
                                    ui.tags.button(
                                        ui.tags.span(_("Show diagrams")),
                                        id="btn_toggle_remote_diagrams",
                                        type="button",
                                        class_="btn btn-default kh-logic-btn",
                                        **{
                                            "data-show-label": _("Show diagrams"),
                                            "data-hide-label": _("Hide diagrams"),
                                        },
                                    ),
                                    ui.div(
                                        ui.markdown(remote_mode_doc_diagrams),
                                        id="remote_diagrams_expand",
                                        class_="kh-remote-doc",
                                        style_="display:none; margin-top:0.75rem;",
                                    ),
                                    style_="margin-top:1.25rem;",
                                )
                                if remote_mode_doc_diagrams
                                else ui.div(),
                            ),
                        ),
                        class_="generic-container align-left",
                        style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                    ),
                ),
                ui.tags.div(
                    {
                        "id": "kh_config_leave_i18n",
                        "style": "display:none;",
                        "data-msg": _(
                            "Your settings have unsaved changes. Leave without saving?"
                        ),
                    }
                ),
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

    @reactive.Effect
    @reactive.event(input.btn_reset_muted_notifications)
    def on_reset_muted_notifications():
        count = UserNotifications.clear_muted()
        if count:
            ui.notification_show(
                _("Silenced notifications were reset. These warnings can appear again."),
                duration=6,
                type="message",
            )
            reload_trigger_config.set(reload_trigger_config.get() + 1)
        else:
            ui.notification_show(
                _("There are no silenced notifications."),
                duration=5,
                type="message",
            )

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
    async def on_save_kittyhack_config():
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
        try:
            prev_max_photos_count = int(CONFIG.get("MAX_PHOTOS_COUNT") or 0)
        except Exception:
            prev_max_photos_count = 0
        prev_suggest_pir = should_suggest_pir_second_factor(
            bool(CONFIG.get("REQUIRE_OUTSIDE_PIR_FOR_CAMERA_ENTRY", False)),
            bool(CONFIG.get("USE_CAMERA_FOR_MOTION_DETECTION", False)),
            bool(CONFIG.get("USE_CAMERA_FOR_CAT_DETECTION", False)),
        )
        prev_original_model = uses_original_detection_model(
            CONFIG.get("TFLITE_MODEL_VERSION")
        )

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
                    _("Exit time ranges: ")
                    + _("Enter the time in HH:MM format.")
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
                        _("Exit time ranges: ")
                        + _("Time must be between 00:00 and 23:59.")
                        + "\n"
                        + _("Changes were not saved."),
                        duration=10,
                        type="error",
                    )
                    return False
            except ValueError:
                ui.notification_show(
                    _("Exit time ranges: ")
                    + _("Enter a valid time.")
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
                    _("Update source: ")
                    + _(
                        "Use 'owner/repo', 'owner/repo@branch-or-tag', or 'owner:branch'."
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
                        _("Update source: ")
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
                    _("IP camera URL: ")
                    + _(
                        "Enter a valid stream URL starting with rtsp://, http://, https://, rtmp://, udp://, tcp://, or file://."
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
                        "Install `ffmpeg` manually, then try again. Changes were not saved."
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

        previous_inference_device = CONFIG.get("INFERENCE_DEVICE", "cpu")
        CONFIG["INFERENCE_DEVICE"] = (
            input.inference_device() if is_remote_mode() else "cpu"
        )

        CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = input.btnUseCameraForCatDetection()
        CONFIG["CAT_THRESHOLD"] = float(input.sldCatThreshold())
        CONFIG["USE_CAMERA_FOR_MOTION_DETECTION"] = (
            input.btnUseCameraForMotionDetection()
        )
        try:
            CONFIG["REQUIRE_OUTSIDE_PIR_FOR_CAMERA_ENTRY"] = bool(
                input.btnRequireOutsidePirForCameraEntry()
            )
        except Exception:
            CONFIG["REQUIRE_OUTSIDE_PIR_FOR_CAMERA_ENTRY"] = False
        CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter(input.txtAllowedToEnter())
        CONFIG["LIVE_VIEW_REFRESH_INTERVAL"] = float(input.numLiveViewUpdateInterval())
        from src.baseconfig import AllowedToExit as ATE

        CONFIG["ALLOWED_TO_EXIT"] = ATE(input.btnAllowedToExit())
        CONFIG["PERIODIC_VERSION_CHECK"] = input.btnPeriodicVersionCheck()
        _new_update_mode = str(input.update_repository_mode() or "standard").strip().lower()
        if _new_update_mode not in {"standard", "beta", "custom"}:
            _new_update_mode = "standard"
        CONFIG["UPDATE_REPOSITORY_MODE"] = _new_update_mode
        # Use the normalized value computed during validation above, or keep the
        # raw input when mode is 'standard'/'beta' (UPDATE_REPOSITORY is ignored there).
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
            CONFIG["USE_ALL_CORES_FOR_IMAGE_PROCESSING"] = False
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
                _("These settings cannot be used together: ")
                + _(
                    "Camera motion detection needs a custom trained model and does not work with an original Kittyflap model."
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
                _("These settings cannot be used together: ")
                + _(
                    "Camera cat detection needs a custom trained model and does not work with an original Kittyflap model."
                )
                + "\n"
                + _('The setting "{}" was disabled.').format(
                    _("Use camera for cat detection")
                ),
                duration=None,
                type="warning",
            )

        if inference_device_changed:
            model_root = ""
            model_imgsz = 320
            if CONFIG.get("YOLO_MODEL"):
                model_root = YoloModel.get_model_path(CONFIG["YOLO_MODEL"]) or ""
                try:
                    model_imgsz = int(
                        YoloModel.get_model_image_size(CONFIG["YOLO_MODEL"]) or 320
                    )
                except Exception:
                    model_imgsz = 320
            phase_detail = {
                "cuda": _("Installing the NVIDIA CUDA packages."),
                "export": _("Exporting the model for this device."),
                "torch_cleanup": _("Removing the temporary PyTorch packages."),
                "openvino": _("Installing OpenVINO."),
                "opencv": _("Installing OpenCV."),
            }
            install_message = _(
                "Installing packages for the new inference device. This can take several minutes."
            )
            with ui.Progress() as install_progress:
                install_progress.set(message=install_message)
                # Let the progress panel reach the browser before the install starts.
                await asyncio.sleep(0)
                loop = asyncio.get_running_loop()
                reported = {"at": 0.0, "phase": ""}

                def _report(phase: str, detail: str = "") -> None:
                    now = time.monotonic()
                    if phase == reported["phase"] and (now - reported["at"]) < 0.4:
                        return
                    reported["at"] = now
                    reported["phase"] = phase
                    text = (detail or phase_detail.get(phase) or "").strip()
                    if len(text) > 160:
                        text = text[:157] + "..."

                    def _apply(shown: str = text) -> None:
                        if install_progress._closed:
                            return
                        install_progress.set(
                            message=install_message,
                            detail=shown or None,
                        )

                    loop.call_soon_threadsafe(_apply)

                prepare_ok, prepare_error = await asyncio.to_thread(
                    DependencyInstaller.prepare_inference_device,
                    str(CONFIG.get("INFERENCE_DEVICE") or "cpu"),
                    model_root,
                    model_imgsz,
                    _report,
                )
            if not prepare_ok:
                CONFIG["INFERENCE_DEVICE"] = previous_inference_device
                prepare_messages = {
                    "cuda_install_failed": _(
                        "The NVIDIA CUDA packages could not be installed. Changes were not saved."
                    ),
                    "openvino_install_failed": _(
                        "The OpenVINO package could not be installed. Changes were not saved."
                    ),
                    "model_pt_missing": _(
                        "This model has no model.pt file, so it cannot be prepared for that device. Changes were not saved."
                    ),
                    "export_failed": _(
                        "The model could not be exported for this device. Changes were not saved."
                    ),
                    "opencv_install_failed": _(
                        "OpenCV could not be installed for this device. Changes were not saved."
                    ),
                }
                ui.notification_show(
                    prepare_messages.get(
                        prepare_error,
                        _("Could not switch the inference device. Changes were not saved."),
                    ),
                    duration=None,
                    type="error",
                )
                return

        if save_config():
            ui.notification_show(
                _("Settings saved."),
                duration=5,
                type="message",
            )

            # Apply photo retention immediately when the limit was reduced.
            try:
                new_max_photos_count = int(CONFIG.get("MAX_PHOTOS_COUNT") or 0)
            except Exception:
                new_max_photos_count = prev_max_photos_count
            if (
                new_max_photos_count > 0
                and new_max_photos_count < prev_max_photos_count
            ):
                try:
                    purge_result = EventsRepo.purge_excess_photos(
                        CONFIG["KITTYHACK_DATABASE_PATH"],
                        max_count=new_max_photos_count,
                    )
                    if purge_result.success:
                        try:
                            purged_count = int(purge_result.message or "0")
                        except Exception:
                            purged_count = 0
                        if purged_count > 0:
                            ui.notification_show(
                                _(
                                    "Deleted {} oldest photos to apply the new retention limit."
                                ).format(purged_count),
                                duration=10,
                                type="message",
                            )
                            try:
                                startup.free_disk_space = (
                                    SystemInfo.get_free_disk_space()
                                )
                            except Exception:
                                pass
                    else:
                        ui.notification_show(
                            _(
                                "Failed to apply the new photo retention limit: {}"
                            ).format(purge_result.message),
                            duration=12,
                            type="error",
                        )
                except Exception as e:
                    logging.error(
                        f"[CONFIG] Failed to purge excess photos after lowering MAX_PHOTOS_COUNT: {e}"
                    )
                    ui.notification_show(
                        _(
                            "Failed to apply the new photo retention limit: {}"
                        ).format(e),
                        duration=12,
                        type="error",
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
                        "Restart the Kittyflap under **System** to use the new language."
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
                or rfid_state_changed
            ):
                ui.modal_remove()
                ui.modal_show(
                    ui.modal(
                        _(
                            "Restart the Kittyflap to apply these changes. Restart now?"
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

                # When the update source changed: only refresh the channel's latest version
            # and point the user to the INFO tab. Do **not** start an update here —
            # the user decides on INFO whether/when to install.
            _new_update_mode_saved = (
                str(CONFIG.get("UPDATE_REPOSITORY_MODE") or "standard").strip().lower()
            )
            update_repo_changed = (
                _prev_update_mode != _new_update_mode_saved
                or _prev_update_repo
                != str(CONFIG.get("UPDATE_REPOSITORY") or "").strip()
            )
            if update_repo_changed:
                latest_after_source_change = "unknown"
                try:
                    latest_after_source_change = (
                        Versioning.read_latest_kittyhack_version(timeout=5)
                    )
                    CONFIG["LATEST_VERSION"] = latest_after_source_change
                except Exception as e:
                    logging.warning(
                        f"[UPDATE] Failed to refresh LATEST_VERSION after update-source change: {e}"
                    )
                    latest_after_source_change = str(
                        CONFIG.get("LATEST_VERSION") or "unknown"
                    )
                try:
                    ctx.reload_trigger_info.set(ctx.reload_trigger_info.get() + 1)
                except Exception:
                    pass

                if not restart_modal_shown:
                    beta_channel_switch = (
                        _prev_update_mode == "beta"
                        or _new_update_mode_saved == "beta"
                    )
                    modal_parts = [
                        _(
                            "The update source changed. Kittyhack is checking its latest version."
                        ),
                    ]
                    if (
                        latest_after_source_change
                        and latest_after_source_change != "unknown"
                    ):
                        modal_parts.extend(
                            [
                                "\n\n",
                                _("Latest version for the new source: **{}**").format(
                                    latest_after_source_change
                                ),
                            ]
                        )
                    else:
                        modal_parts.extend(
                            [
                                "\n\n",
                                _(
                                    "The latest version for the new source could not be determined yet. You can retry the check on the **INFO** tab."
                                ),
                            ]
                        )
                    if beta_channel_switch:
                        modal_parts.extend(
                            [
                                "\n\n",
                                _(
                                    "Back up your database before switching between Standard and Beta."
                                ),
                            ]
                        )
                        if (
                            _prev_update_mode == "beta"
                            and _new_update_mode_saved != "beta"
                        ):
                            modal_parts.extend(
                                [
                                    "\n\n",
                                    _(
                                        "Switching from beta back to the latest release may work, "
                                        "but it is not recommended."
                                    ),
                                ]
                            )
                    modal_parts.extend(
                        [
                            "\n\n",
                            _(
                                    "If another version is available, review and install it under **Info**."
                            ),
                        ]
                    )
                    ui.modal_remove()
                    ui.modal_show(
                        ui.modal(
                            ui.markdown("".join(modal_parts)),
                            title=_("Update source changed"),
                            easy_close=True,
                            footer=ui.div(
                                ui.input_action_button(
                                    "btn_modal_cancel", _("Close")
                                ),
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
            try:
                queued_pir_tip = sync_pir_second_factor_suggestion()
                queued_model_tip = (
                    sync_original_model_notice() if selected_model_changed else False
                )
                if (queued_pir_tip and not prev_suggest_pir) or (
                    queued_model_tip and not prev_original_model
                ):
                    show_notes = getattr(ctx, "show_user_notifications", None)
                    if callable(show_notes):
                        show_notes()
            except Exception as e:
                logging.warning(
                    f"[CONFIG] Failed to sync settings hints: {e}"
                )
            # Trigger UI components that depend on these settings to re-render
            reload_trigger_config.set(reload_trigger_config.get() + 1)

        else:
            ui.notification_show(
                _("Kittyhack could not save the settings."),
                duration=10,
                type="error",
            )
