"""Camera / model-handler runtime (create, start thread, hot-reload)."""
import logging
import multiprocessing
import threading
from threading import Lock

from src.baseconfig import CONFIG
from src.mode import is_remote_mode
from src.model import ModelHandler, YoloModel


def _get_model_threads() -> int:
    """Return inference thread count (all CPUs in remote/all-cores mode, else 1)."""
    if is_remote_mode() or CONFIG['USE_ALL_CORES_FOR_IMAGE_PROCESSING']:
        return multiprocessing.cpu_count()
    return 1


def _create_model_handler_from_config() -> ModelHandler:
    """Build a TFLite or YOLO ``ModelHandler`` from the current CONFIG selection."""
    threads = _get_model_threads()
    if CONFIG['TFLITE_MODEL_VERSION']:
        logging.info(f"[BACKEND] Using TFLite model version {CONFIG['TFLITE_MODEL_VERSION']}")
        return ModelHandler(
            model="tflite",
            modeldir=f"./tflite/{CONFIG['TFLITE_MODEL_VERSION']}",
            graph="cv-lite-model.tflite",
            labelfile="labels.txt",
            model_image_size=320,
            num_threads=threads,
        )

    yolo_model_path = YoloModel.get_model_path(CONFIG['YOLO_MODEL'])
    yolo_model_image_size = YoloModel.get_model_image_size(CONFIG['YOLO_MODEL'])
    logging.info(f"[BACKEND] Using YOLO model {yolo_model_path}")
    return ModelHandler(
        model="yolo",
        modeldir=yolo_model_path,
        graph="",
        labelfile="labels.txt",
        resolution="800x600",
        framerate=10,
        jpeg_quality=75,
        model_image_size=(yolo_model_image_size if yolo_model_image_size else 320),
        num_threads=threads,
    )


# Initialize model runtime
model_handler = _create_model_handler_from_config()
_camera_thread = None
_model_runtime_lock = Lock()


def _start_model_thread(start_paused: bool = True) -> bool:
    """Start the model-handler daemon thread; pause or resume per ``start_paused``."""
    global _camera_thread
    global model_handler

    try:
        _camera_thread = threading.Thread(target=model_handler.run, daemon=True)
        _camera_thread.start()
        if start_paused:
            model_handler.pause()
        else:
            model_handler.resume()
        return True
    except Exception as e:
        logging.error(f"[BACKEND] Failed to start model thread: {e}")
        return False


def reload_model_handler_runtime() -> tuple[bool, ModelHandler]:
    """Hot-reload the configured model; returns ``(success, active ModelHandler)``."""
    global model_handler
    global _camera_thread

    with _model_runtime_lock:
        previous_handler = model_handler
        previous_thread = _camera_thread
        start_paused = bool(getattr(previous_handler, "paused", True))

        try:
            next_handler = _create_model_handler_from_config()
        except Exception as e:
            logging.error(f"[BACKEND] Failed to create new model handler: {e}")
            return False, model_handler

        try:
            if previous_handler is not None:
                previous_handler.stop()
        except Exception as e:
            logging.warning(f"[BACKEND] Failed to request stop for previous model handler: {e}")

        if previous_thread is not None and previous_thread.is_alive():
            previous_thread.join(timeout=12)
            if previous_thread.is_alive():
                logging.warning("[BACKEND] Previous model thread did not stop within timeout.")

        model_handler = next_handler
        started = _start_model_thread(start_paused=start_paused)
        if not started:
            logging.error("[BACKEND] New model thread failed to start after reload.")
            return False, model_handler

        logging.info("[BACKEND] Model handler reloaded successfully.")
        return True, model_handler
