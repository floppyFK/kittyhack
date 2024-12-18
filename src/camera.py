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

class VideoStream:
    """Camera object that controls video streaming from the Picamera"""
    def __init__(self, resolution=(640, 480), framerate=15, jpeg_quality=75):
        self.resolution = resolution
        self.framerate = framerate
        self.jpeg_quality = jpeg_quality
        self.stopped = False
        self.frame = None
        self.process = None
        self.lock = threading.Lock()

    def start(self):
        # Start the thread that reads frames from the video stream
        threading.Thread(target=self.update, args=(), daemon=True).start()
        return self

    def update(self):
        command = f"/usr/bin/libcamera-vid -t 0 --inline --width {self.resolution[0]} --height {self.resolution[1]} --framerate {self.framerate} --codec mjpeg --quality {self.jpeg_quality} -o -"
        logging.info(f"[CAMERA] Running command: {command}")

        self.process = subprocess.Popen(shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=4096*10000)
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

class ImageBufferElement:
    def __init__(self, timestamp: float, original_image: cv2.typing.MatLike, modified_image: cv2.typing.MatLike, 
                 mouse_probability: float, no_mouse_probability: float):
        self.timestamp = timestamp
        self.original_image = original_image
        self.modified_image = modified_image
        self.mouse_probability = mouse_probability
        self.no_mouse_probability = no_mouse_probability

    def __repr__(self):
        return (f"ImageBufferElement(timestamp={self.timestamp}, mouse_probability={self.mouse_probability}, "
                f"no_mouse_probability={self.no_mouse_probability})")

class ImageBuffer:
    def __init__(self):
        """Initialize an empty buffer."""
        self._buffer: List[ImageBufferElement] = []

    def append(self, timestamp: float, original_image: cv2.typing.MatLike, modified_image: cv2.typing.MatLike, 
               mouse_probability: float, no_mouse_probability: float):
        """
        Append a new element to the buffer.

        Args:
            timestamp (float): Timestamp of the element.
            original_image (cv2.typing.MatLike): The original image.
            modified_image (cv2.typing.MatLike): The modified image.
            mouse_probability (float): Probability of mouse presence.
            no_mouse_probability (float): Probability of no mouse presence.
        """
        element = ImageBufferElement(timestamp, original_image, modified_image, mouse_probability, no_mouse_probability)
        self._buffer.append(element)

    def pop(self) -> Optional[ImageBufferElement]:
        """
        Return and remove the last element from the buffer.

        Returns:
            Optional[ImageBufferElement]: The last element if the buffer is not empty, else None.
        """
        if self._buffer:
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

# Initialize the image buffer
image_buffer = ImageBuffer()

# Declare videostream as a global variable
videostream = None

class TfLite:
    def __init__(self, modeldir="/root/AIContainer/app/",
                 graph="cv-lite-model.tflite",
                 labelfile="labels.txt",
                 threshold=0.5,
                 resolution="800x600",
                 framerate=10,
                 jpeg_quality=75,
                 simulate_kittyflap=False):
        self.modeldir = modeldir
        self.graph = graph
        self.labelfile = labelfile
        self.threshold = threshold
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

        # Check if we are running in simulation mode
        if self.simulate_kittyflap:
            logging.info("[CAMERA] Running in simulation mode. No camera stream available.")
            while True:
                if not self.paused:
                    save_image = np.random.rand() < 0.05  # set save_image randomly to True with a 5% chance
                    if save_image:
                        # Simulate a cv2.typing.MatLike image
                        timestamp = tm.time()
                        frame = np.zeros((imH, imW, 3), dtype=np.uint8)
                        frame_with_overlay = frame.copy()
                        mouse_probability = np.random.uniform(self.threshold, 1.0)
                        no_mouse_probability = np.random.uniform(self.threshold, 1.0)
                        image_buffer.append(timestamp, frame, frame_with_overlay, mouse_probability, no_mouse_probability)
                tm.sleep(0.1)

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

        while True:
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
                        if ((scores[i] > self.threshold) and (scores[i] <= 1.0)):
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
                        image_buffer.append(timestamp, frame, frame_with_overlay, mouse_probability, no_mouse_probability)

                # To avoid intensive CPU load, wait here until we reached the desired framerate
                elapsed_time = (cv2.getTickCount() - t1) / freq
                sleep_time = max(0, (1.0 / self.framerate) - elapsed_time)
                tm.sleep(sleep_time)

                # Calculate framerate
                t2 = cv2.getTickCount()
                time1 = (t2 - t1) / freq
                frame_rate_calc = 1 / time1
                logging.debug(f"[CAMERA] Frame rate: {frame_rate_calc:.2f}")

            else:
                # Log warning only once every 10 seconds to avoid flooding the log
                current_time = tm.time()
                if current_time - self.last_log_time > 10:
                    logging.warning("[CAMERA] No frame received!")
                    self.last_log_time = current_time

    def pause(self):
        logging.info("[CAMERA] Pausing the TFLite processing.")
        self.paused = True

    def resume(self):
        logging.info("[CAMERA] Resuming the TFLite processing.")
        self.paused = False

    def get_run_state(self):
        return not self.paused

    def stop(self):
        logging.info("[CAMERA] Stopping the video stream.")
        # Stop the video stream
        VideoStream.stop()

    def get_camera_frame(self):
        if videostream is not None:
            return videostream.read()
        else:
            logging.error("[CAMERA] 'Get Frame' failed. Video stream is not yet initialized.")
            return None
