"""Remote model training jobs and download worker orchestration."""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
import time as tm
from typing import Any

import requests
from shiny import ui

from src.baseconfig import CONFIG, set_language, update_single_config_parameter, UserNotifications
from src.helper import (
    DateTimeUtil,
    is_valid_uuid4,
)
from src.paths import kittyhack_root, models_yolo_root

from .json_util import (
    _atomic_write_json,
    _default_download_state,
    _pid_alive,
    _read_json,
)
from .yolo_model import YoloModel

_ = set_language(CONFIG["LANGUAGE"])

_MODEL_DL_STATE_PATH = "/tmp/kittyhack_model_download_state.json"

class RemoteModelTrainer:
    """Remote training API client and local download/install orchestration."""

    BASE_URL = "https://kittyhack-models.fk-cloud.de"

    @staticmethod
    def get_model_download_state() -> dict[str, Any]:
        """Read download-worker state from disk; mark stale workers as error."""
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
        """Persist download-worker state (merged over defaults) to the state file."""
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
        """Start a background worker to download/extract a trained model; True if started."""
        state = RemoteModelTrainer.get_model_download_state()
        if state.get("status") == "done" and state.get("result_id") == result_id:
            return False
        if state.get("status") in ("downloading", "extracting") and _pid_alive(int(state.get("pid") or 0)):
            # Do not start a second concurrent worker.
            return False
        # Repo root (directory that contains `src/`). remote_trainer.py lives in
        # `src/model/`, so a single `..` would cwd into `src/` and
        # `python -m src.model_download_worker` fails with ModuleNotFoundError.
        root_dir = kittyhack_root()
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
        if state.get("retry_at"):
            init_state["retry_at"] = state.get("retry_at")
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
        """Return `{maintenance, message}` from the training server, or None on error."""
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
        """Upload a training zip; return job_id or an error status string."""
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
        """Fetch remote training status JSON for `job_id`, or None on error."""
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
        """Cancel a pending remote training job; return True on success."""
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
        """Download and extract a trained model zip under `models_yolo_root()`; return success."""
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
                sanitized = "model_" + datetime.now(DateTimeUtil.get_timezone()).strftime("%Y%m%d_%H%M%S")
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
        """Download the model zip for `result_id` to `/tmp` and return its path."""
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

        expected_sha256 = (response.headers.get("X-Model-SHA256") or "").strip().lower()
        delete_token = (response.headers.get("X-Delete-Token") or "").strip()
        try:
            expected_size = int(response.headers.get("X-Model-Size", "0") or "0")
        except Exception:
            expected_size = 0

        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if not chunk:
                    continue
                f.write(chunk)

        # Best-effort integrity check + acknowledge so server can delete the artifact.
        try:
            size_bytes = int(os.path.getsize(tmp_path))
            if expected_size and size_bytes != expected_size:
                raise RuntimeError("size_mismatch")

            import hashlib

            h = hashlib.sha256()
            with open(tmp_path, "rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(block)
            sha = h.hexdigest().lower()
            if expected_sha256 and sha != expected_sha256:
                raise RuntimeError("sha256_mismatch")

            if delete_token:
                ack_url = f"{RemoteModelTrainer.BASE_URL}/download/{result_id}/ack"
                payload = {"delete_token": delete_token, "sha256": sha, "size_bytes": size_bytes}
                ack_resp = requests.post(ack_url, json=payload, verify=True, timeout=(5, 30))
                if ack_resp.status_code != 200:
                    logging.warning(f"[MODEL_TRAINING] Download ack failed ({ack_resp.status_code}): {ack_resp.text}")
        except Exception as e:
            logging.warning(f"[MODEL_TRAINING] Download ack skipped: {e}")

        return tmp_path

    @staticmethod
    def _extract_model_zip(zip_path: str, model_name: str = "") -> tuple[bool, str, str]:
        """Extract `zip_path` under `models_yolo_root()`; return `(ok, target_dir, error)`."""
        # Sanitize model_name to avoid file system issues
        def sanitize_directory_name(name: str) -> str:
            name = (name or "").lower()
            sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
            sanitized = re.sub(r"_{2,}", "_", sanitized)
            sanitized = sanitized.strip().strip(".")
            if not sanitized:
                sanitized = "model_" + datetime.now(DateTimeUtil.get_timezone()).strftime("%Y%m%d_%H%M%S")
            return sanitized

        if not os.path.exists(zip_path):
            return False, "", "zip_missing"
        # Determine model name from info.json if needed
        info_json_model_name = None
        creation_date = datetime.now(DateTimeUtil.get_timezone()).strftime("%Y-%m-%d_%H-%M-%S")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                if "info.json" in zf.namelist():
                    with zf.open("info.json") as info_file:
                        info_data = json.load(info_file)
                        info_json_model_name = info_data.get("MODEL_NAME")
                        try:
                            timestamp = datetime.fromisoformat(info_data.get("TIMESTAMP_UTC"))
                            creation_date = timestamp.astimezone(DateTimeUtil.get_timezone()).strftime("%Y-%m-%d_%H:%M:%S")
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
        """Poll/finalize the active training job; return status or a pretty UI message."""
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
            elif state_matches and dl_status == "error":
                err = str(dl_state.get("error") or "")
                no_progress = int(dl_state.get("bytes_downloaded") or 0) == 0
                start_failed = (
                    "worker_died" in err
                    or "No module named" in err
                    or "failed_to_start_worker" in err
                )
                last_retry = float(dl_state.get("retry_at") or 0)
                can_retry = no_progress and start_failed and (tm.time() - last_retry) > 60
                if can_retry and dl_state.get("result_id"):
                    dl_state["retry_at"] = tm.time()
                    dl_state["finalized"] = False
                    try:
                        RemoteModelTrainer._write_model_download_state(dl_state)
                    except Exception:
                        pass
                    RemoteModelTrainer.start_download_model_async(
                        job_id,
                        str(dl_state.get("result_id")),
                    )
                    training_status = "downloading"
                else:
                    if not bool(dl_state.get("finalized")):
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
                        if not dl_state.get("training_job_id"):
                            dl_state["training_job_id"] = job_id
                        try:
                            RemoteModelTrainer._write_model_download_state(dl_state)
                        except Exception:
                            pass
                    training_status = "download_error"
            elif state_matches and dl_status == "done":
                # Finalize (clear config + add persistent notification) in the main process.
                if not bool(dl_state.get("finalized")):
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

                    dl_state["finalized"] = True
                    # Preserve job_id for future checks
                    if not dl_state.get("training_job_id"):
                        dl_state["training_job_id"] = job_id
                    try:
                        RemoteModelTrainer._write_model_download_state(dl_state)
                    except Exception:
                        pass

                training_status = "downloaded"
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
