import requests
import logging
import os
import zipfile
import json
import re
import shutil
from io import BytesIO
from datetime import datetime
from shiny import ui
import subprocess
import sys
from typing import Any
import cv2
import numpy as np
import time as tm
import multiprocessing
import threading
from src.baseconfig import CONFIG, set_language, update_single_config_parameter, UserNotifications
from src.mode import is_remote_mode
from src.camera import videostream, image_buffer, VideoStream, DetectedObject
from src.helper import sigterm_monitor, get_timezone, is_valid_uuid4
from src.database import get_cat_names_list
from src.paths import models_yolo_root

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tflite_runtime.interpreter import Interpreter

_ = set_language(CONFIG['LANGUAGE'])

_MODEL_DL_STATE_PATH = "/tmp/kittyhack_model_download_state.json"


def _remote_internal_proxy_url() -> str | None:
    """Return implicit target MJPEG URL for remote-mode internal camera selection."""
    if not is_remote_mode():
        return None

    camera_source = str(CONFIG.get("CAMERA_SOURCE") or "").strip().lower()
    if camera_source != "internal":
        return None

    host = str(CONFIG.get("REMOTE_TARGET_HOST") or "").strip()
    if not host:
        return None

    if host.startswith("http://") or host.startswith("https://"):
        base = host
    else:
        base = f"http://{host}"
    return base.rstrip("/") + "/video"


def _effective_camera_stream_config() -> tuple[str, str]:
    """Resolve runtime stream source/url from current CONFIG.

    In remote-mode with CAMERA_SOURCE=internal, stream from target MJPEG relay
    (`http://REMOTE_TARGET_HOST/video`) while keeping config unchanged.
    """
    proxy_url = _remote_internal_proxy_url()
    if proxy_url:
        return "ip_camera", proxy_url
    return str(CONFIG.get("CAMERA_SOURCE") or "internal"), str(CONFIG.get("IP_CAMERA_URL") or "")


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _default_download_state() -> dict[str, Any]:
    return {
        "status": "idle",  # idle|downloading|extracting|done|error
        "training_job_id": "",
        "result_id": "",
        "model_name": "",
        "bytes_downloaded": 0,
        "total_bytes": 0,
        "started_at": 0.0,
        "finished_at": 0.0,
        "error": "",
        "target_dir": "",
        "pid": 0,
        "finalized": False,
    }

class RemoteModelTrainer:
    BASE_URL = "https://kittyhack-models.fk-cloud.de"

    @staticmethod
    def get_model_download_state() -> dict[str, Any]:
        state = _default_download_state()
        state.update(_read_json(_MODEL_DL_STATE_PATH) or {})

        # If the worker died unexpectedly, mark it as error so UI can recover.
        if state.get("status") in ("downloading", "extracting"):
            pid = int(state.get("pid") or 0)
            if pid and not _pid_alive(pid):
                state["status"] = "error"
                state["error"] = state.get("error") or "worker_died"
                state["finished_at"] = float(state.get("finished_at") or 0.0) or tm.time()
                state["pid"] = 0
                try:
                    _atomic_write_json(_MODEL_DL_STATE_PATH, state)
                except Exception:
                    pass

        return state

    @staticmethod
    def _write_model_download_state(state: dict[str, Any]) -> None:
        merged = _default_download_state()
        merged.update(state)
        _atomic_write_json(_MODEL_DL_STATE_PATH, merged)

    @staticmethod
    def start_download_model_async(
        training_job_id: str,
        result_id: str,
        model_name: str = "",
        token: str | None = None,
    ) -> bool:
        """Start downloading/extracting a trained model in a separate worker process.

        This prevents UI freezing from CPU/GIL-heavy extraction.
        Returns True if a new worker was started.
        """

        state = RemoteModelTrainer.get_model_download_state()
        if state.get("status") == "done" and state.get("result_id") == result_id:
            return False
        if state.get("status") in ("downloading", "extracting") and _pid_alive(int(state.get("pid") or 0)):
            # Do not start a second concurrent worker.
            return False

        root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        args = [
            sys.executable,
            "-m",
            "src.model_download_worker",
            "--base-url",
            RemoteModelTrainer.BASE_URL,
            "--result-id",
            result_id,
            "--model-name",
            model_name or "",
            "--state-path",
            _MODEL_DL_STATE_PATH,
        ]
        if token:
            args += ["--token", token]

        # Initialize state before starting (worker will update as it runs)
        init_state = _default_download_state()
        init_state.update(
            {
                "status": "downloading",
                "training_job_id": training_job_id,
                "result_id": result_id,
                "model_name": model_name or "",
                "bytes_downloaded": 0,
                "total_bytes": 0,
                "started_at": tm.time(),
                "finished_at": 0.0,
                "error": "",
                "target_dir": "",
                "pid": 0,
                "finalized": False,
            }
        )
        try:
            RemoteModelTrainer._write_model_download_state(init_state)
        except Exception:
            pass

        worker_log_path = "/tmp/kittyhack_model_download_worker.log"
        try:
            log_fh = open(worker_log_path, "a", encoding="utf-8")
        except Exception:
            log_fh = None

        try:
            proc = subprocess.Popen(
                args,
                cwd=root_dir,
                stdout=(log_fh if log_fh is not None else subprocess.DEVNULL),
                stderr=(log_fh if log_fh is not None else subprocess.DEVNULL),
            )
        except Exception as e:
            init_state["status"] = "error"
            init_state["error"] = f"failed_to_start_worker: {e}"
            init_state["finished_at"] = tm.time()
            try:
                RemoteModelTrainer._write_model_download_state(init_state)
            except Exception:
                pass
            return False
        finally:
            try:
                if log_fh is not None:
                    log_fh.close()
            except Exception:
                pass

        init_state["pid"] = int(proc.pid)
        try:
            RemoteModelTrainer._write_model_download_state(init_state)
        except Exception:
            pass

        return True

    @staticmethod
    def get_server_status():
        """
        Checks the availability/maintenance state of the model training server.

        Expected JSON response example:
        {
            "maintenance": false,
            "message": "Optional maintenance info text"
        }

        Returns:
            dict | None: Parsed JSON on success, or None on error.
        """
        url = f"{RemoteModelTrainer.BASE_URL}/server_status"
        try:
            response = requests.get(url, verify=True, timeout=5)
            response.raise_for_status()
            data = response.json()
            # Normalize keys and defaults
            maintenance = bool(data.get("maintenance", False))
            message = data.get("message") or data.get("maintenance_text") or ""
            return {"maintenance": maintenance, "message": message}
        except Exception as e:
            logging.warning(f"[MODEL_TRAINING] Server status check failed: {e}")
            return None

    @staticmethod
    def enqueue_model_training(
        zip_file_path,
        model_name = "",
        user_name = "",
        email = "",
        yolo_model_variant: str = "n",
        image_size: int = 320,
    ):
        """
        Uploads a zip file to the remote server to start model training.
        Returns the job_id on success.
        """
        url = f"{RemoteModelTrainer.BASE_URL}/upload"
        files = {'file': open(zip_file_path, 'rb')}

        variant = str(yolo_model_variant or "n").strip().lower()
        if variant not in {"n", "s", "m", "l", "x"}:
            variant = "n"

        image_size_int = YoloModel._normalize_model_image_size(image_size)
        pretrained_model = f"yolov8{variant}.pt"

        data = {
            'username': user_name,
            'email': email,
            'model_name': model_name,
            # Explicit model training configuration (remote-mode advanced options)
            'yolo_model_variant': variant,
            'pretrained_model': pretrained_model,
            'image_size': str(image_size_int),
            # Backward/compat key used by some training pipelines
            'imgsz': str(image_size_int),
        }
        try:
            response = requests.post(url, files=files, data=data, verify=True)
            response.raise_for_status()
            response_json = response.json()
            return response_json.get("job_id")
        except Exception as e:
            # FIXME: If the http return code is 400, we should return "invalid_file" instead of None. If the destination is not reachable, we should return "destination_unreachable"
            if response.status_code == 400:
                logging.error(f"[MODEL_TRAINING] Invalid file: {e}")
                return "invalid_file"
            elif response.status_code == 503:
                logging.error(f"[MODEL_TRAINING] Destination unreachable: {e}")
                return "destination_unreachable"
            elif response.status_code == 413:
                logging.error(f"[MODEL_TRAINING] File too large: {e}")
                return "file_too_large"
            elif response.status_code == 500:
                logging.error(f"[MODEL_TRAINING] Internal server error: {e}")
                return "internal_server_error"
            elif response.status_code == 404:
                logging.error(f"[MODEL_TRAINING] Destination not found: {e}")
                return "destination_not_found"
            else:
                logging.error(f"[MODEL_TRAINING] Unknown error: {e}")
                return "unknown_error"
        finally:
            files['file'].close()

    @staticmethod
    def get_model_training_status(job_id):
        """
        Checks the status of a model training job.
        Returns status info on success.
        """
        url = f"{RemoteModelTrainer.BASE_URL}/status/{job_id}"
        try:
            response = requests.get(url, verify=True, timeout=5)
            if response.status_code == 404:
                logging.error(f"[MODEL_TRAINING] Job {job_id} not found.")
                return None
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"[MODEL_TRAINING] Error checking status: {e}")
            return None
        
    @staticmethod
    def cancel_model_training(job_id):
        """
        Cancels a pending model training job.
        Returns True if the cancellation was successful, False otherwise.
        """
        url = f"{RemoteModelTrainer.BASE_URL}/cancel/{job_id}"
        try:
            response = requests.post(url, verify=True)
            if response.status_code == 404:
                logging.error(f"[MODEL_TRAINING] Job {job_id} not found for cancellation.")
                return False
            response.raise_for_status()
            return True
        except Exception as e:
            logging.error(f"[MODEL_TRAINING] Error cancelling job: {e}")
            return False
    

    @staticmethod
    def download_model(result_id: str, model_name="", token: str = None):
        """
        Downloads the trained model zip file from the remote server,
        extracts the data to /root/models/yolo/<model_name>/.
        If model_name is not provided, the model_name of the info.json will be used. If this is 
        also not available, it is set to the current timestamp as YYYY-MM-DD_HH-MM-SS.
        If the target directory already exists, a unique name is generated by appending a number.
        """

        # Sanitize model_name to avoid file system issues
        def sanitize_directory_name(name):
            # Convert to lowercase
            name = name.lower()
            # Allow only alphanumeric characters, underscores, and hyphens
            sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
            # Replace subsequent underscores with a single underscore
            sanitized = re.sub(r'_{2,}', '_', sanitized)
            # Remove leading/trailing spaces and dots (though regex already handles this)
            sanitized = sanitized.strip().strip('.')
            # Ensure we have a valid name, default if entirely invalid
            if not sanitized:
                sanitized = "model_" + datetime.now(get_timezone()).strftime("%Y%m%d_%H%M%S")
            return sanitized

        # Synchronous compatibility wrapper (now uses temp file instead of buffering in RAM)
        tmp_zip_path = ""
        try:
            tmp_zip_path = RemoteModelTrainer._download_model_zip_to_tempfile(result_id=result_id, token=token)
            success, _target_dir, _err = RemoteModelTrainer._extract_model_zip(zip_path=tmp_zip_path, model_name=model_name)
            return bool(success)
        except Exception as e:
            logging.error(f"[MODEL_TRAINING] Error downloading or extracting model: {e}")
            return False
        finally:
            if tmp_zip_path and os.path.exists(tmp_zip_path):
                try:
                    os.remove(tmp_zip_path)
                except Exception:
                    pass

    @staticmethod
    def _download_model_zip_to_tempfile(result_id: str, token: str | None = None) -> str:
        url = f"{RemoteModelTrainer.BASE_URL}/download/{result_id}"
        headers: dict[str, str] = {}
        if token:
            headers["token"] = token

        tmp_path = os.path.join("/tmp", f"kittyhack_model_{result_id}.zip")
        # Ensure a clean slate
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

        response = requests.get(url, headers=headers, stream=True, verify=True, timeout=(5, 60))
        if response.status_code == 404:
            raise FileNotFoundError(f"Result {result_id} not found")
        response.raise_for_status()

        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if not chunk:
                    continue
                f.write(chunk)

        return tmp_path

    @staticmethod
    def _extract_model_zip(zip_path: str, model_name: str = "") -> tuple[bool, str, str]:
        """Extract zip_path into /root/models/yolo/<unique_model_name>.

        Returns: (success, target_dir, error_message)
        """
        # Sanitize model_name to avoid file system issues
        def sanitize_directory_name(name: str) -> str:
            name = (name or "").lower()
            sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
            sanitized = re.sub(r"_{2,}", "_", sanitized)
            sanitized = sanitized.strip().strip(".")
            if not sanitized:
                sanitized = "model_" + datetime.now(get_timezone()).strftime("%Y%m%d_%H%M%S")
            return sanitized

        if not os.path.exists(zip_path):
            return False, "", "zip_missing"

        # Determine model name from info.json if needed
        info_json_model_name = None
        creation_date = datetime.now(get_timezone()).strftime("%Y-%m-%d_%H-%M-%S")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                if "info.json" in zf.namelist():
                    with zf.open("info.json") as info_file:
                        info_data = json.load(info_file)
                        info_json_model_name = info_data.get("MODEL_NAME")
                        try:
                            timestamp = datetime.fromisoformat(info_data.get("TIMESTAMP_UTC"))
                            creation_date = timestamp.astimezone(get_timezone()).strftime("%Y-%m-%d_%H:%M:%S")
                        except Exception:
                            pass
        except Exception as e:
            logging.warning(f"[MODEL_TRAINING] Could not parse info.json: {e}")

        if not model_name:
            model_name = info_json_model_name or creation_date

        base_dir = models_yolo_root()
        model_name = sanitize_directory_name(model_name)
        target_dir = os.path.join(base_dir, model_name)
        unique_dir = target_dir
        count = 1
        while os.path.exists(unique_dir):
            unique_dir = f"{target_dir}_{count}"
            count += 1
        target_dir = unique_dir

        try:
            os.makedirs(target_dir, exist_ok=True)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(target_dir)

            required_files = [
                "model.pt",
                "labels.txt",
                "info.json",
                "best_ncnn_model/model.ncnn.bin",
                "best_ncnn_model/model.ncnn.param",
            ]
            missing_files = [f for f in required_files if not os.path.exists(os.path.join(target_dir, f))]
            if missing_files:
                return False, target_dir, f"missing_files: {', '.join(missing_files)}"

            logging.info(f"[MODEL_TRAINING] Model extracted to {target_dir}")
            return True, target_dir, ""
        except Exception as e:
            logging.error(f"[MODEL_TRAINING] Error extracting model: {e}")
            # Best-effort cleanup to avoid leaving half-extracted directories
            try:
                if os.path.isdir(target_dir):
                    shutil.rmtree(target_dir, ignore_errors=True)
            except Exception:
                pass
            return False, "", str(e)
        
    @staticmethod
    def check_model_training_result(show_notification=True, show_in_progress=False, return_pretty_status=False):
        """
        Checks if a model training is in progress and handles the result.
        If the training is completed, it downloads the model and updates the configuration.
        If the training is not in progress, it returns "not_in_progress".
        If the training is in progress, it returns the training status.
        """
        # Check if a model training is in progress
        if is_valid_uuid4(CONFIG["MODEL_TRAINING"]):
            job_id = CONFIG["MODEL_TRAINING"]

            # Always honor existing download state first to avoid re-start loops.
            dl_state = RemoteModelTrainer.get_model_download_state()
            dl_status = dl_state.get("status")

            # Heuristic: treat the state as relevant if it either matches this job_id (preferred)
            # or if the worker did not include training_job_id but the state is recent.
            state_job_id = (dl_state.get("training_job_id") or "").strip()
            state_recent = False
            try:
                started_at = float(dl_state.get("started_at") or 0.0)
                state_recent = started_at > 0.0 and (tm.time() - started_at) < (6 * 3600)
            except Exception:
                state_recent = False

            state_matches = (state_job_id == job_id) or (not state_job_id and state_recent)

            if state_matches and dl_status in ("downloading", "extracting"):
                training_status = dl_status
            elif state_matches and dl_status in ("done", "error"):
                # Finalize (clear config + add persistent notification) in the main process.
                if not bool(dl_state.get("finalized")):
                    if dl_status == "done":
                        try:
                            CONFIG["MODEL_TRAINING"] = ""
                            update_single_config_parameter("MODEL_TRAINING")
                        except Exception as e:
                            logging.warning(f"[MODEL_TRAINING] Failed to clear MODEL_TRAINING after download: {e}")
                        try:
                            UserNotifications.add(
                                header=_("Model downloaded"),
                                message=_(
                                    "Model training completed and the new model was downloaded successfully. You can select it now in the 'Configuration' section."
                                ),
                                type="message",
                                id=f"model_download_success_{dl_state.get('result_id')}",
                                skip_if_id_exists=True,
                            )
                        except Exception as e:
                            logging.warning(f"[MODEL_TRAINING] Failed to add success notification: {e}")
                    else:
                        try:
                            UserNotifications.add(
                                header=_("Model download failed"),
                                message=_(
                                    "Model training completed, but the model could not be downloaded. Please retry later."
                                ),
                                type="error",
                                id=f"model_download_error_{dl_state.get('result_id')}",
                                skip_if_id_exists=True,
                            )
                        except Exception as e:
                            logging.warning(f"[MODEL_TRAINING] Failed to add error notification: {e}")

                    dl_state["finalized"] = True
                    # Preserve job_id for future checks
                    if not dl_state.get("training_job_id"):
                        dl_state["training_job_id"] = job_id
                    try:
                        RemoteModelTrainer._write_model_download_state(dl_state)
                    except Exception:
                        pass

                training_status = "downloaded" if dl_status == "done" else "download_error"
            else:
                # No relevant download state yet; poll the model server.
                response = RemoteModelTrainer.get_model_training_status(job_id)
                try:
                    training_status = response.get("status")
                    training_result_id = response.get("result_id")
                except Exception:
                    training_status = "unknown"
                    training_result_id = ""

                if training_status == "completed":
                    # Start the download worker and switch UI to download progress.
                    if training_result_id:
                        RemoteModelTrainer.start_download_model_async(job_id, training_result_id)
                    dl_state = RemoteModelTrainer.get_model_download_state()
                    dl_status = dl_state.get("status")
                    if dl_status in ("downloading", "extracting"):
                        training_status = dl_status
                    elif dl_status == "done":
                        training_status = "downloaded"
                    elif dl_status == "error":
                        training_status = "download_error"
                elif training_status == "aborted":
                    # Abort on client side as well
                    CONFIG["MODEL_TRAINING"] = ""
                    update_single_config_parameter("MODEL_TRAINING")
                    # Show user notification
                    UserNotifications.add(
                        header=_("Model Training Aborted"),
                        message=_(
                            "The model training was aborted. This can happen if the provided training data was not correct. "
                            "Please ensure you have exported the data from Label Studio as **'YOLO with Images'** and that your labels are set correctly in the images."
                        ),
                        type="error",
                        id="model_training_aborted",
                        skip_if_id_exists=True
                    )
                else:
                    if show_notification and show_in_progress:
                        ui.notification_show(_("Model training is in progress. Please check back later."), duration=5, type="default")
        else:
            training_status = "not_in_progress"
        
        # Map statuses to user-friendly messages
        pretty_status_messages = {
            "pending": _("The training is pending and will start soon."),
            "queued": _("The training is queued and waiting for resources."),
            "completed": _("The training has been successfully completed."),
            "downloading": _("Training completed. Downloading the model…"),
            "extracting": _("Training completed. Installing the model…"),
            "downloaded": _("Training completed. Model downloaded and installed."),
            "download_error": _("Training completed, but the model download failed."),
            "aborted": _("The training was aborted. Please try again."),
            "unknown": _("The training status is unknown. Please check back later."),
            "not_in_progress": _("No model training is in progress."),
        }

        if return_pretty_status:
            # Add progress details for background download/extraction when possible
            if training_status in ("downloading", "extracting"):
                state = RemoteModelTrainer.get_model_download_state()
                total = int(state.get("total_bytes") or 0)
                done = int(state.get("bytes_downloaded") or 0)
                if total > 0 and done >= 0:
                    pct = min(100, int((done / total) * 100))
                    base = pretty_status_messages.get(training_status, training_status)
                    return f"{base} ({pct}%)"
            # If the status is not in the mapping, return the original status
            return pretty_status_messages.get(training_status, training_status)
        
        # If not returning pretty status, return the training status directly
        # Return the training status
        return training_status
    
class YoloModel:
    """
    Class to handle YOLO models on the local filesystem.
    """
    BASE_DIR = models_yolo_root()

    @staticmethod
    def get_supported_image_sizes() -> list[int]:
        """Return supported YOLO image sizes.

        YOLO models commonly expect input dimensions that are multiples of 32.
        We support the common range between 320 and 640 (inclusive).
        """
        return list(range(320, 640 + 1, 32))

    @staticmethod
    def _normalize_model_image_size(raw_value: Any) -> int:
        """Normalize model image size to a supported value.

        - Parses any input to int (fallback 320)
        - Clamps into the supported range
        - Snaps to the nearest supported size (multiples of 32)
        """
        try:
            size = int(str(raw_value).strip())
        except Exception:
            size = 320

        supported = YoloModel.get_supported_image_sizes()
        if not supported:
            return 320

        min_size = supported[0]
        max_size = supported[-1]
        if size < min_size:
            size = min_size
        elif size > max_size:
            size = max_size

        # Snap to nearest supported size
        return min(supported, key=lambda s: (abs(s - size), s))

    @staticmethod
    def get_model_list():
        """
        Returns a list of available YOLO models with their creation dates.
        Each model is represented as a dictionary with keys: 'display_name', 'directory', 'creation_date'.
        """
        model_list = []
        if not os.path.exists(YoloModel.BASE_DIR):
            return model_list
        
        for dir_name in os.listdir(YoloModel.BASE_DIR):
            model_path = os.path.join(YoloModel.BASE_DIR, dir_name)
            if os.path.isdir(model_path):
                model_name = dir_name
                model_image_size = 320
                yolo_variant = "yolov8n.pt"
                effective_fps: float | None = None
                effective_fps_updated_at_utc: str | None = None

                # Fallback creation date: filesystem time
                try:
                    creation_time = os.path.getctime(model_path)
                    creation_date = datetime.fromtimestamp(creation_time, get_timezone()).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    creation_date = ""
                
                # Try to read model_name, timestamp and unique_id from info.json if it exists
                info_json_path = os.path.join(model_path, "info.json")
                unique_id = None
                if os.path.exists(info_json_path):
                    try:
                        with open(info_json_path, 'r') as f:
                            info_data = json.load(f)
                            model_name = info_data.get('MODEL_NAME', dir_name)
                            unique_id = info_data.get('JOB_ID')
                            # Support current and legacy keys from info.json
                            # (MODEL_IMAGE_SIZE, IMAGE_SIZE/image_size, IMGSZ/imgsz).
                            raw_image_size = (
                                info_data.get('MODEL_IMAGE_SIZE')
                                or info_data.get('IMAGE_SIZE')
                                or info_data.get('image_size')
                                or info_data.get('IMGSZ')
                                or info_data.get('imgsz')
                                or 320
                            )
                            model_image_size = YoloModel._normalize_model_image_size(raw_image_size)

                            # YOLO model variant / base model name (if available)
                            raw_variant = (
                                info_data.get('YOLO_MODEL_VARIANT')
                                or info_data.get('yolo_model_variant')
                                or ""
                            )
                            raw_pretrained = (
                                info_data.get('PRETRAINED_MODEL')
                                or info_data.get('pretrained_model')
                                or ""
                            )
                            if isinstance(raw_pretrained, str) and raw_pretrained.strip():
                                yolo_variant = raw_pretrained.strip()
                            else:
                                variant = str(raw_variant or "").strip().lower()
                                if variant in {"n", "s", "m", "l", "x"}:
                                    yolo_variant = f"yolov8{variant}.pt"
                                else:
                                    yolo_variant = "yolov8n.pt"

                            # Effective FPS (if available)
                            raw_fps = (
                                info_data.get('EFFECTIVE_FPS')
                                or info_data.get('effective_fps')
                                or info_data.get('fps')
                            )
                            try:
                                if raw_fps is not None and str(raw_fps).strip() != "":
                                    effective_fps = float(raw_fps)
                            except Exception:
                                effective_fps = None

                            raw_fps_updated = (
                                info_data.get('EFFECTIVE_FPS_UPDATED_AT_UTC')
                                or info_data.get('effective_fps_updated_at_utc')
                            )
                            if isinstance(raw_fps_updated, str) and raw_fps_updated.strip():
                                effective_fps_updated_at_utc = raw_fps_updated.strip()

                            # Read the creation date from info.json
                            try:
                                # Parse timestamp from info.json
                                timestamp = datetime.fromisoformat(info_data['TIMESTAMP_UTC'])
                                creation_date = timestamp.astimezone(get_timezone()).strftime("%Y-%m-%d %H:%M:%S")
                            except (ValueError, TypeError) as e:
                                logging.warning(f"[MODEL] Invalid TIMESTAMP_UTC format for {dir_name}: {e}")
                            
                    except Exception as e:
                        logging.error(f"[MODEL] Could not parse info.json for {dir_name}: {e}")
                
                model_list.append({
                    'display_name': model_name, 
                    'directory': dir_name, 
                    'creation_date': creation_date,
                    'unique_id': unique_id,
                    'full_display_name': f"{model_name} ({creation_date})",
                    'model_image_size': model_image_size,
                    'yolo_variant': yolo_variant,
                    'effective_fps': effective_fps,
                    'effective_fps_updated_at_utc': effective_fps_updated_at_utc,
                })
        return model_list

    @staticmethod
    def update_model_metadata(unique_id: str, updates: dict[str, Any]) -> bool:
        """Merge `updates` into the model's info.json (atomic best-effort).

        This is intended for runtime metrics like effective FPS.
        """
        try:
            model_path = YoloModel.get_model_path(unique_id)
            if not model_path:
                return False

            info_json_path = os.path.join(model_path, "info.json")
            current: dict[str, Any] = {}
            if os.path.exists(info_json_path):
                try:
                    with open(info_json_path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                        if isinstance(loaded, dict):
                            current = loaded
                except Exception:
                    current = {}
            else:
                # Create minimal metadata if missing
                current = {
                    "JOB_ID": unique_id,
                    "MODEL_NAME": os.path.basename(model_path),
                }

            if not isinstance(updates, dict):
                return False

            current.update(updates)
            try:
                _atomic_write_json(info_json_path, current)
            except Exception:
                # Fallback to non-atomic write if atomic fails (e.g. FS limitations)
                with open(info_json_path, "w", encoding="utf-8") as f:
                    json.dump(current, f, indent=2)
            return True
        except Exception as e:
            logging.error(f"[MODEL] Failed to update metadata for model {unique_id}: {e}")
            return False
    
    @staticmethod
    def get_model_path(unique_id):
        """
        Returns the path to the model directory based on its unique ID (JOB_ID from info.json).
        If the model is not found, returns None.
        """
        model_list = YoloModel.get_model_list()
        for model in model_list:
            if model.get('unique_id') == unique_id:
                return os.path.join(YoloModel.BASE_DIR, model['directory'])
        return None
    
    @staticmethod
    def get_model_image_size(unique_id):
        """
        Returns the model image size based on its unique ID (JOB_ID from info.json).
        If the model is not found, returns None.
        """
        model_list = YoloModel.get_model_list()
        for model in model_list:
            if model.get('unique_id') == unique_id:
                return model['model_image_size']
        return None
    
    @staticmethod
    def delete_model(unique_id):
        """
        Deletes a YOLO model directory based on its unique ID (JOB_ID from info.json).
        Returns True if the deletion was successful, False otherwise.
        """
        model_list = YoloModel.get_model_list()
        directory_to_delete = None
        
        for model in model_list:
            if model.get('unique_id') == unique_id:
                directory_to_delete = model['directory']
                break
        
        if directory_to_delete:
            model_path = os.path.join(YoloModel.BASE_DIR, directory_to_delete)
            if os.path.exists(model_path):
                try:
                    shutil.rmtree(model_path)  # Using rmtree to delete non-empty directories
                    logging.info(f"[MODEL] Deleted model directory: {model_path}")
                    return True
                except Exception as e:
                    logging.error(f"[MODEL] Error deleting model directory: {e}")
                    return False
            else:
                logging.warning(f"[MODEL] Model directory does not exist: {model_path}")
                return False
        else:
            logging.warning(f"[MODEL] No model found with unique ID: {unique_id}")
            return False
        
    @staticmethod
    def rename_model(unique_id, new_name):
        """
        Renames the MODEL_NAME in the info.json file for the model with the given unique_id (JOB_ID).
        Returns True if the rename was successful, False otherwise.
        """
        model_list = YoloModel.get_model_list()
        directory_to_rename = None

        for model in model_list:
            if model.get('unique_id') == unique_id:
                directory_to_rename = model['directory']
                break

        if directory_to_rename:
            info_json_path = os.path.join(YoloModel.BASE_DIR, directory_to_rename, "info.json")
            if os.path.exists(info_json_path):
                try:
                    with open(info_json_path, 'r') as f:
                        info_data = json.load(f)
                    info_data['MODEL_NAME'] = new_name
                    with open(info_json_path, 'w') as f:
                        json.dump(info_data, f, indent=4)
                    logging.info(f"[MODEL] Renamed model {unique_id} to {new_name} in {info_json_path}")
                    return True
                except Exception as e:
                    logging.error(f"[MODEL] Error renaming model in info.json: {e}")
                    return False
            else:
                logging.warning(f"[MODEL] info.json does not exist for: {directory_to_rename}")
                return False
        else:
            logging.warning(f"[MODEL] No model found with unique ID: {unique_id}")
            return False

class ModelHandler:
    def __init__(self, 
                 model = "tflite",  # Can be instance of TfLite or Yolo
                 modeldir="./tflite/",
                 graph="cv-lite-model.tflite",
                 labelfile="labels.txt",
                 resolution="800x600",
                 framerate=10,
                 jpeg_quality=75,
                 model_image_size=320,
                 num_threads=4):

        self.labelfile = os.path.join(modeldir, labelfile)
        self.model = model
        if self.model == "tflite":
            self.modeldir = modeldir
        else:
            self.modeldir = os.path.join(modeldir, "best_ncnn_model")
        self.graph = graph
        self.resolution = resolution
        self.framerate = framerate
        self.jpeg_quality = jpeg_quality
        self.paused = False
        self.last_log_time = 0
        self._videostream_not_ready_last_log: dict[str, float] = {}
        self.input_size = int(model_image_size)
        self.num_threads = num_threads
        self.cat_names = [cat_name.lower() for cat_name in get_cat_names_list(CONFIG['KITTYHACK_DATABASE_PATH'])]
        self._stop_requested = threading.Event()

        self._fps_lock = threading.Lock()
        self._last_effective_fps: float | None = None
        self._last_avg_inference_fps: float | None = None
        self._last_fps_update_tm: float = 0.0

        # Load labels early so the model loop cannot crash depending on whether a UI client
        # accessed the camera API during startup.
        self.labels: list[str] = []
        self._load_labels()

    def _load_labels(self) -> None:
        try:
            with open(self.labelfile, 'r') as f:
                self.labels = [line.strip() for line in f.readlines() if line.strip()]
            if self.labels:
                logging.info(f"[MODEL] Labels loaded: {self.labels}")
            else:
                logging.warning(f"[MODEL] Label file '{self.labelfile}' is empty.")
        except Exception as e:
            self.labels = []
            logging.error(f"[MODEL] Failed to load labels from '{self.labelfile}': {e}")

    def _log_videostream_not_ready(self, what: str, *, interval_s: float = 10.0):
        try:
            now = tm.time()
        except Exception:
            now = 0.0
        last = float(self._videostream_not_ready_last_log.get(what, 0.0) or 0.0)
        if (now - last) >= float(interval_s):
            logging.warning(f"[CAMERA] '{what}' skipped. Video stream is not yet initialized.")
            self._videostream_not_ready_last_log[what] = now

    def load_model(self):
        if self.model == "tflite":
            from tflite_runtime.interpreter import Interpreter
            self._Interpreter = Interpreter
            self._yolo = None
            self._model_worker = None

        elif self.model == "yolo":
            from ultralytics import YOLO
            import multiprocessing
            from multiprocessing import Queue
            import os
            from src.baseconfig import configure_logging

            # Check if we're using all available cores
            all_cores = multiprocessing.cpu_count()
            using_all_cores = self.num_threads >= all_cores

            # If using all cores, run the model directly for better performance
            if using_all_cores:
                logging.info(f"[MODEL] Loading YOLO model directly in main process using all available cores")
                logging.getLogger("ultralytics").setLevel(logging.WARNING)
                logging.getLogger("ultralytics.yolo.engine.model").setLevel(logging.WARNING)
                self._yolo_model = YOLO(self.modeldir, task="detect", verbose=False)
                # Re-Configure logging to silence the model's output
                configure_logging(CONFIG['LOGLEVEL'])
                
                # Create a wrapper function to match the expected interface
                def direct_inference(frame, input_size):
                    # Run inference directly
                    results = self._yolo_model(frame, stream=True, imgsz=input_size, verbose=False)
                    
                    # Process results
                    mouse_probability = 0
                    own_cat_probability = 0
                    detected_objects = []
                    detected_info = []
                    probability_threshold_exceeded = False
                    min_threshold = CONFIG.get('MIN_THRESHOLD', 0)

                    for r in results:
                        for i, (box, conf, cls) in enumerate(zip(r.boxes.xyxy, r.boxes.conf, r.boxes.cls)):
                            xmin, ymin, xmax, ymax = box
                            cls_idx = int(cls)
                            object_name = self.labels[cls_idx] if 0 <= cls_idx < len(self.labels) else str(cls_idx)
                            probability = float(conf * 100)
                            detected_info.append(f"{object_name} ({probability:.1f}%)")
                            if probability >= min_threshold:
                                probability_threshold_exceeded = True

                            # Calculate original dimensions from the scale
                            imH, imW = frame.shape[:2]
                            scale = int(input_size) / max(imW, imH)
                            pad_x = (input_size - imW * scale) / 2
                            pad_y = (input_size - imH * scale) / 2

                            # Map bounding box coordinates back to original size
                            xmin_orig = float((xmin - pad_x) / scale)
                            ymin_orig = float((ymin - pad_y) / scale)
                            xmax_orig = float((xmax - pad_x) / scale)
                            ymax_orig = float((ymax - pad_y) / scale)

                            detected_object = {
                                'x': float(xmin_orig / imW * 100),
                                'y': float(ymin_orig / imH * 100),
                                'w': float((xmax_orig - xmin_orig) / imW * 100),
                                'h': float((ymax_orig - ymin_orig) / imH * 100),
                                'name': object_name,
                                'probability': probability
                            }

                            detected_objects.append(detected_object)

                            if object_name.lower() in ["prey", "beute"]:
                                mouse_probability = int(probability)
                            elif object_name.lower() in self.cat_names:
                                own_cat_probability = int(probability)

                    if detected_info and probability_threshold_exceeded:
                        logging.info(f"[MODEL] Detected {len(detected_info)} objects in image: {', '.join(detected_info)} (MIN_THRESHOLD={min_threshold})")

                    return (mouse_probability, own_cat_probability, detected_objects)
                
                self._yolo = direct_inference
                self._model_worker = None
            else:
                # Use the multiprocessing approach for limited CPU cores
                def model_worker_process(model_path, input_queue, output_queue, num_threads=1):
                    try:
                        # Set CPU affinity for this process based on num_threads
                        import psutil
                        process = psutil.Process()

                        # Calculate which cores to use (0..num_threads-1) but respect cpuset/container limits.
                        requested = list(range(int(num_threads) if num_threads else 1))
                        try:
                            allowed = process.cpu_affinity()  # type: ignore[call-arg]
                        except Exception:
                            allowed = []

                        cores_to_use = requested
                        if allowed:
                            cores_to_use = [c for c in requested if c in allowed]
                            if not cores_to_use:
                                # Fallback: use whatever the OS allows.
                                cores_to_use = list(allowed)

                        # Best-effort pinning: may fail on some kernels/containers (e.g. Errno 22).
                        try:
                            if cores_to_use:
                                process.cpu_affinity(cores_to_use)
                                logging.info(f"[MODEL] Worker process running on CPU cores {cores_to_use}")
                            else:
                                logging.info("[MODEL] Worker process running without CPU affinity (no cores resolved)")
                        except OSError as e:
                            logging.warning(f"[MODEL] CPU affinity not supported/allowed here; continuing without pinning: {e}")
                        except Exception as e:
                            logging.warning(f"[MODEL] Failed to set CPU affinity; continuing without pinning: {e}")
                        
                        # Load the YOLO model in this process
                        logging.getLogger("ultralytics").setLevel(logging.WARNING)
                        logging.getLogger("ultralytics.yolo.engine.model").setLevel(logging.WARNING)
                        model = YOLO(model_path, task="detect", verbose=False)
                        # Re-Configure logging to silence the model's output
                        configure_logging(CONFIG['LOGLEVEL'])
                        
                        while True:
                            # Get input from queue
                            job = input_queue.get()
                            if job is None:  # None is our signal to exit
                                break
                                
                            job_id, frame, input_size, pad_x, pad_y, scale, labels, cat_names, min_threshold = job
                            
                            # Perform inference
                            results = model(frame, stream=True, imgsz=input_size)
                            
                            # Process results
                            mouse_probability = 0
                            own_cat_probability = 0
                            detected_objects = []
                            
                            for r in results:
                                detected_info = []
                                probability_threshold_exceeded = False
                                for i, (box, conf, cls) in enumerate(zip(r.boxes.xyxy, r.boxes.conf, r.boxes.cls)):
                                    xmin, ymin, xmax, ymax = box
                                    cls_idx = int(cls)
                                    object_name = labels[cls_idx] if 0 <= cls_idx < len(labels) else str(cls_idx)
                                    probability = float(conf * 100)
                                    detected_info.append(f"{object_name} ({probability:.1f}%)")
                                    if probability >= min_threshold:
                                        probability_threshold_exceeded = True
                                    
                                    # Map bounding box coordinates back to original size
                                    xmin_orig = float((xmin - pad_x) / scale)
                                    ymin_orig = float((ymin - pad_y) / scale)
                                    xmax_orig = float((xmax - pad_x) / scale)
                                    ymax_orig = float((ymax - pad_y) / scale)
                                    
                                    # Calculate original image dimensions from the scale
                                    imW = int(frame.shape[1] / scale)
                                    imH = int(frame.shape[0] / scale)
                                    
                                    detected_object = {
                                        'x': float(xmin_orig / imW * 100),
                                        'y': float(ymin_orig / imH * 100),
                                        'w': float((xmax_orig - xmin_orig) / imW * 100),
                                        'h': float((ymax_orig - ymin_orig) / imH * 100),
                                        'name': object_name,
                                        'probability': probability
                                    }
                                    
                                    detected_objects.append(detected_object)
                                    
                                    if object_name.lower() in ["prey", "beute"]:
                                        mouse_probability = int(probability)
                                    elif object_name.lower() in cat_names:
                                        own_cat_probability = int(probability)

                                if detected_info and probability_threshold_exceeded:
                                    logging.info(f"[MODEL] Detected {len(detected_info)} objects in image: {', '.join(detected_info)} (MIN_THRESHOLD={min_threshold})")
                            
                            # Return results through the output queue
                            output_queue.put((job_id, mouse_probability, own_cat_probability, detected_objects))
                            
                    except Exception as e:
                        logging.error(f"[MODEL] Worker process error: {e}")
                        import traceback
                        logging.error(traceback.format_exc())
                    finally:
                        logging.info("[MODEL] Worker process exiting")

                # Create the queues for communication
                self._input_queue = Queue()
                self._output_queue = Queue()
                
                # Start the worker process with the specified number of threads
                self._model_worker = multiprocessing.Process(
                    target=model_worker_process,
                    args=(self.modeldir, self._input_queue, self._output_queue, self.num_threads)
                )
                self._model_worker.daemon = True
                self._model_worker.start()
                logging.info(f"[MODEL] Started YOLO worker process using {self.num_threads} CPU cores")

                self._yolo = self._send_to_worker
                self._Interpreter = None
                self._next_job_id = 0
                self._job_results = {}
        else:
            logging.error(f"[MODEL] Unknown model type: {self.model}. Failed to start inference.")
            
    def _send_to_worker(self, frame, input_size):
        """Send a frame to the worker process and return a job ID"""
        job_id = self._next_job_id
        self._next_job_id += 1
        
        # Calculate letterbox parameters
        imH, imW = frame.shape[:2]
        scale = int(input_size) / max(imW, imH)
        pad_x = (input_size - imW * scale) / 2
        pad_y = (input_size - imH * scale) / 2
        
        # Put the job in the queue
        self._input_queue.put((job_id, frame, input_size, pad_x, pad_y, scale, self.labels, self.cat_names, CONFIG['MIN_THRESHOLD']))
        return job_id
    
    def _get_result(self, job_id, timeout=1.0):
        """Get the result for a specific job ID"""
        try:
            # Check if we've received the result
            if job_id in self._job_results:
                return self._job_results.pop(job_id)
            
            # Try to get new results from the output queue
            while True:
                try:
                    result_job_id, mouse_prob, cat_prob, objects = self._output_queue.get(timeout=timeout)
                    self._job_results[result_job_id] = (mouse_prob, cat_prob, objects)
                    
                    if result_job_id == job_id:
                        return self._job_results.pop(job_id)
                except:
                    return None
        except Exception as e:
            logging.error(f"[MODEL] Error getting result: {e}")
            return None
    
    def __del__(self):
        """Cleanup when the object is deleted"""
        if hasattr(self, '_model_worker') and self._model_worker and self._model_worker.is_alive():
            self._input_queue.put(None)  # Signal to exit
            self._model_worker.join(timeout=2)  # Give it 2 seconds to exit gracefully

    def run(self):
        """Run the model on the video stream."""
        global videostream
        global CONFIG

        self._stop_requested.clear()

        resW, resH = self.resolution.split('x')
        imW, imH = int(resW), int(resH)

        self.load_model()

        # Store the last used effective camera config to detect changes
        last_camera_source, last_ip_camera_url = _effective_camera_stream_config()
        last_enable_ip_camera_decode_scale_pipeline = CONFIG.get('ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE', False)
        last_ip_camera_target_resolution = CONFIG.get('IP_CAMERA_TARGET_RESOLUTION', '640x360')
        last_ip_camera_pipeline_fps_limit = int(CONFIG.get('IP_CAMERA_PIPELINE_FPS_LIMIT', 10) or 10)

        # Check if the model is a YOLO model
        if self.model == "tflite":
            # ------------- TFLite Model -------------
            # Path to .tflite file, which contains the model that is used for object detection
            PATH_TO_TFLITE = os.path.join(self.modeldir, self.graph)

            logging.info(f"[MODEL] Preparing to run TFLite model {PATH_TO_TFLITE} on video stream with resolution {imW}x{imH} @ {self.framerate}fps and quality {self.jpeg_quality}%")

            # Load the Tensorflow Lite model.
            interpreter = self._Interpreter(model_path=PATH_TO_TFLITE, num_threads=self.num_threads)
            interpreter.allocate_tensors()

            # Get model details
            self.tf_input_details = interpreter.get_input_details()
            self.tf_output_details = interpreter.get_output_details()
            self.tf_height = self.tf_input_details[0]['shape'][1]
            self.tf_width = self.tf_input_details[0]['shape'][2]

            self.input_size = max(self.tf_height, self.tf_width)

            self.tf_floating_model = (self.tf_input_details[0]['dtype'] == np.float32)
            logging.info(f"[MODEL] Floating model: {self.tf_floating_model}")
            logging.info(f"[MODEL] Input details: {self.tf_input_details} (model shape: {self.tf_height}x{self.tf_width} --> {self.input_size})")

            # Check output layer name to determine if this model was created with TF2 or TF1,
            # because outputs are ordered differently for TF2 and TF1 models
            self.tf_outname = self.tf_output_details[0]['name']
        
        elif self.model == "yolo":
            # ------------- YOLO Model -------------
            logging.info(f"[MODEL] Preparing to run YOLO model {self.modeldir} on video stream with resolution {imW}x{imH} @ {self.framerate}fps and quality {self.jpeg_quality}%")
            # No need to initialize YOLO here - it's already running in the worker process

        else:
            logging.error(f"[MODEL] Unknown model type: {self.model}. Failed to start inference.") 
            return
        
        # Register task in the sigterm_monitor object
        sigterm_monitor.register_task()
        task_done_signaled = False

        try:
            # Initialize frame rate calculation
            frame_rate_calc = 1
            freq = cv2.getTickFrequency()

            # Initialize video stream
            effective_camera_source, effective_ip_camera_url = _effective_camera_stream_config()
            if is_remote_mode() and str(CONFIG.get('CAMERA_SOURCE') or '').strip().lower() == 'internal':
                if effective_camera_source == 'ip_camera' and effective_ip_camera_url:
                    logging.info(f"[CAMERA] Remote-mode implicit internal camera mapping active: {effective_ip_camera_url}")
                else:
                    logging.warning("[CAMERA] Remote-mode internal camera selected, but REMOTE_TARGET_HOST is empty. Falling back to local internal source.")

            videostream = VideoStream(
                source=effective_camera_source,
                ip_camera_url=effective_ip_camera_url,
                use_ip_camera_decode_scale_pipeline=CONFIG.get('ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE', False),
                ip_camera_target_resolution=CONFIG.get('IP_CAMERA_TARGET_RESOLUTION', '640x360'),
                ip_camera_pipeline_fps_limit=int(CONFIG.get('IP_CAMERA_PIPELINE_FPS_LIMIT', 10) or 10),
            ).start()
            logging.info(f"[CAMERA] Starting video stream...")

            # Wait for the camera to warm up
            detected_objects = []
            frame = None
            stream_start_time = tm.time()
            while frame is None and not sigterm_monitor.stop_now and not self._stop_requested.is_set():
                frame = videostream.read_oldest()
                if tm.time() - stream_start_time > 10:
                    logging.error("[CAMERA] Camera stream failed to start within 10 seconds!")
                    break
                else:
                    tm.sleep(0.1)

            if frame is not None:
                logging.info("[CAMERA] Camera stream started successfully.")

        # Calculate padding for letterbox resizing
        #scale = int(self.input_size) / max(imW, imH)
        #pad_x = (self.input_size - imW * scale) / 2  # Horizontal padding
        #pad_y = (self.input_size - imH * scale) / 2  # Vertical padding

            # Flag to ensure we run at least one inference to initialize the model
            first_run = True
            
            while not sigterm_monitor.stop_now and not self._stop_requested.is_set():
                # --- Detect camera config changes and re-init videostream if needed ---
                current_camera_source, current_ip_camera_url = _effective_camera_stream_config()
                current_enable_ip_camera_decode_scale_pipeline = CONFIG.get('ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE', False)
                current_ip_camera_target_resolution = CONFIG.get('IP_CAMERA_TARGET_RESOLUTION', '640x360')
                current_ip_camera_pipeline_fps_limit = int(CONFIG.get('IP_CAMERA_PIPELINE_FPS_LIMIT', 10) or 10)
                if (
                    (current_camera_source != last_camera_source)
                    or (current_ip_camera_url != last_ip_camera_url)
                    or (current_enable_ip_camera_decode_scale_pipeline != last_enable_ip_camera_decode_scale_pipeline)
                    or (current_ip_camera_target_resolution != last_ip_camera_target_resolution)
                    or (current_ip_camera_pipeline_fps_limit != last_ip_camera_pipeline_fps_limit)
                ):
                    tm.sleep(0.2)
                    logging.info(
                        "[MODEL] Detected change in camera stream settings. Reinitializing videostream..."
                    )
                    self.reinit_videostream()
                    last_camera_source = current_camera_source
                    last_ip_camera_url = current_ip_camera_url
                    last_enable_ip_camera_decode_scale_pipeline = current_enable_ip_camera_decode_scale_pipeline
                    last_ip_camera_target_resolution = current_ip_camera_target_resolution
                    last_ip_camera_pipeline_fps_limit = current_ip_camera_pipeline_fps_limit
                    # Wait for the new stream to warm up
                    frame = None
                    stream_start_time = tm.time()
                    while frame is None and not sigterm_monitor.stop_now and not self._stop_requested.is_set():
                        frame = videostream.read_oldest()
                        if tm.time() - stream_start_time > 10:
                            logging.error("[CAMERA] Camera stream failed to start within 10 seconds after reinit!")
                            break
                        else:
                            tm.sleep(0.1)
                    if frame is not None:
                        logging.info("[CAMERA] Camera stream re-initialized successfully.")

                # Start timer (for calculating frame rate)
                t1 = cv2.getTickCount()

                # Run at least one inference to initialize the model, even if paused
                if first_run or not self.paused:
                    # Grab frame from video stream
                    frame = videostream.read_oldest()

                if frame is not None:
                    # Run the CPU intensive model inference only if not paused
                    timestamp = tm.time()
                    try:
                        timestamp_mono = tm.monotonic()
                    except Exception:
                        timestamp_mono = None

                    if self.model == "tflite":
                        own_cat_probability = 0 # Not supported in the original Kittyflap TFLite models
                        mouse_probability, no_mouse_probability, detected_objects = self._process_frame_tflite(frame, interpreter)
                    elif self.model == "yolo":
                        # Resize the frame to the model input size (keeping aspect ratio)
                        resized_frame = self.letterbox(frame, self.input_size)
                        
                        if hasattr(self, '_model_worker') and self._model_worker:
                            # Using multiprocessing approach
                            job_id = self._yolo(resized_frame, self.input_size)
                            result = self._get_result(job_id)
                            
                            if result:
                                mouse_probability, own_cat_probability, obj_list = result
                                no_mouse_probability = 0
                                
                                detected_objects = []
                                for obj in obj_list:
                                    detected_objects.append(DetectedObject(
                                        obj['x'], obj['y'], obj['w'], obj['h'], obj['name'], obj['probability']
                                    ))
                            else:
                                # No result available yet
                                mouse_probability = 0
                                no_mouse_probability = 0
                                own_cat_probability = 0
                                detected_objects = []
                        else:
                            # Direct inference approach
                            mouse_probability, own_cat_probability, obj_list = self._yolo(resized_frame, self.input_size)
                            no_mouse_probability = 0
                            
                            detected_objects = []
                            for obj in obj_list:
                                detected_objects.append(DetectedObject(
                                    obj['x'], obj['y'], obj['w'], obj['h'], obj['name'], obj['probability']
                                ))
                    
                    if not first_run:
                        image_buffer.append(timestamp, self.encode_jpg_image(frame), None, 
                                            mouse_probability, no_mouse_probability, own_cat_probability, detected_objects=detected_objects,
                                            timestamp_mono=timestamp_mono)

                    # Calculate framerate
                    t2 = cv2.getTickCount()
                    time1 = (t2 - t1) / freq
                    frame_rate_calc = 1 / time1

                    # Track effective FPS (frames actually processed over time) independent of motion mode.
                    if not hasattr(self, '_last_model_log_time'):
                        self._last_model_log_time = tm.time()
                        self._frame_count_since_log = 0
                        self._fps_sum_since_log = 0.0
                    self._frame_count_since_log += 1
                    self._fps_sum_since_log += float(frame_rate_calc)

                    now = tm.time()
                    if now - self._last_model_log_time >= 60:
                        interval_s = max(0.001, float(now - self._last_model_log_time))
                        effective_processing_fps = (
                            float(self._frame_count_since_log) / interval_s
                            if self._frame_count_since_log > 0
                            else 0.0
                        )
                        avg_inference_fps = (
                            self._fps_sum_since_log / self._frame_count_since_log
                            if self._frame_count_since_log > 0
                            else 0.0
                        )
                        with self._fps_lock:
                            self._last_effective_fps = float(effective_processing_fps)
                            self._last_avg_inference_fps = float(avg_inference_fps)
                            self._last_fps_update_tm = float(now)

                        if CONFIG.get('USE_CAMERA_FOR_MOTION_DETECTION', False):
                            logging.info(
                                f"[MODEL] Model processing: {self._frame_count_since_log} frames in last {interval_s:.0f}s, "
                                f"effective FPS: {effective_processing_fps:.2f}, avg inference FPS: {avg_inference_fps:.2f}"
                            )

                        self._last_model_log_time = now
                        self._frame_count_since_log = 0
                        self._fps_sum_since_log = 0.0
                    elif not CONFIG.get('USE_CAMERA_FOR_MOTION_DETECTION', False):
                        logging.debug(f"[MODEL] Model processing time: {time1:.2f} sec, Frame Rate: {frame_rate_calc:.2f} fps")

                    # Set first_run to False after processing the first frame
                    first_run = False

                else:
                    # Log warning only once every 10 seconds to avoid flooding the log
                    current_time = tm.time()
                    if current_time - self.last_log_time > 10:
                        logging.warning("[CAMERA] No frame received!")
                        self.last_log_time = current_time
            
                # To avoid intensive CPU load, wait here until we reached the desired framerate
                elapsed_time = (cv2.getTickCount() - t1) / freq
                effective_fps = float(self.framerate or 10)
                if is_remote_mode():
                    try:
                        effective_fps = float(CONFIG.get('REMOTE_INFERENCE_MAX_FPS', effective_fps) or effective_fps)
                    except Exception:
                        effective_fps = float(self.framerate or 10)

                # Clamp to sane values to avoid division-by-zero or extreme sleeps.
                if effective_fps < 1.0:
                    effective_fps = 1.0
                if effective_fps > 60.0:
                    effective_fps = 60.0

                sleep_time = max(0, (1.0 / effective_fps) - elapsed_time)
                tm.sleep(sleep_time)
        except Exception as e:
            logging.error(f"[MODEL] Unhandled error in model loop: {e}")
            import traceback
            logging.error(traceback.format_exc())
        finally:
            # Stop the video stream
            try:
                if videostream is not None:
                    videostream.stop()
            except Exception as e:
                logging.error(f"[MODEL] Error stopping videostream during shutdown: {e}")

            # Stop the worker process (if any)
            try:
                if hasattr(self, '_model_worker') and self._model_worker and self._model_worker.is_alive():
                    self._input_queue.put(None)
                    self._model_worker.join(timeout=2)
            except Exception:
                pass

            if not task_done_signaled:
                sigterm_monitor.signal_task_done()
                task_done_signaled = True

    def pause(self):
        logging.info("[MODEL] Pausing model processing.")
        self.paused = True

    def stop(self):
        logging.info("[MODEL] Stop requested.")
        self.paused = True
        self._stop_requested.set()

    def resume(self):
        logging.info("[MODEL] Resuming model processing.")
        # Update the list of the cat names
        self.cat_names = [cat_name.lower() for cat_name in get_cat_names_list(CONFIG['KITTYHACK_DATABASE_PATH'])]
        self.paused = False

    def get_effective_fps_snapshot(self) -> tuple[float | None, float]:
        """Return (effective_fps, last_update_time_s).

        The timestamp is based on `time.time()` (seconds since epoch).
        """
        with self._fps_lock:
            return self._last_effective_fps, float(self._last_fps_update_tm)

    def reinit_videostream(self):
        """
        Re-initialize the videostream if CAMERA_SOURCE or IP_CAMERA_URL has changed.
        Always uses the latest CONFIG values.
        """
        global videostream
        from src.baseconfig import CONFIG  # Ensure latest config is used

        # Stop the current videostream if it exists
        if videostream is not None:
            try:
                videostream.stop()
                # Stop journal monitor if switching to internal camera
                if CONFIG['CAMERA_SOURCE'] == "internal":
                    videostream.stop_journal_monitor()
                videostream = None
                logging.info("[MODEL] Stopped previous videostream.")
                tm.sleep(1.0) # Give some time for the stream to stop
            except Exception as e:
                logging.warning(f"[MODEL] Error stopping previous videostream: {e}")

        # Start a new videostream with the latest/effective config values
        effective_camera_source, effective_ip_camera_url = _effective_camera_stream_config()
        videostream = VideoStream(
            source=effective_camera_source,
            ip_camera_url=effective_ip_camera_url,
            use_ip_camera_decode_scale_pipeline=CONFIG.get('ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE', False),
            ip_camera_target_resolution=CONFIG.get('IP_CAMERA_TARGET_RESOLUTION', '640x360'),
            ip_camera_pipeline_fps_limit=int(CONFIG.get('IP_CAMERA_PIPELINE_FPS_LIMIT', 10) or 10),
        ).start()
        if is_remote_mode() and str(CONFIG.get('CAMERA_SOURCE') or '').strip().lower() == 'internal' and effective_camera_source == 'ip_camera':
            logging.info(f"[MODEL] Re-initialized videostream with implicit remote MJPEG relay source: {effective_ip_camera_url}.")
        elif CONFIG['CAMERA_SOURCE'] == "internal":
            logging.info(f"[MODEL] Re-initialized videostream with internal camera source.")
        else:
            logging.info(f"[MODEL] Re-initialized videostream with external camera source: {effective_ip_camera_url}.")

    def set_videostream_buffer_size(self, new_size: int):
        """
        Set the buffer size of the videostream.
        Args:
            new_size (int): The new buffer size to set.
        """
        global videostream
        if videostream is not None:
            videostream.set_buffer_size(new_size)
            logging.info(f"[MODEL] Changed videostream buffer size to {new_size}")
        else:
            logging.warning("[MODEL] Cannot set buffer size: videostream is not initialized.")

    def get_run_state(self):
        return not self.paused

    def _process_frame_tflite(self, frame: np.ndarray, interpreter: "Interpreter") -> tuple:
        """
        Process a single frame for object detection using TensorFlow Lite.
        This method handles the preprocessing of the frame, performs inference using the TensorFlow Lite
        interpreter, and processes the detection results.
        Args:
            frame (np.ndarray): Input frame in BGR format
            interpreter (tensorflow.lite.python.interpreter.Interpreter): TFLite interpreter
        Returns:
            tuple: Contains:
                - mouse_probability (float): Probability of mouse detection (0-100)
                - no_mouse_probability (float): Probability of no mouse present (0-100)
                - detected_objects (list): List of DetectedObject instances containing detection results
        """

        input_mean = 127.5
        input_std = 127.5

        if ('StatefulPartitionedCall' in self.tf_outname): # This is a TF2 model
            boxes_idx, classes_idx, scores_idx = 1, 3, 0
        elif ('detected_scores:0' in self.tf_outname):
            boxes_idx, classes_idx, scores_idx = 1, 2, 0
        else: # This is a TF1 model
            boxes_idx, classes_idx, scores_idx = 0, 1, 2
        

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        frame_resized = cv2.resize(frame_rgb, (self.tf_width, self.tf_height))
        input_data = np.expand_dims(frame_resized, axis=0)

        original_h, original_w, __ = frame.shape

        if self.tf_floating_model:
            input_data = (np.float32(input_data) - input_mean) / input_std

        interpreter.set_tensor(self.tf_input_details[0]['index'], input_data)
        interpreter.invoke()

        boxes = interpreter.get_tensor(self.tf_output_details[boxes_idx]['index'])[0]
        classes = interpreter.get_tensor(self.tf_output_details[classes_idx]['index'])[0]
        scores = interpreter.get_tensor(self.tf_output_details[scores_idx]['index'])[0]

        if np.isscalar(scores):
            scores = np.array([scores])
        if np.isscalar(classes):
            classes = np.array([classes])
        if boxes.ndim == 1:
            boxes = np.expand_dims(boxes, axis=0)
        
        no_mouse_probability = 0.0
        mouse_probability = 0.0
        detected_objects = []

        for i in range(len(scores)):
            ymin = int(max(1, (boxes[i][0] * original_h)))
            xmin = int(max(1, (boxes[i][1] * original_w)))
            ymax = int(min(original_h, (boxes[i][2] * original_h)))
            xmax = int(min(original_w, (boxes[i][3] * original_w)))

            object_name = str(self.labels[int(classes[i])])
            probability = float(scores[i] * 100)
            
            detected_objects.append(DetectedObject(
                float(xmin / original_w * 100),
                float(ymin / original_h * 100),
                float((xmax - xmin) / original_w * 100),
                float((ymax - ymin) / original_h * 100),
                object_name,
                probability
            ))

            if object_name == "Maus":
                mouse_probability = int(probability)
            elif object_name == "Keine Maus":
                no_mouse_probability = int(probability)


        return mouse_probability, no_mouse_probability, detected_objects

    def get_camera_frame(self):
        if videostream is not None:
            return videostream.read()
        else:
            self._log_videostream_not_ready("Get Frame")
            return None
        
    def get_camera_state(self):
        if videostream is not None:
            return videostream.get_camera_state()
        else:
            self._log_videostream_not_ready("Get Camera State")
            return None
        
    def get_camera_resolution(self):
        """
        Returns the current camera resolution as a tuple (width, height).
        If the video stream is not initialized, returns None.
        """
        if videostream is not None:
            return videostream.get_resolution()
        else:
            self._log_videostream_not_ready("Get Camera Resolution")
            return None
        
    def check_videostream_status(self):
        """
        Checks if the video stream is running and returns its status.
        Returns:
            bool: True if the video stream is running, False otherwise.
        """
        global videostream
        if videostream is not None:
            return True
        else:
            return False
        
    def encode_jpg_image(self, decoded_image: cv2.typing.MatLike) -> bytes:
        """
        Encodes a decoded image into JPG format.
        Args:
            decoded_image (cv2.typing.MatLike): The image to be encoded, represented as a matrix.
        Returns:
            bytes: The encoded image in JPG format as a byte array.
        """
        # Encode the image as JPG
        _, buffer = cv2.imencode('.jpg', decoded_image)
        blob_data = buffer.tobytes()
        
        return blob_data
    
    def letterbox(self, img, target_size):
        """
        Resize the image to the target size while maintaining the aspect ratio.
        Pads the image with a gray background if necessary.
        Args:
            img (np.ndarray): The input image to be resized.
            target_size (int): The target size for the output image.
        Returns:
            np.ndarray: The resized image with padding if necessary.
        """

        h, w = img.shape[:2]
        scale = target_size / max(h, w)
        new_w, new_h = int(w * scale), int(h * scale)
        resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((target_size, target_size, 3), 114, dtype=np.uint8)
        top = (target_size - new_h) // 2
        left = (target_size - new_w) // 2
        canvas[top:top+new_h, left:left+new_w] = resized
        return canvas