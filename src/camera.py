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
from typing import List, Optional
from src.helper import CONFIG, sigterm_monitor

class VideoStream:
    """Camera object that controls video streaming from the Picamera"""
    def __init__(self, resolution=(640, 480), framerate=15, jpeg_quality=75, tuning_file="/usr/share/libcamera/ipa/rpi/vc4/ov5647_noir.json"):
        self.resolution = resolution
        self.framerate = framerate
        self.jpeg_quality = jpeg_quality
        self.tuning_file = tuning_file  # Path to the tuning file
        self.stopped = False
        self.frame = None
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
                        self.frame = frame
                else:
                    logging.error("[CAMERA] Failed to decode frame")

        self.process.terminate()

    def read(self):
        # Return the most recent frame
        with self.lock:
            return self.frame

    def stop(self):
        # Stop the video stream
        self.stopped = True
        if self.process:
            self.process.terminate()
            logging.info("[CAMERA] Video stream stopped.")
        else:
            logging.error("[CAMERA] Video stream not yet started. Nothing to stop.")

class ImageBufferElement:
    def __init__(self, id: int, block_id: int, timestamp: float, original_image: bytes, modified_image: bytes, 
                 mouse_probability: float, no_mouse_probability: float, tag_id: str = ""):
        self.id = id
        self.block_id = block_id
        self.timestamp = timestamp
        self.original_image = original_image
        self.modified_image = modified_image
        self.mouse_probability = mouse_probability
        self.no_mouse_probability = no_mouse_probability
        self.tag_id = tag_id

    def __repr__(self):
        return (f"ImageBufferElement(id={self.id}, block_id={self.block_id}, timestamp={self.timestamp}, mouse_probability={self.mouse_probability}, "
                f"no_mouse_probability={self.no_mouse_probability}, tag_id={self.tag_id})")

class ImageBuffer:
    def __init__(self):
        """Initialize an empty buffer."""
        self._buffer: List[ImageBufferElement] = []
        self._next_id = 0

    def append(self, timestamp: float, original_image: bytes, modified_image: bytes, 
               mouse_probability: float, no_mouse_probability: float):
        """
        Append a new element to the buffer.
        """
        element = ImageBufferElement(self._next_id, 0, timestamp, original_image, modified_image, mouse_probability, no_mouse_probability)
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
                        frame_with_overlay = frame.copy()
                        mouse_probability = np.random.uniform(CONFIG['MIN_THRESHOLD']/100, 1.0)
                        no_mouse_probability = np.random.uniform(CONFIG['MIN_THRESHOLD']/100, 1.0)
                        image_buffer.append(timestamp, self.encode_jpg_image(frame), self.encode_jpg_image(frame_with_overlay), mouse_probability, no_mouse_probability)
                tm.sleep(0.1)
            return

        # Import TensorFlow libraries
        # If tflite_runtime is installed, import interpreter from tflite_runtime, else import from regular tensorflow
        pkg = importlib.util.find_spec('tflite_runtime')
        if pkg:
            from tflite_runtime.interpreter import Interpreter
        else:
            from tensorflow.lite.python.interpreter import Interpreter

        # Path to .tflite file, which contains the model that is used for object detection
        PATH_TO_CKPT = os.path.join(self.modeldir, self.graph)

        # Path to label map file
        PATH_TO_LABELS = os.path.join(self.modeldir, self.labelfile)

        # Load the label map
        with open(PATH_TO_LABELS, 'r') as f:
            labels = [line.strip() for line in f.readlines()]

        logging.info(f"[CAMERA] Labels loaded: {labels}")
        logging.info(f"[CAMERA] Preparing to run TFLite model {PATH_TO_CKPT} on video stream with resolution {imW}x{imH} @ {self.framerate}fps and quality {self.jpeg_quality}%")

        # Load the Tensorflow Lite model.
        interpreter = Interpreter(model_path=PATH_TO_CKPT)
        interpreter.allocate_tensors()

        # Get model details
        input_details = interpreter.get_input_details()
        output_details = interpreter.get_output_details()
        height = input_details[0]['shape'][1]
        width = input_details[0]['shape'][2]

        floating_model = (input_details[0]['dtype'] == np.float32)

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
        #tm.sleep(5)

        # Wait for the camera to warm up
        frame = None
        stream_start_time = tm.time()
        while frame is None:
            frame = videostream.read()
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

            # Grab frame from video stream
            frame = videostream.read()

            # Acquire frame and resize to expected shape [1xHxWx3]
            if frame is not None:
                # Run the CPU intensive TFLite only if paused is not set
                if not self.paused:
                    frame_with_overlay = frame.copy()
                    frame_rgb = cv2.cvtColor(frame_with_overlay, cv2.COLOR_BGR2RGB)
                    frame_resized = cv2.resize(frame_rgb, (width, height))
                    input_data = np.expand_dims(frame_resized, axis=0)

                    timestamp = tm.time()

                    # Normalize pixel values if using a floating model (i.e. if model is non-quantized)
                    if floating_model:
                        input_data = (np.float32(input_data) - input_mean) / input_std

                    # Perform the actual detection by running the model with the image as input
                    interpreter.set_tensor(input_details[0]['index'], input_data)
                    interpreter.invoke()

                    # Retrieve detection results
                    boxes = interpreter.get_tensor(output_details[boxes_idx]['index'])[0] # Bounding box coordinates of detected objects
                    classes = interpreter.get_tensor(output_details[classes_idx]['index'])[0] # Class index of detected objects
                    scores = interpreter.get_tensor(output_details[scores_idx]['index'])[0] # Confidence of detected objects

                    # Ensure scores is an array
                    if np.isscalar(scores):
                        scores = np.array([scores])

                    # Ensure classes is an array
                    if np.isscalar(classes):
                        classes = np.array([classes])

                    # Ensure boxes is a 2-dimensional array
                    if boxes.ndim == 1:
                        boxes = np.expand_dims(boxes, axis=0)

                    # Loop over all detections and draw detection box if confidence is above minimum threshold
                    save_image = False
                    no_mouse_probability = 0.0
                    mouse_probability = 0.0
                    for i in range(len(scores)):
                        if ((scores[i] > (CONFIG['MIN_THRESHOLD']/100)) and (scores[i] <= 1.0)):
                            save_image = True

                            # Get bounding box coordinates and draw box
                            # Interpreter can return coordinates that are outside of image dimensions, need to force them to be within image using max() and min()
                            ymin = int(max(1, (boxes[i][0] * imH)))
                            xmin = int(max(1, (boxes[i][1] * imW)))
                            ymax = int(min(imH, (boxes[i][2] * imH)))
                            xmax = int(min(imW, (boxes[i][3] * imW)))

                            cv2.rectangle(frame_with_overlay, (xmin, ymin), (xmax, ymax), (10, 255, 0), 2)

                            # Draw label
                            object_name = labels[int(classes[i])] # Look up object name from "labels" array using class index
                            probability = int(scores[i] * 100)
                            label = '%s: %d%%' % (object_name, probability) # Example: 'person: 72%'
                            labelSize, baseLine = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2) # Get font size
                            label_ymin = max(ymin, labelSize[1] + 10) # Make sure not to draw label too close to top of window
                            cv2.rectangle(frame_with_overlay, (xmin, label_ymin - labelSize[1] - 10), (xmin + labelSize[0], label_ymin + baseLine - 10), (255, 255, 255), cv2.FILLED) # Draw white box to put label text in
                            cv2.putText(frame_with_overlay, label, (xmin, label_ymin - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2) # Draw label text

                            if object_name == "Maus":
                                mouse_probability = probability
                            elif object_name == "Keine Maus":
                                no_mouse_probability = probability

                    if save_image:
                        image_buffer.append(timestamp, self.encode_jpg_image(frame), self.encode_jpg_image(frame_with_overlay), mouse_probability, no_mouse_probability)

                # To avoid intensive CPU load, wait here until we reached the desired framerate
                elapsed_time = (cv2.getTickCount() - t1) / freq
                sleep_time = max(0, (1.0 / self.framerate) - elapsed_time)
                tm.sleep(sleep_time)

                # Calculate framerate
                t2 = cv2.getTickCount()
                time1 = (t2 - t1) / freq
                frame_rate_calc = 1 / time1
                #logging.debug(f"[CAMERA] Frame rate: {frame_rate_calc:.2f}")

            else:
                # Log warning only once every 10 seconds to avoid flooding the log
                current_time = tm.time()
                if current_time - self.last_log_time > 10:
                    logging.warning("[CAMERA] No frame received!")
                    self.last_log_time = current_time
            
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
