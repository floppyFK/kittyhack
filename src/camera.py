# This code is based and inspired from the great Tensorflow + CV2 examples from Evan Juras:
# https://github.com/EdjeElectronics/TensorFlow-Lite-Object-Detection-on-Android-and-Raspberry-Pi/


# Import packages
import cv2
import numpy as np
import subprocess
import shlex
import threading
import logging
from typing import List, Optional
class VideoStream:
    # FIXME: self.frame shall be changed to a list of frames. The last frame is always the most recent one. The list can be used to store the last N frames. If the list is full, the oldest frame is removed.
    """Camera object that controls video streaming from the Picamera"""
    def __init__(self, resolution=(640, 480), framerate=15, jpeg_quality=75, tuning_file="/usr/share/libcamera/ipa/rpi/vc4/ov5647_noir.json"):
        self.resolution = resolution
        self.framerate = framerate
        self.jpeg_quality = jpeg_quality
        self.tuning_file = tuning_file  # Path to the tuning file
        self.stopped = False
        self.frames = []
        self.buffer_size = 30
        self.process = None
        self.lock = threading.Lock()

    def start(self):
        # Start the thread that reads frames from the video stream
        threading.Thread(target=self.update, args=(), daemon=True).start()
        return self

    def update(self):
        # Include the tuning file in the command if specified
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

        if self.stopped:
            final_frame = np.zeros((self.resolution[1], self.resolution[0], 3), dtype=np.uint8)
            # Calculate text size
            text = "Stream Ended. Please restart the Kittyflap."
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
        if self.process:
            self.process.terminate()
            logging.info("[CAMERA] Video stream stopped.")
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
    MAX_IMAGE_BUFFER_SIZE = 2000

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

        # Periodic log for appended images and max probabilities
        if timestamp - self._last_log_time >= 60:
            logging.info(
                f"[IMAGEBUFFER] {self._appended_count} images appended in last 60s. "
                f"Max Mouse prob: {self._max_mouse_prob}, "
                f"Max No-mouse prob: {self._max_no_mouse_prob}, "
                f"Max Own-cat prob: {self._max_own_cat_prob}"
            )
            self._last_log_time = timestamp
            self._appended_count = 0
            self._max_mouse_prob = 0.0
            self._max_no_mouse_prob = 0.0
            self._max_own_cat_prob = 0.0

        # Periodic log for discarded elements
        if timestamp - self._last_discard_log_time >= 60:
            if self._discarded_count > 0:
                logging.info(
                    f"[IMAGEBUFFER] {self._discarded_count} oldest elements discarded from buffer in last 60s (buffer full)."
                )
                self._discarded_count = 0
            self._last_discard_log_time = timestamp

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
                logging.info(f"[IMAGEBUFFER] Deleted element with ID {id} from the buffer.")
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



