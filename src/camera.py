# This code is based and inspired from the great Tensorflow + CV2 examples from Evan Juras:
# https://github.com/EdjeElectronics/TensorFlow-Lite-Object-Detection-on-Android-and-Raspberry-Pi/


# Import packages
import os
import cv2
import numpy as np
import subprocess
import shlex
import time as tm
import threading
import importlib.util
import logging
import multiprocessing
import concurrent.futures
from typing import List, Optional
from src.helper import CONFIG, sigterm_monitor

# Import TensorFlow libraries
# If tflite_runtime is installed, import interpreter from tflite_runtime, else import from regular tensorflow
pkg = importlib.util.find_spec('tflite_runtime')
if pkg:
    from tflite_runtime.interpreter import Interpreter
else:
    from tensorflow.lite.python.interpreter import Interpreter

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
                 mouse_probability: float, no_mouse_probability: float, tag_id: str = "", detected_objects: List[DetectedObject] = None):
        self.id = id
        self.block_id = block_id
        self.timestamp = timestamp
        self.original_image = original_image
        self.modified_image = modified_image
        self.mouse_probability = mouse_probability
        self.no_mouse_probability = no_mouse_probability
        self.tag_id = tag_id
        self.detected_objects = detected_objects

    def __repr__(self):
        return (f"ImageBufferElement(id={self.id}, block_id={self.block_id}, timestamp={self.timestamp}, mouse_probability={self.mouse_probability}, "
                f"no_mouse_probability={self.no_mouse_probability}, tag_id={self.tag_id}, detected_objects={self.detected_objects})")

class ImageBuffer:
    def __init__(self):
        """Initialize an empty buffer."""
        self._buffer: List[ImageBufferElement] = []
        self._next_id = 0

    def append(self, timestamp: float, original_image: bytes | None, modified_image: bytes | None, 
               mouse_probability: float, no_mouse_probability: float, detected_objects: List[DetectedObject] = None):
        """
        Append a new element to the buffer.
        """
        element = ImageBufferElement(self._next_id, 0, timestamp, original_image, modified_image, mouse_probability, no_mouse_probability, detected_objects=detected_objects)
        self._buffer.append(element)
        logging.info(f"[IMAGEBUFFER] Appended new element with ID {self._next_id}, timestamp: {timestamp}, Mouse probability: {mouse_probability}, No mouse probability: {no_mouse_probability} to the buffer.")
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
                         max_no_mouse_probability=100.0) -> List[int]:
        """
        Return the IDs of elements that match the given filter criteria.

        Args:
            min_timestamp (float): The minimum timestamp.
            max_timestamp (float): The maximum timestamp.
            min_mouse_probability (float): The minimum mouse probability.
            max_mouse_probability (float): The maximum mouse probability.
            min_no_mouse_probability (float): The minimum no mouse probability.
            max_no_mouse_probability (float): The maximum no mouse probability.

        Returns:
            List[int]: A list of IDs that match the filter criteria.
        """
        return [element.id for element in self._buffer if 
                (min_timestamp <= element.timestamp <= max_timestamp) and 
                (min_mouse_probability <= element.mouse_probability <= max_mouse_probability) and 
                (min_no_mouse_probability <= element.no_mouse_probability <= max_no_mouse_probability)]
    
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

# Initialize the image buffer
image_buffer = ImageBuffer()

# Declare videostream as a global variable
videostream = None

class TfLite:
    def __init__(self, modeldir="./tflite/",
                 graph="cv-lite-model.tflite",
                 labelfile="labels.txt",
                 resolution="800x600",
                 framerate=10,
                 jpeg_quality=75,
                 simulate_kittyflap=False):
        self.modeldir = modeldir
        self.graph = graph
        self.labelfile = labelfile
        self.resolution = resolution
        self.framerate = framerate
        self.jpeg_quality = jpeg_quality
        self.paused = False
        self.last_log_time = 0
        self.simulate_kittyflap = simulate_kittyflap

    def run(self):
        """Run the TFLite model on the video stream."""

        global videostream

        resW, resH = self.resolution.split('x')
        imW, imH = int(resW), int(resH)

        # Register task in the sigterm_monitor object
        sigterm_monitor.register_task()

        # Check if we are running in simulation mode
        if self.simulate_kittyflap:
            logging.info("[CAMERA] Running in simulation mode. No camera stream available.")
            while not sigterm_monitor.stop_now:
                if not self.paused:
                    save_image = np.random.rand() < 0.05  # set save_image randomly to True with a 5% chance
                    if save_image:
                        # Simulate a image
                        timestamp = tm.time()
                        frame = np.zeros((imH, imW, 3), dtype=np.uint8)
                        mouse_probability = np.random.uniform(CONFIG['MIN_THRESHOLD']/100, 1.0)
                        no_mouse_probability = np.random.uniform(CONFIG['MIN_THRESHOLD']/100, 1.0)
                        image_buffer.append(timestamp, self.encode_jpg_image(frame), None, mouse_probability, no_mouse_probability)
                tm.sleep(0.1)
            return

        # Path to .tflite file, which contains the model that is used for object detection
        PATH_TO_TFLITE = os.path.join(self.modeldir, self.graph)

        # Path to label map file
        PATH_TO_LABELS = os.path.join(self.modeldir, self.labelfile)

        # Load the label map
        with open(PATH_TO_LABELS, 'r') as f:
            labels = [line.strip() for line in f.readlines()]

        logging.info(f"[CAMERA] Labels loaded: {labels}")
        logging.info(f"[CAMERA] Preparing to run TFLite model {PATH_TO_TFLITE} on video stream with resolution {imW}x{imH} @ {self.framerate}fps and quality {self.jpeg_quality}%")

        # Load the Tensorflow Lite model.
        interpreter = Interpreter(model_path=PATH_TO_TFLITE, num_threads=multiprocessing.cpu_count())
        interpreter.allocate_tensors()

        # Get model details
        input_details = interpreter.get_input_details()
        output_details = interpreter.get_output_details()
        height = input_details[0]['shape'][1]
        width = input_details[0]['shape'][2]

        floating_model = (input_details[0]['dtype'] == np.float32)
        logging.info(f"[CAMERA] Floating model: {floating_model}")

        input_mean = 127.5
        input_std = 127.5

        # Check output layer name to determine if this model was created with TF2 or TF1,
        # because outputs are ordered differently for TF2 and TF1 models
        outname = output_details[0]['name']

        if ('StatefulPartitionedCall' in outname): # This is a TF2 model
            boxes_idx, classes_idx, scores_idx = 1, 3, 0
        elif ('detected_scores:0' in outname):
            boxes_idx, classes_idx, scores_idx = 1, 2, 0
        else: # This is a TF1 model
            boxes_idx, classes_idx, scores_idx = 0, 1, 2

        # Initialize frame rate calculation
        frame_rate_calc = 1
        freq = cv2.getTickFrequency()

        # Initialize video stream
        videostream = VideoStream(resolution=(imW, imH), framerate=self.framerate, jpeg_quality=self.jpeg_quality).start()
        logging.info(f"[CAMERA] Starting video stream...")

        # Wait for the camera to warm up
        detected_objects = []
        frame = None
        stream_start_time = tm.time()
        while frame is None:
            frame = videostream.read_oldest()
            if tm.time() - stream_start_time > 10:
                logging.error("[CAMERA] Camera stream failed to start within 10 seconds!")
                break
            else:
                tm.sleep(0.1)

        if frame is not None:
            logging.info("[CAMERA] Camera stream started successfully.")

        while not sigterm_monitor.stop_now:
            # Start timer (for calculating frame rate)
            t1 = cv2.getTickCount()

            if not self.paused:
                # Grab frame from video stream
                frame = videostream.read_oldest()

                if frame is not None:
                    # Run the CPU intensive TFLite only if paused is not set
                    timestamp = tm.time()
                    _timestamp, mouse_probability, no_mouse_probability, detected_objects = self._process_frame(
                        frame, width, height, input_mean, input_std, floating_model, interpreter,
                        input_details, output_details, boxes_idx, classes_idx, scores_idx, labels
                    )
                    
                    image_buffer.append(timestamp, self.encode_jpg_image(frame), None, 
                                        mouse_probability, no_mouse_probability, detected_objects=detected_objects)

                    # Calculate framerate
                    t2 = cv2.getTickCount()
                    time1 = (t2 - t1) / freq
                    frame_rate_calc = 1 / time1
                    logging.info(f"[CAMERA] Model processing time: {time1:.2f} sec, Frame Rate: {frame_rate_calc:.2f} fps")

                else:
                    # Log warning only once every 10 seconds to avoid flooding the log
                    current_time = tm.time()
                    if current_time - self.last_log_time > 10:
                        logging.warning("[CAMERA] No frame received!")
                        self.last_log_time = current_time
            
            # To avoid intensive CPU load, wait here until we reached the desired framerate
            elapsed_time = (cv2.getTickCount() - t1) / freq
            sleep_time = max(0, (1.0 / self.framerate) - elapsed_time)
            tm.sleep(sleep_time)
            
        # Stop the video stream
        videostream.stop()

        sigterm_monitor.signal_task_done()

    def pause(self):
        logging.info("[CAMERA] Pausing the TFLite processing.")
        self.paused = True

    def resume(self):
        logging.info("[CAMERA] Resuming the TFLite processing.")
        self.paused = False

    def get_run_state(self):
        return not self.paused

    def _process_frame(self, 
                       frame: np.ndarray, 
                       width: int, 
                       height: int, 
                       input_mean: float, 
                       input_std: float, 
                       floating_model: bool, 
                       interpreter: Interpreter, 
                       input_details: list, 
                       output_details: list, 
                       boxes_idx: int, 
                       classes_idx: int, 
                       scores_idx: int, 
                       labels: list
    ) -> tuple:
        """
        Process a single frame for object detection using TensorFlow Lite.
        This method handles the preprocessing of the frame, performs inference using the TensorFlow Lite
        interpreter, and processes the detection results.
        Args:
            frame (np.ndarray): Input frame in BGR format
            width (int): Target width for model input
            height (int): Target height for model input
            input_mean (float): Mean value for input normalization
            input_std (float): Standard deviation for input normalization
            floating_model (bool): Whether the model uses floating point numbers
            interpreter (tensorflow.lite.python.interpreter.Interpreter): TFLite interpreter
            input_details (list): Model input details
            output_details (list): Model output details
            boxes_idx (int): Index for bounding boxes in output
            classes_idx (int): Index for classes in output
            scores_idx (int): Index for scores in output
            labels (list): List of label names
        Returns:
            tuple: Contains:
                - timestamp (float): Processing timestamp
                - mouse_probability (float): Probability of mouse detection (0-100)
                - no_mouse_probability (float): Probability of no mouse present (0-100)
                - detected_objects (list): List of DetectedObject instances containing detection results
        """
        
        #frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        #h, w, __ = frame_rgb.shape
        #scale = max(width / w, height / h)
        #resized_w = int(w * scale)
        #resized_h = int(h * scale)
        #frame_resized = cv2.resize(frame_rgb, (resized_w, resized_h))

        #start_x = (resized_w - width) // 2
        #start_y = (resized_h - height) // 2
        #frame_cropped = frame_resized[start_y:start_y + height, start_x:start_x + width]
        #input_data = np.expand_dims(frame_cropped, axis=0)

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        frame_resized = cv2.resize(frame_rgb, (width, height))
        input_data = np.expand_dims(frame_resized, axis=0)

        original_h, original_w, __ = frame.shape

        timestamp = tm.time()

        if floating_model:
            input_data = (np.float32(input_data) - input_mean) / input_std

        interpreter.set_tensor(input_details[0]['index'], input_data)
        interpreter.invoke()

        boxes = interpreter.get_tensor(output_details[boxes_idx]['index'])[0]
        classes = interpreter.get_tensor(output_details[classes_idx]['index'])[0]
        scores = interpreter.get_tensor(output_details[scores_idx]['index'])[0]

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

            object_name = str(labels[int(classes[i])])
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


        return timestamp, mouse_probability, no_mouse_probability, detected_objects

    def get_camera_frame(self):
        if videostream is not None:
            return videostream.read()
        else:
            logging.error("[CAMERA] 'Get Frame' failed. Video stream is not yet initialized.")
            return None
        
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
