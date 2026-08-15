"""Event detail modal (Shiny module)."""

import os
import pandas as pd
import time as tm
from shiny import render, ui, reactive, module
import logging
from faicons import icon_svg
import asyncio
import tarfile
import json
import zipfile
import shutil
from typing import List
from src.baseconfig import CONFIG, set_language
from src.helper import DateTimeUtil
from src.system import LabelStudioInstall
from src.database import (
    DatabaseCore,
    EventsRepo,
    ORIGINAL_IMAGE_DIR,
    ReturnDataPhotosDB,
    THUMBNAIL_DIR,
)
from src.camera import DetectedObject
from src.paths import pictures_original_dir
from src.labelstudio_api import upload_image_to_labelstudio_project
from src.server_ui.state import reload_trigger_photos

_ = set_language(CONFIG["LANGUAGE"])


@module.ui
def btn_show_event():
    """UI fragment: magnifying-glass button that opens the event detail modal."""
    return ui.input_action_button(
        id=f"btn_show_event",
        label="",
        icon=icon_svg("magnifying-glass", margin_left="0", margin_right="0"),
        class_="btn-icon-square btn-outline-secondary",
    )


@module.server
def show_event_server(input, output, session, block_id: int):
    """Server logic for the event detail modal (playback data, download, delete)."""

    # Server-side state: kept for download/delete handlers.
    # All playback, scrubbing and overlay rendering is done client-side (event-modal.js).
    pictures: list[int] = []
    timestamps = []
    photo_ids: list[int | None] = []
    # Store lists of DetectedObjects, one list per event
    event_datas: List[List[DetectedObject]] = []
    fallback_mode = [False]
    bundle_url = [
        None
    ]  # optional: /thumb/... tar that contains all thumbnails for this event
    aspect_style = [""]  # CSS var: --kh-event-aspect: w / h
    event_effective_fps = [None]  # float | None

    def _event_bundle_rel_url(block_id: int) -> str:
        # Served via static_assets mapping in app.py: "/thumb" -> THUMBNAIL_DIR.
        # We store bundles in a subfolder to avoid collisions with <id>.jpg.
        # Use a plain .tar
        return f"/thumb/bundles/event_{int(block_id)}.tar"

    def _event_bundle_versioned_url(block_id: int, bundle_path: str) -> str:
        """Return a cache-busting bundle URL based on the file mtime.

        Important: Browsers may reuse cached responses for the same URL even if the
        underlying file was deleted/recreated. Adding a version query parameter ensures
        a rebuilt bundle is fetched again.
        """
        try:
            v = int(float(os.path.getmtime(bundle_path)) * 1000)
        except Exception:
            v = int(tm.time() * 1000)
        return f"{_event_bundle_rel_url(block_id)}?v={v}"

    def _event_bundle_fs_path(block_id: int) -> str:
        bundles_dir = os.path.join(THUMBNAIL_DIR, "bundles")
        os.makedirs(bundles_dir, exist_ok=True)
        return os.path.join(bundles_dir, f"event_{int(block_id)}.tar")

    def _ensure_event_bundle_file(block_id: int, pids: list[int]) -> str | None:
        """Create/update a tar containing all thumbnail JPGs for an event.

        Returns the URL path (under /thumb) or None if creation fails.
        """
        try:
            pids_int = [int(x) for x in (pids or [])]
        except Exception:
            pids_int = []

        if not pids_int or len(pids_int) < 2:
            return None

        bundle_path = _event_bundle_fs_path(block_id)

        # Rebuild bundle only if missing or older than any contained thumbnail.
        newest_thumb_mtime = 0.0
        thumb_paths: list[tuple[int, str]] = []
        for pid in pids_int:
            try:
                thumb_path = os.path.join(THUMBNAIL_DIR, f"{pid}.jpg")
                if not os.path.exists(thumb_path):
                    # Generate if missing (legacy rows).
                    EventsRepo.get_thubmnail_by_id(
                        database=CONFIG["KITTYHACK_DATABASE_PATH"], photo_id=pid
                    )
                if os.path.exists(thumb_path):
                    thumb_paths.append((pid, thumb_path))
                    newest_thumb_mtime = max(
                        newest_thumb_mtime, float(os.path.getmtime(thumb_path))
                    )
            except Exception:
                continue

        if not thumb_paths:
            return None

        try:
            if os.path.exists(bundle_path):
                try:
                    if (
                        float(os.path.getmtime(bundle_path)) >= newest_thumb_mtime
                        and os.path.getsize(bundle_path) > 0
                    ):
                        return _event_bundle_versioned_url(block_id, bundle_path)
                except Exception:
                    pass
        except Exception:
            pass

        tmp_path = bundle_path + ".tmp"
        try:
            # Build tar: entries are <pid>.jpg. Keep it simple for the JS tar parser.
            with tarfile.open(tmp_path, mode="w") as tf:
                for pid, thumb_path in thumb_paths:
                    try:
                        tf.add(thumb_path, arcname=f"{pid}.jpg", recursive=False)
                    except Exception:
                        continue

            # Atomic-ish replace
            try:
                os.replace(tmp_path, bundle_path)
            except Exception:
                shutil.move(tmp_path, bundle_path)

            return _event_bundle_versioned_url(block_id, bundle_path)
        except Exception as e:
            logging.debug(f"Failed creating event bundle for block_id={block_id}: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return None

    @render.ui
    @reactive.effect
    @reactive.event(input.btn_show_event)
    async def show_event():
        logging.info(f"Show event with block_id {block_id}")
        picture_type = ReturnDataPhotosDB.all_original_image
        blob_picture = "original_image"

        # FALLBACK: The event_text column was added in version 1.4.0. If it is not present, show the "modified_image" with baked-in event data
        event = EventsRepo.db_get_photos_by_block_id(
            CONFIG["KITTYHACK_DATABASE_PATH"],
            block_id,
            ReturnDataPhotosDB.all_except_photos,
        )
        if event.empty:
            # All frames may have been deleted; keep state empty and avoid index errors.
            pictures.clear()
            timestamps.clear()
            event_datas.clear()
            photo_ids.clear()
            bundle_url[0] = None
            aspect_style[0] = ""
            return

        # Read effective FPS (capture/playback speed) from DB if available.
        event_effective_fps[0] = None
        try:
            if "effective_fps" in event.columns:
                for __, r in event.iterrows():
                    v = r.get("effective_fps")
                    if v is None or pd.isna(v):
                        continue
                    try:
                        fv = float(v)
                    except Exception:
                        continue
                    if fv > 0:
                        event_effective_fps[0] = fv
                        break
        except Exception:
            event_effective_fps[0] = None

        # Compute aspect ratio for stable initial modal size (before first image loads).
        try:
            w = None
            h = None
            if "img_width" in event.columns and "img_height" in event.columns:
                try:
                    for __, r in event.iterrows():
                        rw = r.get("img_width")
                        rh = r.get("img_height")
                        if rw is not None and rh is not None:
                            try:
                                rw_i = int(rw)
                                rh_i = int(rh)
                            except Exception:
                                continue
                            if rw_i > 0 and rh_i > 0:
                                w, h = rw_i, rh_i
                                break
                except Exception:
                    pass

            # Fallback: infer from the first thumbnail file
            if (w is None or h is None) and "id" in event.columns:
                try:
                    pid0 = int(event.iloc[0]["id"])
                    thumb_path0 = os.path.join(THUMBNAIL_DIR, f"{pid0}.jpg")
                    if not os.path.exists(thumb_path0):
                        try:
                            EventsRepo.get_thubmnail_by_id(
                                database=CONFIG["KITTYHACK_DATABASE_PATH"],
                                photo_id=pid0,
                            )
                        except Exception:
                            pass
                    if os.path.exists(thumb_path0):
                        with open(thumb_path0, "rb") as f:
                            s = EventsRepo.get_jpeg_size(f.read(256 * 1024))
                        if s:
                            w, h = int(s[0]), int(s[1])
                    # As a last resort, try the original image file
                    if w is None or h is None:
                        orig_path0 = os.path.join(ORIGINAL_IMAGE_DIR, f"{pid0}.jpg")
                        if os.path.exists(orig_path0):
                            with open(orig_path0, "rb") as f:
                                s = EventsRepo.get_jpeg_size(f.read(256 * 1024))
                            if s:
                                w, h = int(s[0]), int(s[1])
                except Exception:
                    pass

            if w and h:
                aspect_style[0] = f"--kh-event-aspect: {w} / {h};"
                try:
                    EventsRepo.update_image_dimensions_for_block(
                        CONFIG["KITTYHACK_DATABASE_PATH"], block_id, w, h
                    )
                except Exception:
                    pass
            else:
                aspect_style[0] = ""
        except Exception:
            aspect_style[0] = ""

        if not event.iloc[0]["event_text"]:
            fallback_mode[0] = True
            if CONFIG["SHOW_IMAGES_WITH_OVERLAY"]:
                blob_picture = "modified_image"
                picture_type = ReturnDataPhotosDB.all_modified_image

        event = EventsRepo.db_get_photos_by_block_id(
            CONFIG["KITTYHACK_DATABASE_PATH"], block_id, picture_type
        )

        # Clear modal state
        pictures.clear()
        timestamps.clear()
        event_datas.clear()
        photo_ids.clear()

        # Iterate over the rows and encode the pictures
        async def process_event_row(row):
            try:
                event_text = row["event_text"]

                try:
                    pid = int(row["id"])
                except Exception:
                    return

                # Ensure a thumbnail file exists on disk (generate if missing).
                # Avoid reading/encoding image bytes here.
                try:
                    thumb_path = os.path.join(THUMBNAIL_DIR, f"{pid}.jpg")
                    if not os.path.exists(thumb_path):
                        # Will generate and persist to THUMBNAIL_DIR if needed.
                        EventsRepo.get_thubmnail_by_id(
                            database=CONFIG["KITTYHACK_DATABASE_PATH"], photo_id=pid
                        )
                except Exception:
                    pass

                pictures.append(pid)
                photo_ids.append(pid)

                # Convert the timestamp to the local timezone and format it
                try:
                    timestamp = pd.to_datetime(row["created_at"])
                    timestamps.append(
                        timestamp.tz_convert(CONFIG["TIMEZONE"]).strftime(
                            "%Y-%m-%d %H:%M:%S.%f"
                        )[:-3]
                    )
                except Exception as e:
                    logging.error(f"Failed to process timestamp: {e}")
                    timestamps.append("")

                try:
                    if event_text:
                        event_datas.append(EventsRepo.read_event_from_json(event_text))
                    else:
                        event_datas.append([])
                except Exception as e:
                    logging.error(f"Failed to parse event data: {e}")
                    event_datas.append([])
            except Exception as e:
                logging.error(f"Failed to encode picture: {e}")

        # Process the event rows asynchronously
        await asyncio.gather(*(process_event_row(row) for x, row in event.iterrows()))

        # Sort the timestamps, picture IDs, and event_datas lists by timestamps
        if len(timestamps) > 0:
            sorted_data = sorted(
                zip(timestamps, pictures, event_datas, photo_ids), key=lambda x: x[0]
            )
            timestamps[:], pictures[:], event_datas[:], photo_ids[:] = zip(*sorted_data)
            # zip() returns tuples
            pictures[:] = list(pictures)
            photo_ids[:] = list(photo_ids)
            event_datas[:] = list(event_datas)
            timestamps[:] = list(timestamps)

        # Legacy fallback: if no effective_fps stored, approximate it from the event's frame timestamps.
        if event_effective_fps[0] is None:
            try:
                ts = pd.to_datetime(event["created_at"], errors="coerce")
                ts = ts.dropna()
                if len(ts) >= 2 and len(pictures) >= 2:
                    span = (ts.max() - ts.min()).total_seconds()
                    if span and span > 0:
                        fps = float(len(pictures) - 1) / float(span)
                        if fps > 0:
                            # Keep within the same bounds as the frontend.
                            fps = max(0.1, min(30.0, fps))
                            event_effective_fps[0] = fps
            except Exception:
                pass

        # Optional optimization: build a single tar containing all thumbnails.
        # The browser can fetch & unpack once and then swap <img> sources to blob: URLs.
        try:
            bundle_url[0] = await asyncio.to_thread(
                _ensure_event_bundle_file, block_id, list(pictures)
            )
        except Exception:
            bundle_url[0] = None

        # ---- Build frames JSON for the client-side player ----
        def _format_event_modal_ts(ts_value):
            if not ts_value:
                return ""

            ts_text = str(ts_value).strip()
            time_part = ts_text.split(" ", 1)[1] if " " in ts_text else ts_text

            if "." not in time_part:
                return time_part

            base, frac = time_part.split(".", 1)
            frac = frac.strip()
            if not frac:
                return base

            return f"{base}.{frac[:1]}"

        frames_json = []
        for i in range(len(pictures)):
            frame_obj = {
                "pid": pictures[i],
                "ts": _format_event_modal_ts(
                    timestamps[i] if i < len(timestamps) else ""
                ),
                "objects": [],
            }
            if i < len(event_datas):
                for dobj in event_datas[i]:
                    obj_name = (dobj.object_name or "").strip()
                    if obj_name.lower() == "false-accept":
                        continue
                    frame_obj["objects"].append(
                        {
                            "x": round(dobj.x, 2),
                            "y": round(dobj.y, 2),
                            "w": round(dobj.width, 2),
                            "h": round(dobj.height, 2),
                            "name": obj_name,
                            "prob": round(dobj.probability, 1)
                            if dobj.probability
                            else 0,
                        }
                    )
            frames_json.append(frame_obj)

        player_data = json.dumps(
            {
                "frames": frames_json,
                "mouseThreshold": float(CONFIG.get("MOUSE_THRESHOLD", 50)),
                "overlayInitial": bool(
                    int(CONFIG.get("SHOW_IMAGES_WITH_OVERLAY", True))
                )
                and not fallback_mode[0],
                "fps": float(event_effective_fps[0]) if event_effective_fps[0] else 4.0,
                "fallbackMode": fallback_mode[0],
                "blockId": block_id,
            }
        )

        # Generate SVG icons for static control buttons (not Shiny action buttons)
        prev_icon_html = str(
            icon_svg("backward-step", margin_left="0", margin_right="0")
        )
        play_icon_html = str(icon_svg("play", margin_left="0", margin_right="0"))
        pause_icon_html = str(icon_svg("pause", margin_left="0", margin_right="0"))
        next_icon_html = str(
            icon_svg("forward-step", margin_left="0", margin_right="0")
        )
        overlay_on_icon_html = str(
            icon_svg("border-all", margin_left="0", margin_right="0")
        )
        overlay_off_icon_html = str(
            icon_svg("border-none", margin_left="0", margin_right="0")
        )

        initial_overlay = (
            bool(int(CONFIG.get("SHOW_IMAGES_WITH_OVERLAY", True)))
            and not fallback_mode[0]
        )

        nav_html = f'''
        <div class="event-modal-toolbar-nav">
            <button type="button" class="btn btn-default btn-icon-square btn-outline-secondary action-button"
                    data-action="prev" title="{_("Previous frame")}">
                {prev_icon_html}
            </button>
            <button type="button" class="btn btn-default btn-icon-square btn-outline-secondary action-button"
                    data-action="play-pause" title="{_("Play/Pause")}">
                <span class="kh-icon-play" {"" if len(pictures) <= 1 else 'style="display:none"'}>{play_icon_html}</span>
                <span class="kh-icon-pause" {'style="display:none"' if len(pictures) <= 1 else ""}>{pause_icon_html}</span>
            </button>
            <button type="button" class="btn btn-default btn-icon-square btn-outline-secondary action-button"
                    data-action="next" title="{_("Next frame")}">
                {next_icon_html}
            </button>
        </div>
        '''

        overlay_btn_html = f'''
        <button type="button" class="btn btn-default btn-icon-square btn-outline-secondary action-button"
                data-action="toggle-overlay" title="{_("Toggle overlay for detected objects")}"
                {"disabled" if fallback_mode[0] else ""}
                style="{"opacity:0.5" if fallback_mode[0] else ""}">
            <span class="kh-icon-overlay-on" {"" if initial_overlay else 'style="display:none"'}>{overlay_on_icon_html}</span>
            <span class="kh-icon-overlay-off" {'style="display:none"' if initial_overlay else ""}>{overlay_off_icon_html}</span>
        </button>
        '''

        # Shiny module namespace for JS → Python communication
        ns_frame_idx = session.ns("client_frame_idx")

        ui.modal_show(
            ui.modal(
                ui.card(
                    ui.div(
                        ui.tags.script(
                            ui.HTML(player_data),
                            type="application/json",
                            id="event_modal_data",
                        ),
                        ui.div(
                            ui.tags.div(
                                id="event_modal_js_layer",
                                class_="event-modal-js-layer",
                            ),
                            ui.tags.div(
                                id="event_modal_overlay_container",
                                class_="event-modal-overlay-container",
                            ),
                            ui.tags.div(
                                {
                                    "class": "event-modal-picture-spinner",
                                    "aria-hidden": "true",
                                },
                                ui.tags.div(
                                    {
                                        "class": "spinner-border text-primary",
                                        "role": "status",
                                    },
                                    ui.tags.span(
                                        {"class": "visually-hidden"}, _("Loading...")
                                    ),
                                ),
                            ),
                            id="event_modal_picture_wrap",
                            class_="event-modal-picture-wrap",
                            style_=aspect_style[0],
                        ),
                        id="event_modal_root",
                        **{
                            "data-block-id": str(block_id),
                            "data-bundle-url": str(bundle_url[0] or ""),
                            "data-ns-frame-idx": ns_frame_idx,
                        },
                    ),
                    ui.card_footer(
                        ui.div(
                            ui.div(
                                ui.div(
                                    ui.tooltip(
                                        ui.input_action_button(
                                            id="btn_delete_event",
                                            label="",
                                            icon=icon_svg(
                                                "trash-can",
                                                margin_left="0",
                                                margin_right="0",
                                            ),
                                            class_="btn-icon-square btn-outline-danger",
                                        ),
                                        _("Delete all pictures of this event"),
                                        id="tooltip_delete_event",
                                        options={"trigger": "hover"},
                                    ),
                                    class_="event-modal-toolbar-left",
                                ),
                                ui.div(
                                    ui.HTML(nav_html),
                                ),
                                ui.div(
                                    ui.HTML(overlay_btn_html),
                                    class_="event-modal-toolbar-close",
                                ),
                                class_="event-modal-toolbar-row event-modal-toolbar-top",
                            ),
                            ui.div(
                                ui.div(
                                    ui.tooltip(
                                        ui.download_button(
                                            id="btn_download_single",
                                            label="",
                                            icon=icon_svg(
                                                "image",
                                                margin_left="0",
                                                margin_right="0",
                                            ),
                                            class_="btn-icon-square btn-outline-secondary",
                                        ),
                                        _("Download current picture"),
                                        id="tooltip_download_single",
                                        options={"trigger": "hover"},
                                    ),
                                    ui.tooltip(
                                        ui.input_action_button(
                                            id="btn_send_to_labelstudio",
                                            label="",
                                            icon=icon_svg(
                                                "upload",
                                                margin_left="0",
                                                margin_right="0",
                                            ),
                                            class_="btn-icon-square btn-outline-secondary",
                                            disabled_=(
                                                not CONFIG.get("LABELSTUDIO_API_TOKEN")
                                                or not CONFIG.get("LABELSTUDIO_PROJECT")
                                                or not LabelStudioInstall.get_labelstudio_status()
                                            ),
                                        ),
                                        _("Send current picture to Label Studio"),
                                        id="tooltip_send_to_labelstudio",
                                        options={"trigger": "hover"},
                                    ),
                                    class_="event-modal-toolbar-bottom-left",
                                ),
                                ui.div(
                                    ui.tags.div(
                                        id="event_modal_scrubber_container",
                                    ),
                                    class_="event-modal-toolbar-bottom-middle",
                                ),
                                ui.div(
                                    ui.tooltip(
                                        ui.download_button(
                                            id="btn_download",
                                            label="",
                                            icon=icon_svg(
                                                "file-zipper",
                                                margin_left="0",
                                                margin_right="0",
                                            ),
                                            class_="btn-icon-square btn-outline-secondary",
                                        ),
                                        _("Download all pictures of this event (ZIP)"),
                                        id="tooltip_download_zip",
                                        options={"trigger": "hover"},
                                    ),
                                    ui.input_action_button(
                                        id="btn_modal_cancel",
                                        label="",
                                        icon=icon_svg(
                                            "xmark", margin_left="0", margin_right="0"
                                        ),
                                        class_="btn-icon-square btn-outline-secondary btn-icon-close",
                                    ),
                                    class_="event-modal-toolbar-bottom-right",
                                ),
                                class_="event-modal-toolbar-row event-modal-toolbar-bottom",
                            ),
                            class_="event-modal-toolbar",
                        ),
                    ),
                    full_screen=False,
                    class_="image-container",
                ),
                footer=ui.div(
                    ui.input_action_button(
                        "modal_pulse",
                        "",
                        style_="visibility:hidden; width:1px; height:1px;",
                    ),
                ),
                size="l",
                easy_close=True,
                class_="transparent-modal-content",
            )
        )

    # ---- Modal close ----
    @reactive.effect
    @reactive.event(input.btn_modal_cancel, input.modal_pulse)
    def modal_cancel():
        ui.modal_remove()
        pictures.clear()
        timestamps.clear()
        event_datas.clear()
        photo_ids.clear()

    # ---- Single image download ----
    def _single_image_filename():
        try:
            frame_idx = int(input.client_frame_idx() or 0)
            picture_number = frame_idx + 1
        except Exception:
            picture_number = 0
        return f"kittyhack_event_{block_id}_{picture_number}.jpg"

    @render.download(filename=_single_image_filename)
    def btn_download_single():
        try:
            vis_idx = int(input.client_frame_idx() or 0)
            if vis_idx >= len(photo_ids):
                raise RuntimeError("No visible image")

            pid = photo_ids[int(vis_idx)]
            if pid is None:
                raise RuntimeError("Missing photo ID")

            # Prefer original image from filesystem if present
            img_bytes = None
            try:
                fp = os.path.join(pictures_original_dir(), f"{int(pid)}.jpg")
                if os.path.exists(fp):
                    with open(fp, "rb") as f:
                        img_bytes = f.read()
            except Exception:
                img_bytes = None

            # Fallback: legacy DB BLOB for older rows (if file not present)
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
                raise RuntimeError("No image bytes")

            out_path = os.path.join(
                "/tmp", f"kittyhack_event_{block_id}_img_{int(pid)}.jpg"
            )
            with open(out_path, "wb") as f:
                f.write(img_bytes)
            return out_path
        except Exception as e:
            logging.warning(f"[DOWNLOAD] Single image download failed: {e}")
            out_path = os.path.join(
                "/tmp", f"kittyhack_event_{block_id}_download_failed.txt"
            )
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("Single image download failed.\n")
            return out_path

    # ---- Send to Label Studio ----
    @reactive.effect
    @reactive.event(input.btn_send_to_labelstudio)
    async def send_to_labelstudio():
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

        try:
            vis_idx = int(input.client_frame_idx() or 0)
            if vis_idx >= len(photo_ids):
                raise RuntimeError("No visible image")

            pid = photo_ids[int(vis_idx)]
            if pid is None:
                raise RuntimeError("Missing photo ID")

            # Load image bytes (same logic as single-image download)
            img_bytes = None
            try:
                fp = os.path.join(pictures_original_dir(), f"{int(pid)}.jpg")
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

            # Show progress notification
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

            filename = f"kittyhack_event_{block_id}_{vis_idx + 1}.jpg"
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

        except Exception as e:
            logging.warning(f"[LABELSTUDIO] Send to Label Studio failed: {e}")
            ui.notification_remove("ls_upload_progress")
            ui.notification_show(
                _("Failed to send picture to Label Studio."), type="error", duration=5
            )

    # ---- Delete event ----
    @reactive.effect
    @reactive.event(input.btn_delete_event)
    def delete_event():
        ui.modal_remove()
        ui.modal_show(
            ui.modal(
                _("Delete this event and all its pictures?"),
                title=_("Delete event"),
                easy_close=False,
                footer=ui.div(
                    ui.input_action_button(
                        "btn_modal_delete_event_ok",
                        _("Delete"),
                        class_="btn-danger",
                    ),
                    ui.input_action_button("btn_modal_cancel", _("Cancel")),
                ),
            )
        )

    @reactive.effect
    @reactive.event(input.btn_modal_delete_event_ok)
    def delete_event_confirmed():
        logging.info(f"Delete all pictures of event with block_id {block_id}")
        EventsRepo.delete_photos_by_block_id(
            CONFIG["KITTYHACK_DATABASE_PATH"], block_id
        )
        reload_trigger_photos.set(reload_trigger_photos.get() + 1)
        ui.modal_remove()
        pictures.clear()
        timestamps.clear()
        event_datas.clear()
        photo_ids.clear()

    # ---- ZIP download ----
    @render.download(filename=f"kittyhack_event_{block_id}.zip")
    def btn_download():
        df = EventsRepo.db_get_photos_by_block_id(
            CONFIG["KITTYHACK_DATABASE_PATH"], block_id, ReturnDataPhotosDB.all
        )

        files: list[tuple[str, bytes]] = []
        if not df.empty:
            for __, row in df.iterrows():
                pid = int(row["id"])
                try:
                    local_dt_str = DateTimeUtil.get_local_date_from_utc_date(
                        str(row["created_at"])
                    )
                    ts = pd.to_datetime(local_dt_str, errors="coerce")
                    ts = (
                        ts.strftime("%Y%m%d_%H%M%S")
                        if isinstance(ts, pd.Timestamp)
                        else "unknown"
                    )
                except Exception as e:
                    logging.warning(
                        f"[DOWNLOAD] Failed to format timestamp for ID {pid}: {e}"
                    )
                    ts = "unknown"

                orig_bytes = None
                try:
                    fp = os.path.join(pictures_original_dir(), f"{pid}.jpg")
                    if os.path.exists(fp):
                        with open(fp, "rb") as f:
                            orig_bytes = f.read()
                except Exception as e:
                    logging.warning(
                        f"[DOWNLOAD] Failed reading original file for ID {pid}: {e}"
                    )
                if orig_bytes is None:
                    ob = row.get("original_image")
                    if isinstance(ob, (bytes, bytearray)) and len(ob) > 0:
                        orig_bytes = bytes(ob)

                if isinstance(orig_bytes, (bytes, bytearray)) and len(orig_bytes) > 0:
                    files.append((f"{pid}_{ts}.jpg", bytes(orig_bytes)))

        if len(files) == 0:
            logging.warning(
                f"[DOWNLOAD] No images available for block_id {block_id}. Returning placeholder ZIP."
            )
            readme = (
                "Kittyhack event download\n"
                f"block_id: {block_id}\n\n"
                "No images are available for this event.\n"
                "- They may have been deleted due to retention limits,\n"
                "- or migrated but missing on disk,\n"
                "- or were never stored.\n"
            ).encode("utf-8")
            files.append(("README.txt", readme))

        tmp_dir = "/tmp"
        zip_path = os.path.join(
            tmp_dir, f"kittyhack_event_{block_id}_{int(tm.time())}.zip"
        )
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, data in files:
                zf.writestr(name, data)

        return zip_path
