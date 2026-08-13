"""Inference loop and videostream lifecycle."""
from __future__ import annotations

import logging
import multiprocessing
import os
import threading
import time as tm
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from src.baseconfig import CONFIG
from src.helper import sigterm_monitor
from src.database import CatsRepo
from src.camera import (
    DetectedObject,
    VideoStream,
    encode_frame_jpg,
    image_buffer,
    redact_camera_url,
    videostream,
)
from src.mode import is_remote_mode

from .camera_config import (
    _effective_camera_stream_config,
    _is_remote_internal_proxy_stream,
)
from .detection import _parse_yolo_detection_results

if TYPE_CHECKING:
    from ai_edge_litert.interpreter import Interpreter


def _yolo_model_worker_process(
    model_path,
    input_queue,
    output_queue,
    num_threads=1,
    inference_device="cpu",
):
    """YOLO inference subprocess (CPU-affinity-limited worker).

    Must stay at module level. Python 3.14 changed the default multiprocessing
    start method on Linux from ``fork`` to ``forkserver``, which pickles the
    process target. A nested ``load_model()`` function is not picklable and
    crashes ``Process.start()`` before the camera videostream is created.
    """
    try:
        import psutil
        from ultralytics import YOLO

        from src.baseconfig import CONFIG, configure_logging

        process = psutil.Process()

        requested = list(range(int(num_threads) if num_threads else 1))
        try:
            allowed = process.cpu_affinity()  # type: ignore[call-arg]
        except Exception:
            allowed = []

        cores_to_use = requested
        if allowed:
            cores_to_use = [c for c in requested if c in allowed]
            if not cores_to_use:
                cores_to_use = list(allowed)

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

        logging.getLogger("ultralytics").setLevel(logging.WARNING)
        logging.getLogger("ultralytics.yolo.engine.model").setLevel(logging.WARNING)
        model = YOLO(model_path, task="detect", verbose=False)
        configure_logging(CONFIG["LOGLEVEL"])

        while True:
            job = input_queue.get()
            if job is None:
                break

            job_id, frame, input_size, labels, cat_names, min_threshold = job

            _worker_kwargs: dict = dict(stream=True, imgsz=input_size)
            if inference_device != "cpu":
                _worker_kwargs["device"] = inference_device
            results = model(frame, **_worker_kwargs)

            mouse_probability, own_cat_probability, detected_objects = _parse_yolo_detection_results(
                results,
                labels,
                cat_names,
                min_threshold,
            )

            output_queue.put((job_id, mouse_probability, own_cat_probability, detected_objects))

    except Exception as e:
        logging.error(f"[MODEL] Worker process error: {e}")
        import traceback
        logging.error(traceback.format_exc())
    finally:
        logging.info("[MODEL] Worker process exiting")


class ModelHandler:
    """Runs TFLite/LiteRT or YOLO inference on the live camera videostream."""

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
        """Configure model paths, inference device, and live-frame cache."""
        self.labelfile = os.path.join(modeldir, labelfile)
        self.model = model
        self._model_rootdir = modeldir
        self.inference_device = str(CONFIG.get('INFERENCE_DEVICE', 'cpu') or 'cpu').strip()
        if not is_remote_mode():
            # GPU inference is only supported in remote-mode (i.e. on a capable Linux PC).
            # On the Kittyflap hardware (target-mode) always use CPU / NCNN.
            self.inference_device = 'cpu'
        if self.model == "tflite":
            self.modeldir = modeldir
        else:
            if self._uses_openvino_backend():
                # OpenVINO uses the exported IR model directory.
                self.modeldir = os.path.join(modeldir, "model_openvino_model")
            elif self.inference_device.lower() == 'cpu':
                # NCNN model: CPU-optimised, best for ARM / Pi
                self.modeldir = os.path.join(modeldir, "best_ncnn_model")
            else:
                # PyTorch model.pt: required for CUDA inference.
                self.modeldir = os.path.join(modeldir, "model.pt")
        self.graph = graph
        self.resolution = resolution
        self.framerate = framerate
        self.jpeg_quality = jpeg_quality
        self.paused = False
        self._live_frame_lock = threading.Lock()
        self._live_frame_jpg: bytes | None = None
        self._live_frame_jpg_ts: float = 0.0
        self.last_log_time = 0
        self._videostream_not_ready_last_log: dict[str, float] = {}
        self.input_size = int(model_image_size)
        self.num_threads = num_threads
        self.cat_names = [cat_name.lower() for cat_name in CatsRepo.get_cat_names_list(CONFIG['KITTYHACK_DATABASE_PATH'])]
        self._stop_requested = threading.Event()

        self._fps_lock = threading.Lock()
        self._last_effective_fps: float | None = None
        self._last_avg_inference_fps: float | None = None
        self._last_fps_update_tm: float = 0.0

        # Load labels early so the model loop cannot crash depending on whether a UI client
        # accessed the camera API during startup.
        self.labels: list[str] = []
        self._load_labels()

    def _uses_openvino_backend(self) -> bool:
        """True when CONFIG inference device should use OpenVINO."""
        device = str(self.inference_device or "").strip().lower()
        return device in {"gpu", "intel:gpu", "intel:cpu", "intel:npu"}

    def _resolved_inference_device(self) -> str:
        """Normalize inference device string (`gpu` → `intel:gpu`)."""
        device = str(self.inference_device or "cpu").strip().lower()
        if device == "gpu":
            return "intel:gpu"
        return device or "cpu"

    def _ensure_openvino_model_export(self) -> None:
        """Export model.pt to OpenVINO IR on demand and verify the output exists."""
        if not self._uses_openvino_backend():
            return

        xml_exists = os.path.isdir(self.modeldir) and any(
            name.endswith(".xml") for name in os.listdir(self.modeldir)
        )
        if xml_exists:
            return

        pt_model_path = os.path.join(self._model_rootdir, "model.pt")
        if not os.path.exists(pt_model_path):
            raise FileNotFoundError(f"OpenVINO export requires '{pt_model_path}'")

        logging.info(f"[MODEL] OpenVINO export missing. Exporting '{pt_model_path}' to '{self.modeldir}'...")

        from ultralytics import YOLO

        export_model = YOLO(pt_model_path, task="detect", verbose=False)
        export_result = export_model.export(
            format="openvino",
            imgsz=self.input_size,
        )

        if isinstance(export_result, str) and export_result.strip():
            self.modeldir = export_result

        xml_exists = os.path.isdir(self.modeldir) and any(
            name.endswith(".xml") for name in os.listdir(self.modeldir)
        )
        if not xml_exists:
            raise RuntimeError(f"OpenVINO export did not produce a valid model directory: {self.modeldir}")

    def _load_labels(self) -> None:
        """Load class labels from `self.labelfile` into `self.labels`."""
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
        """Rate-limited warning when a camera API is called before the stream exists."""
        try:
            now = tm.time()
        except Exception:
            now = 0.0
        last = float(self._videostream_not_ready_last_log.get(what, 0.0) or 0.0)
        if (now - last) >= float(interval_s):
            logging.warning(f"[CAMERA] '{what}' skipped. Video stream is not yet initialized.")
            self._videostream_not_ready_last_log[what] = now

    def load_model(self):
        """Load the TFLite interpreter or YOLO model (direct or worker process)."""
        if self.model == "tflite":
            # LiteRT is the maintained successor of tflite-runtime (required for Python >= 3.12).
            from ai_edge_litert.interpreter import Interpreter
            self._Interpreter = Interpreter
            self._yolo = None
            self._model_worker = None

        elif self.model == "yolo":
            from ultralytics import YOLO
            from src.baseconfig import configure_logging

            if self._uses_openvino_backend():
                self._ensure_openvino_model_export()

            resolved_inference_device = self._resolved_inference_device()

            # Check if we're using all available cores
            all_cores = multiprocessing.cpu_count()
            # GPU devices always use the direct inference path (GPU handles its own parallelism)
            using_all_cores = self.num_threads >= all_cores or self.inference_device.lower() != 'cpu'

            # If using all cores (or GPU), run the model directly for better performance
            if using_all_cores:
                if self.inference_device.lower() == 'cpu':
                    _device_label = 'cpu (NCNN)'
                elif self._uses_openvino_backend():
                    _device_label = f'{self.inference_device} via OpenVINO AUTO'
                else:
                    _device_label = self.inference_device
                logging.info(f"[MODEL] Loading YOLO model directly in main process (device={_device_label})")
                logging.getLogger("ultralytics").setLevel(logging.WARNING)
                logging.getLogger("ultralytics.yolo.engine.model").setLevel(logging.WARNING)
                self._yolo_model = YOLO(self.modeldir, task="detect", verbose=False)
                # Re-Configure logging to silence the model's output
                configure_logging(CONFIG['LOGLEVEL'])

                # Capture device info for the closure
                _inference_device = resolved_inference_device
                _is_openvino = self._uses_openvino_backend()

                # Create a wrapper function to match the expected interface
                def direct_inference(frame, input_size):
                    # Run inference directly. Ultralytics handles letterboxing internally.
                    # For OpenVINO models, do NOT pass device: Ultralytics hardcodes
                    # device_name="AUTO" internally, and select_device() does not
                    # understand Intel GPU strings (it only knows CUDA/CPU/MPS).
                    # OpenVINO AUTO mode automatically selects the best available
                    # device (GPU if available, CPU as fallback).
                    # For CUDA, pass the device explicitly as usual.
                    _predict_kwargs: dict = dict(stream=True, imgsz=input_size, verbose=False)
                    if _inference_device != 'cpu' and not _is_openvino:
                        _predict_kwargs['device'] = _inference_device
                    results = self._yolo_model(frame, **_predict_kwargs)
                    return _parse_yolo_detection_results(
                        results,
                        self.labels,
                        self.cat_names,
                        CONFIG.get('MIN_THRESHOLD', 0),
                    )
                
                self._yolo = direct_inference
                self._model_worker = None
            else:
                # Limited-core path: run YOLO in a child process with CPU affinity.
                # Use the default context (forkserver on Python 3.14 / Linux) with a
                # module-level target so the worker is picklable.
                ctx = multiprocessing.get_context()
                self._input_queue = ctx.Queue()
                self._output_queue = ctx.Queue()
                self._model_worker = ctx.Process(
                    target=_yolo_model_worker_process,
                    args=(self.modeldir, self._input_queue, self._output_queue, self.num_threads, resolved_inference_device),
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
        """Enqueue a frame for the YOLO worker process; return the job ID."""
        job_id = self._next_job_id
        self._next_job_id += 1
        self._input_queue.put((
            job_id, frame, input_size, self.labels, self.cat_names, CONFIG['MIN_THRESHOLD'],
        ))
        return job_id
    
    def _get_result(self, job_id, timeout=1.0):
        """Wait for and return the YOLO worker result for `job_id`, or None."""
        try:
            # Check if we've received the result
            if job_id in self._job_results:
                return self._job_results.pop(job_id)
            
            # Try to get new results from the output queue
            while True:
                try:
                    result_job_id, mouse_prob, cat_prob, objects = self._output_queue.get(timeout=timeout)
                except Exception:
                    return None

                # Drop results that arrived later than expected for an already-abandoned job_id.
                if result_job_id < job_id:
                    continue

                self._job_results[result_job_id] = (mouse_prob, cat_prob, objects)
                
                if result_job_id == job_id:
                    return self._job_results.pop(job_id)
        except Exception as e:
            logging.error(f"[MODEL] Error getting result: {e}")
            return None
    
    def __del__(self):
        """Signal the YOLO worker process to exit on object deletion."""
        if hasattr(self, '_model_worker') and self._model_worker and self._model_worker.is_alive():
            self._input_queue.put(None)  # Signal to exit
            self._model_worker.join(timeout=2)  # Give it 2 seconds to exit gracefully

    def run(self):
        """Main inference loop: load model, stream frames, detect, and buffer results."""
        global videostream
        global CONFIG

        self._stop_requested.clear()

        resW, resH = self.resolution.split('x')
        imW, imH = int(resW), int(resH)

        # Store the last used effective camera config to detect changes
        last_camera_source, last_ip_camera_url = _effective_camera_stream_config()
        last_enable_ip_camera_decode_scale_pipeline = CONFIG.get('ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE', False)
        last_ip_camera_target_resolution = CONFIG.get('IP_CAMERA_TARGET_RESOLUTION', '640x360')
        last_ip_camera_pipeline_fps_limit = int(CONFIG.get('IP_CAMERA_PIPELINE_FPS_LIMIT', 10) or 10)
        last_ip_camera_hw_decode = str(CONFIG.get('IP_CAMERA_HW_DECODE', 'auto') or 'auto')

        interpreter = None
        model_ready = False
        try:
            self.load_model()

            if self.model == "tflite":
                # ------------- TFLite Model -------------
                PATH_TO_TFLITE = os.path.join(self.modeldir, self.graph)

                logging.info(f"[MODEL] Preparing to run TFLite model {PATH_TO_TFLITE} on video stream with resolution {imW}x{imH} @ {self.framerate}fps and quality {self.jpeg_quality}%")

                interpreter = self._Interpreter(model_path=PATH_TO_TFLITE, num_threads=self.num_threads)
                interpreter.allocate_tensors()

                self.tf_input_details = interpreter.get_input_details()
                self.tf_output_details = interpreter.get_output_details()
                self.tf_height = self.tf_input_details[0]['shape'][1]
                self.tf_width = self.tf_input_details[0]['shape'][2]

                self.input_size = max(self.tf_height, self.tf_width)

                self.tf_floating_model = (self.tf_input_details[0]['dtype'] == np.float32)
                logging.info(f"[MODEL] Floating model: {self.tf_floating_model}")
                logging.info(f"[MODEL] Input details: {self.tf_input_details} (model shape: {self.tf_height}x{self.tf_width} --> {self.input_size})")

                self.tf_outname = self.tf_output_details[0]['name']
                model_ready = True

            elif self.model == "yolo":
                logging.info(f"[MODEL] Preparing to run YOLO model {self.modeldir} on video stream with resolution {imW}x{imH} @ {self.framerate}fps and quality {self.jpeg_quality}%")
                model_ready = True

            else:
                logging.error(f"[MODEL] Unknown model type: {self.model}. Failed to start inference.")
        except Exception as e:
            logging.error(f"[MODEL] Failed to load model; starting camera without inference: {e}")
            import traceback
            logging.error(traceback.format_exc())
        
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
                    logging.info(f"[CAMERA] Remote-mode implicit internal camera mapping active: {redact_camera_url(effective_ip_camera_url)}")
                elif effective_camera_source == 'disconnected':
                    logging.info("[CAMERA] Remote control disconnected. IP camera stream remains closed until reconnect.")
                else:
                    logging.warning("[CAMERA] Remote-mode internal camera selected, but REMOTE_TARGET_HOST is empty. Falling back to local internal source.")

            use_decode_scale_pipeline = bool(CONFIG.get('ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE', False))
            if _is_remote_internal_proxy_stream(effective_camera_source, effective_ip_camera_url):
                # Keep original target-side aspect ratio for implicit /video relay streams.
                # This avoids forcing 16:9 output (e.g. 640x360) for native 4:3 sources.
                use_decode_scale_pipeline = False

            videostream = VideoStream(
                source=effective_camera_source,
                ip_camera_url=effective_ip_camera_url,
                use_ip_camera_decode_scale_pipeline=use_decode_scale_pipeline,
                ip_camera_target_resolution=CONFIG.get('IP_CAMERA_TARGET_RESOLUTION', '640x360'),
                ip_camera_pipeline_fps_limit=int(CONFIG.get('IP_CAMERA_PIPELINE_FPS_LIMIT', 10) or 10),
                ip_camera_hw_decode=str(CONFIG.get('IP_CAMERA_HW_DECODE', 'auto') or 'auto'),
            ).start()
            logging.info(f"[CAMERA] Starting video stream...")

            # Wait for the camera to warm up
            detected_objects = []
            frame = None
            stream_start_time = tm.time()
            while frame is None and not sigterm_monitor.stop_now and not self._stop_requested.is_set():
                frame = videostream.read_oldest()
                if tm.time() - stream_start_time > 15:
                    logging.error("[CAMERA] Camera stream failed to start within 15 seconds!")
                    break
                else:
                    tm.sleep(0.1)

            if frame is not None:
                logging.info("[CAMERA] Camera stream started successfully.")

            # Flag to ensure we run at least one inference to initialize the model
            first_run = True
            
            # Recovery watchdog for rare camera handover races (e.g. remote-control takeover timeout).
            no_frame_reinit_after_s = 8.0
            no_frame_reinit_cooldown_s = 10.0
            no_frame_warn_after_s = 3.0
            last_good_frame_ts = tm.time()
            last_no_frame_reinit_ts = 0.0
            no_frame_since_ts = 0.0
            try:
                last_seen_camera_frame_id = int(videostream.get_latest_frame_id()) if videostream is not None else 0
            except Exception:
                last_seen_camera_frame_id = 0

            while not sigterm_monitor.stop_now and not self._stop_requested.is_set():
                # --- Detect camera config changes and re-init videostream if needed ---
                current_camera_source, current_ip_camera_url = _effective_camera_stream_config()
                current_enable_ip_camera_decode_scale_pipeline = CONFIG.get('ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE', False)
                current_ip_camera_target_resolution = CONFIG.get('IP_CAMERA_TARGET_RESOLUTION', '640x360')
                current_ip_camera_pipeline_fps_limit = int(CONFIG.get('IP_CAMERA_PIPELINE_FPS_LIMIT', 10) or 10)
                current_ip_camera_hw_decode = str(CONFIG.get('IP_CAMERA_HW_DECODE', 'auto') or 'auto')
                if (
                    (current_camera_source != last_camera_source)
                    or (current_ip_camera_url != last_ip_camera_url)
                    or (current_enable_ip_camera_decode_scale_pipeline != last_enable_ip_camera_decode_scale_pipeline)
                    or (current_ip_camera_target_resolution != last_ip_camera_target_resolution)
                    or (current_ip_camera_pipeline_fps_limit != last_ip_camera_pipeline_fps_limit)
                    or (current_ip_camera_hw_decode != last_ip_camera_hw_decode)
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
                    last_ip_camera_hw_decode = current_ip_camera_hw_decode
                    # Previous camera's frames must not trip the no-frame watchdog.
                    last_good_frame_ts = tm.time()
                    last_no_frame_reinit_ts = tm.time()
                    no_frame_since_ts = 0.0
                    try:
                        last_seen_camera_frame_id = int(videostream.get_latest_frame_id()) if videostream is not None else 0
                    except Exception:
                        last_seen_camera_frame_id = 0
                    # Wait for the new stream to warm up
                    frame = None
                    stream_start_time = tm.time()
                    while frame is None and not sigterm_monitor.stop_now and not self._stop_requested.is_set():
                        frame = videostream.read_oldest()
                        if tm.time() - stream_start_time > 15:
                            logging.error("[CAMERA] Camera stream failed to start within 15 seconds after reinit!")
                            break
                        else:
                            tm.sleep(0.1)
                    if frame is not None:
                        logging.info("[CAMERA] Camera stream re-initialized successfully.")
                        last_good_frame_ts = tm.time()

                # Start timer (for calculating frame rate)
                t1 = cv2.getTickCount()

                # When paused (after the initial warm-up iteration that primes the
                # YOLO worker so the next real inference is fast), skip the entire
                # inference + buffer-append work. Without this, the worker process
                # keeps re-processing the last frame at full FPS, pegging a CPU core
                # and leaking memory in self._job_results.
                if not first_run and self.paused:
                    elapsed_time = (cv2.getTickCount() - t1) / freq
                    sleep_time = max(0.0, 0.1 - elapsed_time)  # poll for resume at ~10 Hz
                    tm.sleep(sleep_time)
                    continue

                # Grab frame from video stream
                frame = videostream.read_oldest()

                if frame is not None:
                    last_good_frame_ts = tm.time()
                    no_frame_since_ts = 0.0
                    try:
                        current_latest_id = int(videostream.get_latest_frame_id()) if videostream is not None else 0
                        if current_latest_id > last_seen_camera_frame_id:
                            last_seen_camera_frame_id = current_latest_id
                    except Exception:
                        pass
                    # Run the CPU intensive model inference only if not paused
                    timestamp = tm.time()
                    try:
                        timestamp_mono = tm.monotonic()
                    except Exception:
                        timestamp_mono = None

                    mouse_probability = 0
                    no_mouse_probability = 0
                    own_cat_probability = 0
                    detected_objects = []

                    if model_ready and self.model == "tflite" and interpreter is not None:
                        own_cat_probability = 0 # Not supported in the original Kittyflap TFLite models
                        mouse_probability, no_mouse_probability, detected_objects = self._process_frame_tflite(frame, interpreter)
                    elif model_ready and self.model == "yolo" and getattr(self, "_yolo", None):
                        if hasattr(self, '_model_worker') and self._model_worker:
                            job_id = self._yolo(frame, self.input_size)
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
                                mouse_probability = 0
                                no_mouse_probability = 0
                                own_cat_probability = 0
                                detected_objects = []
                        else:
                            mouse_probability, own_cat_probability, obj_list = self._yolo(frame, self.input_size)
                            no_mouse_probability = 0

                            detected_objects = []
                            for obj in obj_list:
                                detected_objects.append(DetectedObject(
                                    obj['x'], obj['y'], obj['w'], obj['h'], obj['name'], obj['probability']
                                ))

                    if not first_run:
                        try:
                            frame_jpg = encode_frame_jpg(frame, jpeg_quality=self.jpeg_quality)
                        except Exception as e:
                            logging.error(f"[MODEL] Failed to encode frame to JPEG: {e}")
                            frame_jpg = None
                        if frame_jpg is not None:
                            with self._live_frame_lock:
                                self._live_frame_jpg = frame_jpg
                                self._live_frame_jpg_ts = tm.monotonic()
                            image_buffer.append(
                                timestamp, frame_jpg, None,
                                mouse_probability, no_mouse_probability, own_cat_probability,
                                detected_objects=detected_objects,
                                timestamp_mono=timestamp_mono,
                            )

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
                    current_time = tm.time()
                    # Distinguish between "no unread frame yet" and a truly stalled stream.
                    # read_oldest() may return None while the stream is still progressing.
                    stream_progressed = False
                    try:
                        current_latest_id = int(videostream.get_latest_frame_id()) if videostream is not None else 0
                        if current_latest_id > last_seen_camera_frame_id:
                            last_seen_camera_frame_id = current_latest_id
                            last_good_frame_ts = current_time
                            stream_progressed = True
                    except Exception:
                        pass

                    if stream_progressed:
                        no_frame_since_ts = 0.0
                    else:
                        if no_frame_since_ts <= 0.0:
                            no_frame_since_ts = current_time

                    if (
                        (not stream_progressed)
                        and (no_frame_since_ts > 0.0)
                        and ((current_time - no_frame_since_ts) >= no_frame_warn_after_s)
                        and (current_time - self.last_log_time > 20)
                    ):
                        logging.warning("[CAMERA] No frame received!")
                        self.last_log_time = current_time

                    # Auto-recover if stream got stuck after startup/handover.
                    if (
                        (not stream_progressed)
                        and
                        (current_time - last_good_frame_ts) >= no_frame_reinit_after_s
                        and (current_time - last_no_frame_reinit_ts) >= no_frame_reinit_cooldown_s
                    ):
                        logging.warning(
                            "[CAMERA] No frames for %.1fs. Reinitializing videostream...",
                            float(current_time - last_good_frame_ts),
                        )
                        try:
                            self.reinit_videostream()
                            last_no_frame_reinit_ts = current_time
                            last_good_frame_ts = tm.time()
                            no_frame_since_ts = 0.0
                            try:
                                last_seen_camera_frame_id = (
                                    int(videostream.get_latest_frame_id()) if videostream is not None else 0
                                )
                            except Exception:
                                last_seen_camera_frame_id = 0

                            # Warm-up probe after reinit
                            probe_deadline = tm.time() + 15.0
                            probe_frame = None
                            while (
                                probe_frame is None
                                and tm.time() < probe_deadline
                                and not sigterm_monitor.stop_now
                                and not self._stop_requested.is_set()
                            ):
                                probe_frame = videostream.read_oldest() if videostream is not None else None
                                if probe_frame is None:
                                    tm.sleep(0.1)

                            if probe_frame is not None:
                                frame = probe_frame
                                last_good_frame_ts = tm.time()
                                logging.info("[CAMERA] Videostream recovered after no-frame reinit.")
                            else:
                                logging.warning("[CAMERA] Videostream reinit did not recover frames yet.")
                        except Exception as e:
                            last_no_frame_reinit_ts = current_time
                            logging.warning(f"[CAMERA] Videostream reinit after no-frame period failed: {e}")
            
                # To avoid intensive CPU load, wait here until we reached the desired framerate
                elapsed_time = (cv2.getTickCount() - t1) / freq
                effective_fps = float(self.framerate or 10)
                if is_remote_mode():
                    try:
                        effective_fps = float(CONFIG.get('REMOTE_INFERENCE_MAX_FPS', effective_fps) or effective_fps)
                    except Exception:
                        effective_fps = float(self.framerate or 10)

                # Apply IP camera FPS cap independent of the decode+scale pipeline setting.
                # This keeps model processing aligned with camera cadence in non-pipeline mode.
                if str(CONFIG.get('CAMERA_SOURCE') or '').strip().lower() == 'ip_camera':
                    try:
                        ip_camera_fps_limit = float(CONFIG.get('IP_CAMERA_PIPELINE_FPS_LIMIT', effective_fps) or effective_fps)
                    except Exception:
                        ip_camera_fps_limit = effective_fps
                    if ip_camera_fps_limit > 0:
                        effective_fps = min(effective_fps, ip_camera_fps_limit)

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
        """Pause inference while keeping the loop and videostream alive."""
        logging.info("[MODEL] Pausing model processing.")
        self.paused = True

    def stop(self):
        """Request the inference loop to exit (also pauses processing)."""
        logging.info("[MODEL] Stop requested.")
        self.paused = True
        self._stop_requested.set()

    def resume(self):
        """Resume inference and refresh the configured cat-name list."""
        logging.info("[MODEL] Resuming model processing.")
        # Update the list of the cat names
        self.cat_names = [cat_name.lower() for cat_name in CatsRepo.get_cat_names_list(CONFIG['KITTYHACK_DATABASE_PATH'])]
        self.paused = False

    def get_effective_fps_snapshot(self) -> tuple[float | None, float]:
        """Return `(effective_fps, last_update_time_s)` where time is `time.time()`."""
        with self._fps_lock:
            return self._last_effective_fps, float(self._last_fps_update_tm)

    def get_fps_metrics_snapshot(self) -> tuple[float | None, float | None, float]:
        """Return `(effective_fps, avg_inference_fps, last_update_time_s)`."""
        with self._fps_lock:
            return (
                self._last_effective_fps,
                self._last_avg_inference_fps,
                float(self._last_fps_update_tm),
            )

    def reinit_videostream(self):
        """Stop and restart the videostream from the latest effective CONFIG."""
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
        use_decode_scale_pipeline = bool(CONFIG.get('ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE', False))
        if _is_remote_internal_proxy_stream(effective_camera_source, effective_ip_camera_url):
            # Preserve source aspect ratio for remote target /video relay.
            use_decode_scale_pipeline = False

        videostream = VideoStream(
            source=effective_camera_source,
            ip_camera_url=effective_ip_camera_url,
            use_ip_camera_decode_scale_pipeline=use_decode_scale_pipeline,
            ip_camera_target_resolution=CONFIG.get('IP_CAMERA_TARGET_RESOLUTION', '640x360'),
            ip_camera_pipeline_fps_limit=int(CONFIG.get('IP_CAMERA_PIPELINE_FPS_LIMIT', 10) or 10),
            ip_camera_hw_decode=str(CONFIG.get('IP_CAMERA_HW_DECODE', 'auto') or 'auto'),
        ).start()
        if is_remote_mode() and str(CONFIG.get('CAMERA_SOURCE') or '').strip().lower() == 'internal' and effective_camera_source == 'ip_camera':
            logging.info(f"[MODEL] Re-initialized videostream with implicit remote MJPEG relay source: {redact_camera_url(effective_ip_camera_url)}.")
        elif effective_camera_source == 'disconnected':
            logging.info("[MODEL] Re-initialized videostream in disconnected mode (IP camera stream closed).")
        elif CONFIG['CAMERA_SOURCE'] == "internal":
            logging.info(f"[MODEL] Re-initialized videostream with internal camera source.")
        else:
            logging.info(f"[MODEL] Re-initialized videostream with external camera source: {redact_camera_url(effective_ip_camera_url)}.")

    def set_videostream_buffer_size(self, new_size: int):
        """Set the videostream frame buffer size, if a stream is active."""
        global videostream
        if videostream is not None:
            videostream.set_buffer_size(new_size)
            logging.info(f"[MODEL] Changed videostream buffer size to {new_size}")
        else:
            logging.warning("[MODEL] Cannot set buffer size: videostream is not initialized.")

    def get_run_state(self):
        """Return True when inference is not paused."""
        return not self.paused

    def _process_frame_tflite(self, frame: np.ndarray, interpreter: "Interpreter") -> tuple:
        """Run TFLite detection on one BGR frame; return mouse/no-mouse probs and objects."""
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
        """Return the latest decoded camera frame, or None if the stream is not ready."""
        if videostream is not None:
            return videostream.read()
        self._log_videostream_not_ready("Get Frame")
        return None

    def get_camera_frame_jpg(self, jpeg_quality: int | None = None) -> bytes | None:
        """Return a JPEG of the latest camera frame (short-lived cache, refresh on demand)."""
        max_cache_age_s = 0.2
        now_mono = tm.monotonic()
        with self._live_frame_lock:
            if self._live_frame_jpg is not None and (now_mono - float(self._live_frame_jpg_ts or 0.0)) <= max_cache_age_s:
                return self._live_frame_jpg
        frame = self.get_camera_frame()
        if frame is None:
            return None
        quality = int(jpeg_quality if jpeg_quality is not None else self.jpeg_quality)
        try:
            jpg = encode_frame_jpg(frame, jpeg_quality=quality)
            with self._live_frame_lock:
                self._live_frame_jpg = jpg
                self._live_frame_jpg_ts = now_mono
            return jpg
        except Exception as e:
            logging.error(f"[MODEL] Failed to encode live-view JPEG: {e}")
            return None
        
    def get_camera_state(self):
        """Return videostream camera state, or None if the stream is not initialized."""
        if videostream is not None:
            return videostream.get_camera_state()
        else:
            self._log_videostream_not_ready("Get Camera State")
            return None
        
    def get_camera_resolution(self):
        """Return `(width, height)` from the videostream, or None if not initialized."""
        if videostream is not None:
            return videostream.get_resolution()
        else:
            self._log_videostream_not_ready("Get Camera Resolution")
            return None
        
    def check_videostream_status(self):
        """Return True if a videostream object exists (not necessarily streaming frames)."""
        global videostream
        if videostream is not None:
            return True
        else:
            return False
        
    def encode_jpg_image(self, decoded_image: cv2.typing.MatLike) -> bytes:
        """Encode a decoded image to JPEG using this handler's quality setting."""
        return encode_frame_jpg(decoded_image, jpeg_quality=self.jpeg_quality)