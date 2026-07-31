"""On-disk YOLO model metadata and install/delete helpers."""
import json
import logging
import os
import shutil
from datetime import datetime
from typing import Any

from src.paths import models_yolo_root
from src.helper import DateTimeUtil

from .json_util import _atomic_write_json

class YoloModel:
    """Filesystem helpers for installed YOLO models under `models/yolo`."""
    BASE_DIR = models_yolo_root()

    @staticmethod
    def get_supported_image_sizes() -> list[int]:
        """Return supported YOLO input sizes (320–640, multiples of 32)."""
        return list(range(320, 640 + 1, 32))

    @staticmethod
    def _normalize_model_image_size(raw_value: Any) -> int:
        """Parse and snap a raw image size to the nearest supported value."""
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
        """List installed YOLO models with display name, path, size, and FPS metadata."""
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
                    creation_date = datetime.fromtimestamp(creation_time, DateTimeUtil.get_timezone()).strftime("%Y-%m-%d %H:%M:%S")
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
                                creation_date = timestamp.astimezone(DateTimeUtil.get_timezone()).strftime("%Y-%m-%d %H:%M:%S")
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
        """Merge `updates` into the model's `info.json` (atomic write when possible)."""
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
        """Return the model directory path for `unique_id` (JOB_ID), or None."""
        model_list = YoloModel.get_model_list()
        for model in model_list:
            if model.get('unique_id') == unique_id:
                return os.path.join(YoloModel.BASE_DIR, model['directory'])
        return None
    
    @staticmethod
    def get_model_image_size(unique_id):
        """Return the model image size for `unique_id`, or None if not found."""
        model_list = YoloModel.get_model_list()
        for model in model_list:
            if model.get('unique_id') == unique_id:
                return model['model_image_size']
        return None
    
    @staticmethod
    def delete_model(unique_id):
        """Delete the YOLO model directory for `unique_id`; return True on success."""
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
        """Set MODEL_NAME in the model's `info.json` for `unique_id`; return True on success."""
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
