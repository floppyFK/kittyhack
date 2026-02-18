# This code is based and inspired from the great Tensorflow + CV2 examples from Evan Juras:
# https://github.com/EdjeElectronics/TensorFlow-Lite-Object-Detection-on-Android-and-Raspberry-Pi/


# Import packages
import cv2
import numpy as np
import subprocess
import re
import shlex
import threading
import logging
import time as tm
import os
import shutil
from typing import List, Optional
from src.baseconfig import CONFIG
class VideoStream:
    """Camera object that controls video streaming from the Picamera or an IP camera"""

    # Camera state constants
    STATE_INITIALIZING = "initializing"
    STATE_RUNNING = "running"
    STATE_ERROR = "error"
    STATE_STOPPED = "stopped"
    STATE_INTERNAL = "internal_camera"
    STATE_IP_CAMERA = "ip_camera"

    def __init__(
        self,
        resolution=(800, 600),
        framerate=10,
        jpeg_quality=75,
        tuning_file="/usr/share/libcamera/ipa/rpi/vc4/ov5647_noir.json",
        source="internal",  # "internal" or "ip_camera"
        ip_camera_url: str = None,
        use_ip_camera_decode_scale_pipeline: bool = False,
        ip_camera_target_resolution: str = "640x360",
        ip_camera_pipeline_fps_limit: int = 10,
    ):
        self.resolution = resolution
        self.framerate = framerate
        self.jpeg_quality = jpeg_quality
        self.tuning_file = tuning_file  # Path to the tuning file
        self.stopped = False
        self.frames = []
        self.buffer_size = 30
        self.process = None
        self.lock = threading.Lock()
        self.source = source
        self.ip_camera_url = ip_camera_url
        self.use_ip_camera_decode_scale_pipeline = use_ip_camera_decode_scale_pipeline
        self.ip_camera_target_resolution = ip_camera_target_resolution
        self.ip_camera_pipeline_fps_limit = ip_camera_pipeline_fps_limit
        self.cap = None  # For IP camera
        self.thread = None
        self.camera_state = self.STATE_INITIALIZING  # <-- Add this line

    def _parse_target_resolution(self) -> tuple[int, int]:
        """Parse WxH target resolution string with a safe fallback."""
        default_resolution = (640, 360)
        try:
            match = re.match(r"^\s*(\d{2,5})x(\d{2,5})\s*$", str(self.ip_camera_target_resolution or ""), re.IGNORECASE)
            if not match:
                return default_resolution
            width = int(match.group(1))
            height = int(match.group(2))
            if width < 64 or height < 64:
                return default_resolution
            return (width, height)
        except Exception:
            return default_resolution

    def _ensure_ffmpeg_installed(self) -> bool:
        """Ensure ffmpeg is available. Try to install it on Debian-based systems if missing."""
        if shutil.which("ffmpeg"):
            return True

        logging.warning("[CAMERA] ffmpeg not found. Attempting to install it...")

        apt_update_cmd = ["apt-get", "update"]
        apt_install_cmd = ["apt-get", "install", "-y", "ffmpeg"]

        if os.geteuid() != 0:
            if shutil.which("sudo"):
                apt_update_cmd = ["sudo", *apt_update_cmd]
                apt_install_cmd = ["sudo", *apt_install_cmd]
            else:
                logging.error("[CAMERA] ffmpeg is missing and sudo is not available for installation.")
                return False

        try:
            update_proc = subprocess.run(
                apt_update_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
                check=False,
            )
            if update_proc.returncode != 0:
                logging.error(f"[CAMERA] Failed to update package index for ffmpeg install: {update_proc.stderr.strip()}")
                return False

            install_proc = subprocess.run(
                apt_install_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=900,
                check=False,
            )
            if install_proc.returncode != 0:
                logging.error(f"[CAMERA] Failed to install ffmpeg: {install_proc.stderr.strip()}")
                return False
        except Exception as e:
            logging.error(f"[CAMERA] Error while installing ffmpeg: {e}")
            return False

        installed = shutil.which("ffmpeg") is not None
        if installed:
            logging.info("[CAMERA] ffmpeg installed successfully.")
        else:
            logging.error("[CAMERA] ffmpeg installation command finished, but ffmpeg is still unavailable.")
        return installed

    def _normalized_pipeline_fps_limit(self) -> int:
        """Normalize the configured pipeline FPS limit. 0 means unlimited."""
        try:
            value = int(self.ip_camera_pipeline_fps_limit)
        except Exception:
            return 10
        if value in (0, 5, 10, 15, 20, 25):
            return value
        return 10

    def get_camera_state(self):
        """Return the current camera connection state."""
        return self.camera_state
    
    def get_resolution(self):
        """Return the current camera resolution as (width, height)."""
        return self.resolution

    def set_buffer_size(self, new_size: int):
        """Dynamically set the buffer size and trim frames if necessary."""
        if new_size < 1:
            raise ValueError("Buffer size must be at least 1")
        with self.lock:
            self.buffer_size = new_size
            if len(self.frames) > self.buffer_size:
                # Remove oldest frames to fit the new buffer size
                self.frames = self.frames[-self.buffer_size:]
        logging.info(f"[CAMERA] Buffer size set to {self.buffer_size}")


    def start(self):
        self.camera_state = self.STATE_INITIALIZING
        self.stopped = False
        self.thread = threading.Thread(target=self.update, args=(), daemon=True)
        self.thread.start()
        if self.source == "ip_camera":
            self._start_journal_monitor()
        return self
    
    def stop_journal_monitor(self):
        # Signal the monitor thread to stop
        self._journal_monitor_stopped = True
        if hasattr(self, 'journal_monitor_thread') and self.journal_monitor_thread is not None:
            self.journal_monitor_thread.join(timeout=2)
            self.journal_monitor_thread = None

    def _start_journal_monitor(self):
        self._journal_monitor_stopped = False
        self.journal_monitor_thread = threading.Thread(target=self._monitor_journal_for_h264_errors, daemon=True)
        self.journal_monitor_thread.start()

    def _monitor_journal_for_h264_errors(self, threshold=5, interval=20):
        error_count = 0
        last_reset = tm.time()
        pattern = re.compile(r"\[h264 @.*error while decoding MB")
        proc = subprocess.Popen(
            ["journalctl", "-u", "kittyhack.service", "-f", "-p", "info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        while not self.stopped and not getattr(self, '_journal_monitor_stopped', False):
            line = proc.stdout.readline()
            if not line:
                continue
            if pattern.search(line):
                error_count += 1
                logging.warning(f"[CAMERA] Journal detected H264 decode error (count={error_count})")
                if error_count >= threshold:
                    if CONFIG['RESTART_IP_CAMERA_STREAM_ON_FAILURE']:
                        logging.error("[CAMERA] Too many H264 errors detected in journal, reconnecting IP camera stream...")
                        self._trigger_ip_camera_reconnect()
                    else:
                        logging.warning("[CAMERA] Too many H264 errors detected, but automatic IP camera reconnect is disabled by configuration.")
                    error_count = 0
                    last_reset = tm.time()
            if tm.time() - last_reset > interval:
                error_count = 0
                last_reset = tm.time()
        proc.terminate()

    def _trigger_ip_camera_reconnect(self):
        # Set stopped to True to break the update loop and reconnect
        self.stopped = True
        # Wait a moment before restarting
        tm.sleep(2)
        self.stopped = False
        self.thread = threading.Thread(target=self.update, args=(), daemon=True)
        self.thread.start()

    def update(self):
        if self.source == "internal":
            self.camera_state = self.STATE_INTERNAL
            # Internal Raspberry Pi camera via libcamera-vid
            tuning_option = f"--tuning-file {self.tuning_file}" if self.tuning_file else ""
            command = (
                f"/usr/bin/libcamera-vid -t 0 --inline --width {self.resolution[0]} "
                f"--height {self.resolution[1]} --framerate {self.framerate} "
                f"--codec mjpeg --quality {self.jpeg_quality} {tuning_option} -o -"
            )
            logging.info(f"[CAMERA] Running command: {command}")

            self.process = subprocess.Popen(
                shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=4096 * 10000
            )
            logging.info(f"[CAMERA] Subprocess started: {self.process.pid}")
            buffer = b""
            try:
                self.camera_state = self.STATE_RUNNING
                while not self.stopped:
                    chunk = self.process.stdout.read(4096)
                    if not chunk:
                        logging.warning("[CAMERA] Stream ended unexpectedly")
                        break
                    buffer += chunk

                    # Extract JPEG frames
                    while b'\xff\xd8' in buffer and b'\xff\xd9' in buffer:
                        start = buffer.find(b'\xff\xd8')  # Start of JPEG
                        end = buffer.find(b'\xff\xd9') + 2  # End of JPEG
                        jpeg_data = buffer[start:end]
                        buffer = buffer[end:]

                    # Decode the JPEG frame
                        frame = cv2.imdecode(np.frombuffer(jpeg_data, np.uint8), cv2.IMREAD_COLOR)
                        if frame is not None:
                            frame = cv2.rotate(frame, cv2.ROTATE_180)
                            with self.lock:
                                self.frames.append(frame)
                                if len(self.frames) > self.buffer_size:
                                    self.frames.pop(0)  # Remove oldest frame
                        else:
                            logging.error("[CAMERA] Failed to decode frame")
            except Exception as e:
                logging.error(f"[CAMERA] Internal camera error: {e}")
                self.camera_state = self.STATE_ERROR
        elif self.source == "ip_camera" and self.ip_camera_url:
            self.camera_state = self.STATE_IP_CAMERA
            retry_delay = 5  # seconds
            corrupt_frame_count = 0
            max_corrupt_frames = 5  # reconnect after 5 consecutive corrupt frames

            # Define maximum allowed resolutions for common aspect ratios
            MAX_PIXELS = 1280 * 720  # 921600
            MAX_RESOLUTIONS = [
                ((16, 9), (1280, 720)),
                ((4, 3), (1024, 768)),
                ((5, 4), (960, 768)),
                ((3, 2), (1080, 720)),
                ((1, 1), (850, 850)),
            ]

            def get_max_resolution(width, height):
                # Find the closest aspect ratio and its max resolution
                aspect = width / height
                best_diff = float('inf')
                best_res = (1280, 720)  # fallback
                for (ar_w, ar_h), (max_w, max_h) in MAX_RESOLUTIONS:
                    ar = ar_w / ar_h
                    diff = abs(aspect - ar)
                    if diff < best_diff:
                        best_diff = diff
                        best_res = (max_w, max_h)
                return best_res

            while not self.stopped:
                logging.info(f"[CAMERA] Connecting to IP camera at {self.ip_camera_url}")
                self.camera_state = self.STATE_INITIALIZING

                # Optional ffmpeg decode+scale pipeline for IP streams
                if self.use_ip_camera_decode_scale_pipeline:
                    if not self._ensure_ffmpeg_installed():
                        logging.error("[CAMERA] FFmpeg decode+scale pipeline enabled, but ffmpeg is unavailable.")
                        self.camera_state = self.STATE_ERROR
                        if self.stopped:
                            break
                        tm.sleep(retry_delay)
                        continue

                    target_w, target_h = self._parse_target_resolution()
                    fps_limit = self._normalized_pipeline_fps_limit()
                    if fps_limit > 0:
                        vf_arg = f"fps={fps_limit},scale={target_w}:{target_h}:flags=fast_bilinear"
                    else:
                        vf_arg = f"scale={target_w}:{target_h}:flags=fast_bilinear"
                    ffmpeg_cmd = [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel", "error",
                        "-fflags", "nobuffer",
                        "-flags", "low_delay",
                    ]
                    if str(self.ip_camera_url).lower().startswith("rtsp://"):
                        ffmpeg_cmd.extend(["-rtsp_transport", "tcp"])
                    ffmpeg_cmd.extend([
                        "-i", self.ip_camera_url,
                        "-an",
                        "-sn",
                        "-dn",
                        "-vf", vf_arg,
                        "-pix_fmt", "bgr24",
                        "-f", "rawvideo",
                        "pipe:1",
                    ])

                    logging.info(
                        f"[CAMERA] Starting FFmpeg pipeline for IP camera at target resolution {target_w}x{target_h}, fps_limit={'unlimited' if fps_limit == 0 else fps_limit}"
                    )
                    try:
                        self.process = subprocess.Popen(
                            ffmpeg_cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            bufsize=target_w * target_h * 3 * 2,
                        )
                    except Exception as e:
                        logging.error(f"[CAMERA] Failed to start FFmpeg IP pipeline: {e}")
                        self.camera_state = self.STATE_ERROR
                        if self.stopped:
                            break
                        tm.sleep(retry_delay)
                        continue

                    self.resolution = (target_w, target_h)
                    frame_bytes = target_w * target_h * 3
                    self.camera_state = self.STATE_RUNNING

                    while not self.stopped:
                        try:
                            raw = self.process.stdout.read(frame_bytes) if self.process and self.process.stdout else b""
                        except Exception as e:
                            logging.error(f"[CAMERA] FFmpeg pipeline read error: {e}")
                            raw = b""

                        if len(raw) != frame_bytes:
                            corrupt_frame_count += 1
                            logging.warning(
                                f"[CAMERA] Incomplete frame from FFmpeg pipeline (count={corrupt_frame_count}, got={len(raw)}/{frame_bytes})"
                            )
                            if corrupt_frame_count >= max_corrupt_frames:
                                logging.error("[CAMERA] Too many incomplete frames from FFmpeg pipeline, reconnecting...")
                                self.camera_state = self.STATE_ERROR
                                break
                            continue

                        try:
                            frame = np.frombuffer(raw, dtype=np.uint8).reshape((target_h, target_w, 3))
                        except Exception as e:
                            corrupt_frame_count += 1
                            logging.warning(f"[CAMERA] Corrupt FFmpeg frame reshape error (count={corrupt_frame_count}): {e}")
                            if corrupt_frame_count >= max_corrupt_frames:
                                logging.error("[CAMERA] Too many corrupt FFmpeg frames, reconnecting...")
                                self.camera_state = self.STATE_ERROR
                                break
                            continue

                        corrupt_frame_count = 0
                        with self.lock:
                            self.frames.append(frame)
                            if len(self.frames) > self.buffer_size:
                                self.frames.pop(0)

                    try:
                        if self.process:
                            self.process.terminate()
                            try:
                                self.process.wait(timeout=2)
                            except Exception:
                                try:
                                    self.process.kill()
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    finally:
                        self.process = None

                    if self.stopped:
                        break
                    logging.info(f"[CAMERA] Reconnecting FFmpeg IP camera pipeline in {retry_delay}s...")
                    self.camera_state = self.STATE_INITIALIZING
                    tm.sleep(retry_delay)
                    continue

                self.cap = cv2.VideoCapture(self.ip_camera_url)
                if not self.cap.isOpened():
                    logging.error(f"[CAMERA] Failed to open IP camera stream: {self.ip_camera_url}. Retrying in {retry_delay}s...")
                    self.camera_state = self.STATE_ERROR
                    self.cap.release()
                    if self.stopped:
                        break
                    tm.sleep(retry_delay)
                    continue

                # Get the actual resolution of the IP camera
                width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                logging.info(f"[CAMERA] IP camera resolution: {width}x{height}")
                self.resolution = (width, height)  # Update to actual resolution

                if width * height > MAX_PIXELS:
                    logging.warning(f"[CAMERA] IP camera resolution {width}x{height} exceeds maximum allowed {MAX_PIXELS} pixels. The performance may be affected!")

                max_w, max_h = get_max_resolution(width, height)

                # Set desired resolution before reading frames
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, max_w)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, max_h)

                self.camera_state = self.STATE_RUNNING
                while not self.stopped:
                    ret, frame = self.cap.read()
                    if not ret or frame is None or frame.size == 0 or np.count_nonzero(frame) < frame.size * 0.01:
                        corrupt_frame_count += 1
                        logging.warning(f"[CAMERA] Corrupt frame detected from IP camera (count={corrupt_frame_count})")
                        if corrupt_frame_count >= max_corrupt_frames:
                            logging.error("[CAMERA] Too many corrupt frames, reconnecting IP camera stream...")
                            self.camera_state = self.STATE_ERROR
                            break  # Break inner loop to reconnect
                        continue
                    else:
                        corrupt_frame_count = 0  # Reset on good frame

                    with self.lock:
                        self.frames.append(frame)
                        if len(self.frames) > self.buffer_size:
                            self.frames.pop(0)
                self.cap.release()
                if self.stopped:
                    break
                logging.info(f"[CAMERA] Reconnecting to IP camera in {retry_delay}s...")
                self.camera_state = self.STATE_INITIALIZING
                tm.sleep(retry_delay)
        else:
            logging.error("[CAMERA] Invalid source or missing IP camera URL")
            self.camera_state = self.STATE_ERROR

        if self.stopped:
            self.camera_state = self.STATE_STOPPED
            final_frame = np.zeros((self.resolution[1], self.resolution[0], 3), dtype=np.uint8)
            # Calculate text size
            text = "Stream Ended."
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 1
            thickness = 2
            text_size = cv2.getTextSize(text, font, font_scale, thickness)[0]

            # Calculate text position
            text_x = (final_frame.shape[1] - text_size[0]) // 2
            text_y = (final_frame.shape[0] + text_size[1]) // 2

            # Draw background rectangle
            cv2.rectangle(final_frame, (text_x - 10, text_y - text_size[1] - 10),
                          (text_x + text_size[0] + 10, text_y + 10), (128, 128, 128), cv2.FILLED)
            
            # Draw the text itself
            cv2.putText(final_frame, text, (text_x, text_y), font, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)

            with self.lock:
                self.frames = [final_frame]
            with self.lock:
                self.frame = final_frame
            logging.info("[CAMERA] Added final frame to indicate stream ended.")

    def read(self):
        # Return the most recent frame
        with self.lock:
            return self.frames[-1] if self.frames else None

    def read_oldest(self):
        # Return and remove the oldest frame from the list, but keep the last frame
        with self.lock:
            if len(self.frames) > 1:
                return self.frames.pop(0)
            elif len(self.frames) == 1:
                return self.frames[0]
            else:
                return None

    def stop(self):
        # Stop the video stream
        self.stopped = True
        self.stop_journal_monitor()
        if self.process:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except Exception:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
            finally:
                self.process = None

        if self.thread is not None:
            self.thread.join(timeout=5)  # Wait for the thread to finish
            self.thread = None

        if self.source == "internal":
            logging.info("[CAMERA] Video stream stopped.")
        elif self.source == "ip_camera" and self.cap:
            self.cap.release()
            logging.info("[CAMERA] IP camera stream stopped.")
        else:
            logging.error("[CAMERA] Video stream not yet started. Nothing to stop.")

class DetectedObject:
    def __init__(self, x: float, y: float, width: float, height: float, object_name: str, probability: float):
        self.x = x  # x as percentage of image width
        self.y = y  # y as percentage of image height
        self.width = width  # width as percentage of image width
        self.height = height  # height as percentage of image height
        self.object_name = object_name
        self.probability = probability

class ImageBufferElement:
    def __init__(self, id: int, block_id: int, timestamp: float, original_image: bytes | None, modified_image: bytes | None, 
                 mouse_probability: float, no_mouse_probability: float, own_cat_probability: float, tag_id: str = "", detected_objects: List[DetectedObject] = None):
        self.id = id
        self.block_id = block_id
        self.timestamp = timestamp
        self.original_image = original_image
        self.modified_image = modified_image
        self.mouse_probability = mouse_probability
        self.no_mouse_probability = no_mouse_probability
        self.own_cat_probability = own_cat_probability
        self.tag_id = tag_id
        self.detected_objects = detected_objects

    def __repr__(self):
        return (f"ImageBufferElement(id={self.id}, block_id={self.block_id}, timestamp={self.timestamp}, mouse_probability={self.mouse_probability}, "
                f"no_mouse_probability={self.no_mouse_probability}, own_cat_probability={self.own_cat_probability}, tag_id={self.tag_id}, detected_objects={self.detected_objects})")

class ImageBuffer:
    MAX_IMAGE_BUFFER_SIZE = 1000

    def __init__(self):
        """Initialize an empty buffer."""
        self._buffer: List[ImageBufferElement] = []
        self._next_id = 0

    def append(self, timestamp: float, original_image: bytes | None, modified_image: bytes | None, 
               mouse_probability: float, no_mouse_probability: float, own_cat_probability: float, detected_objects: List[DetectedObject] = None):
        """
        Append a new element to the buffer.
        """
        # --- Periodic logging for discarded elements ---
        if not hasattr(self, '_last_log_time'):
            self._last_log_time = timestamp
            self._appended_count = 0
            self._max_mouse_prob = 0.0
            self._max_no_mouse_prob = 0.0
            self._max_own_cat_prob = 0.0
            self._discarded_count = 0
            self._last_discard_log_time = timestamp

        if len(self._buffer) >= self.MAX_IMAGE_BUFFER_SIZE:
            self._buffer.pop(0)
            self._discarded_count += 1

        element = ImageBufferElement(self._next_id, 0, timestamp, original_image, modified_image, mouse_probability, no_mouse_probability, own_cat_probability, detected_objects=detected_objects)
        self._buffer.append(element)

        self._appended_count += 1
        self._max_mouse_prob = max(self._max_mouse_prob, mouse_probability)
        self._max_no_mouse_prob = max(self._max_no_mouse_prob, no_mouse_probability)
        self._max_own_cat_prob = max(self._max_own_cat_prob, own_cat_probability)

        # Periodic combined log for appended images, max probabilities and discarded elements
        if (timestamp - self._last_log_time >= 60) or (timestamp - self._last_discard_log_time >= 60):
            parts = []

            if timestamp - self._last_log_time >= 60:
                parts.append(
                    f"{self._appended_count} images appended in last 60s. "
                    f"Max Mouse prob: {self._max_mouse_prob}, "
                    f"Max No-mouse prob: {self._max_no_mouse_prob}, "
                    f"Max Own-cat prob: {self._max_own_cat_prob}."
                )

            if timestamp - self._last_discard_log_time >= 60:
                if self._discarded_count > 0:
                    parts.append(
                        f"{self._discarded_count} oldest elements discarded from buffer in last 60s."
                    )
                else:
                    parts.append("No discarded elements in last 60s.")

            logging.info("[IMAGEBUFFER] " + " ".join(parts))

            # Reset counters/timestamps only for the sections we just logged
            if timestamp - self._last_log_time >= 60:
                self._last_log_time = timestamp
                self._appended_count = 0
                self._max_mouse_prob = 0.0
                self._max_no_mouse_prob = 0.0
                self._max_own_cat_prob = 0.0

            if timestamp - self._last_discard_log_time >= 60:
                self._last_discard_log_time = timestamp
                self._discarded_count = 0

        self._next_id += 1


    def pop(self) -> Optional[ImageBufferElement]:
        """
        Return and remove the last element from the buffer.

        Returns:
            Optional[ImageBufferElement]: The last element if the buffer is not empty, else None.
        """
        if self._buffer:
            logging.info(f"[IMAGEBUFFER] Popped element with ID {self._buffer[-1].id} from the buffer.")
            return self._buffer.pop()
        return None

    def clear(self):
        """Clear all elements in the buffer."""
        self._buffer.clear()

    def size(self) -> int:
        """
        Return the number of elements in the buffer.

        Returns:
            int: The number of elements in the buffer.
        """
        return len(self._buffer)

    def get_all(self) -> List[ImageBufferElement]:
        """
        Return all elements in the buffer.

        Returns:
            List[ImageBufferElement]: A list of all elements in the buffer.
        """
        return self._buffer[:]
    
    def get_by_id(self, id: int) -> Optional[ImageBufferElement]:
        """
        Return the element with the given ID.

        Args:
            id (int): The ID to search for.

        Returns:
            Optional[ImageBufferElement]: The element with the given ID if found, else None.
        """
        for element in self._buffer:
            if element.id == id:
                return element
        return None
    
    def delete_by_id(self, id: int) -> bool:
        """
        Delete the element with the given ID.

        Args:
            id (int): The ID to search for.

        Returns:
            bool: True if the element was deleted, else False.
        """
        for i, element in enumerate(self._buffer):
            if element.id == id:
                self._buffer.pop(i)
                logging.debug(f"[IMAGEBUFFER] Deleted element with ID {id} from the buffer.")
                return True
        logging.warning(f"[IMAGEBUFFER] Element with ID {id} not found in the buffer. Nothing was deleted.")
        return False
    
    def get_filtered_ids(self, min_timestamp=0.0, 
                         max_timestamp=float('inf'), 
                         min_mouse_probability=0.0, 
                         max_mouse_probability=100.0,
                         min_no_mouse_probability=0.0,
                         max_no_mouse_probability=100.0,
                         min_own_cat_probability=0.0,
                         max_own_cat_probability=100.0) -> List[int]:
        """
        Return the IDs of elements that match the given filter criteria.

        Args:
            min_timestamp (float): The minimum timestamp.
            max_timestamp (float): The maximum timestamp.
            min_mouse_probability (float): The minimum mouse probability.
            max_mouse_probability (float): The maximum mouse probability.
            min_no_mouse_probability (float): The minimum no mouse probability.
            max_no_mouse_probability (float): The maximum no mouse probability.
            min_own_cat_probability (float): The minimum own cat probability.
            max_own_cat_probability (float): The maximum own cat probability.

        Returns:
            List[int]: A list of IDs that match the filter criteria.
        """
        return [element.id for element in self._buffer if 
                (min_timestamp <= element.timestamp <= max_timestamp) and 
                (min_mouse_probability <= element.mouse_probability <= max_mouse_probability) and 
                (min_no_mouse_probability <= element.no_mouse_probability <= max_no_mouse_probability) and
                (min_own_cat_probability <= element.own_cat_probability <= max_own_cat_probability)]
    
    def update_block_id(self, id: int, block_id: int) -> bool:
        """
        Update the block ID of the element with the given ID.

        Args:
            id (int): The ID of the element to update.
            block_id (int): The new block ID.

        Returns:
            bool: True if the element was updated, else False.
        """
        for element in self._buffer:
            if element.id == id:
                element.block_id = block_id
                return True
        return False
    
    def update_tag_id(self, id: int, tag_id: str) -> bool:
        """
        Update the tag ID of the element with the given ID.

        Args:
            id (int): The ID of the element to update.
            tag_id (str): The new tag ID.

        Returns:
            bool: True if the element was updated, else False.
        """
        for element in self._buffer:
            if element.id == id:
                element.tag_id = tag_id
                return True
        return False
    
    def get_by_block_id(self, block_id: int) -> List[ImageBufferElement]:
        """
        Return all elements with the given block ID.

        Args:
            block_id (int): The block ID to search for.

        Returns:
            List[ImageBufferElement]: A list of elements with the given block ID.
        """
        return [element for element in self._buffer if element.block_id == block_id]

# Global variable declarations
image_buffer = ImageBuffer()
videostream = None