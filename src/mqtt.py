from paho.mqtt import client as mqtt
import json
import logging
import threading
import time as tm
import base64
import cv2
import numpy as np
from src.baseconfig import CONFIG
from src.helper import get_git_version

class MQTTConfig:
    device_id = CONFIG['MQTT_DEVICE_ID']
    topics = {
        "event_type": f"kittyhack/{device_id}/events",
        "inside_lock_state": f"kittyhack/{device_id}/locks/inside",
        "outside_lock_state": f"kittyhack/{device_id}/locks/outside",
        "motion_outside_state": f"kittyhack/{device_id}/motion/outside",
        "motion_inside_state": f"kittyhack/{device_id}/motion/inside",
        "manual_override": f"kittyhack/{device_id}/manual/override", # toggle_inside
        "prey_detected": f"kittyhack/{device_id}/prey/detected",
        "camera_image": f"kittyhack/{device_id}/camera/image"
    }

class MQTTClient:
    def __init__(self, broker_address, broker_port, username=None, password=None, client_name=None):
        self.broker_address = broker_address
        self.broker_port = int(broker_port)
        self.username = username
        self.password = password
        self.client_name = client_name or MQTTConfig.device_id
        self.client = mqtt.Client(client_id=self.client_name)
        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)
        self.connected = False
        self.topic_callbacks = {}
        
        # Set up the message callback
        self.client.on_message = self.on_message

    def connect(self):
        try:
            self.client.connect(self.broker_address, self.broker_port, 60)
            self.client.loop_start()
            self.connected = True
            logging.info(f"[MQTT] Connected to broker at {self.broker_address}:{self.broker_port}")
            return True
        except Exception as e:
            logging.warning(f"[MQTT] Could not connect to broker: {e}")
            self.connected = False
            return False

    def publish(self, topic, message, retain=False):
        if not self.connected:
            self.connect()
        try:
            self.client.publish(topic, json.dumps(message), retain=retain)
        except Exception as e:
            logging.warning(f"[MQTT] Could not publish to {topic}: {e}")
            
    def subscribe(self, topic, callback=None):
        """Subscribe to a topic and optionally register a callback for it"""
        if not self.connected:
            self.connect()
            
        try:
            self.client.subscribe(topic)
            logging.info(f"[MQTT] Subscribed to {topic}")
            
            if callback:
                self.topic_callbacks[topic] = callback
                
        except Exception as e:
            logging.warning(f"[MQTT] Could not subscribe to {topic}: {e}")
    
    def on_message(self, client, userdata, message):
        """Callback for when a message is received"""
        topic = message.topic
        try:
            payload = json.loads(message.payload.decode())
            logging.info(f"[MQTT] Received message on {topic}: {payload}")
            
            # Call the registered callback for this topic if it exists
            if topic in self.topic_callbacks:
                self.topic_callbacks[topic](payload)
                
        except json.JSONDecodeError:
            payload = message.payload.decode()
            logging.info(f"[MQTT] Received non-JSON message on {topic}: {payload}")
            
            # Call the registered callback anyway
            if topic in self.topic_callbacks:
                self.topic_callbacks[topic](payload)
                
        except Exception as e:
            logging.warning(f"[MQTT] Error handling message on {topic}: {e}")

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()
        self.connected = False

class StatePublisher:
    def __init__(self, mqtt_client, inside_lock_state=None, outside_lock_state=None, 
                motion_inside_state=None, motion_outside_state=None, prey_detected_state=None):
        self.mqtt_client = mqtt_client
        self.image_publish_thread = None
        self.stop_image_thread = False

        if not self.mqtt_client.connected:
            # Wait up to 3 seconds for connection
            for __ in range(30):
                if self.mqtt_client.connected:
                    break
                tm.sleep(0.1)
        
        # Publish discovery topics first
        self.publish_discovery_topics()
        
        # Wait a moment for discovery topics to be processed
        tm.sleep(0.5)
        
        # Publish initial states based on actual values
        logging.info("[MQTT] Publishing initial states")
        if inside_lock_state is not None:
            self.publish_lock_inside(inside_lock_state)
        if outside_lock_state is not None:
            self.publish_lock_outside(outside_lock_state)
        if motion_inside_state is not None:
            self.publish_motion_inside(motion_inside_state)
        if motion_outside_state is not None:
            self.publish_motion_outside(motion_outside_state)
        if prey_detected_state is not None:
            self.publish_prey_detected(prey_detected_state)

    def publish_lock_inside(self, locked: bool):
        topic = MQTTConfig.topics["inside_lock_state"]
        state = "locked" if locked else "unlocked"
        if self.mqtt_client.connected:
            self.mqtt_client.client.publish(topic, state, retain=True)

    def publish_lock_outside(self, locked: bool):
        topic = MQTTConfig.topics["outside_lock_state"]
        state = "locked" if locked else "unlocked"
        if self.mqtt_client.connected:
            self.mqtt_client.client.publish(topic, state, retain=True)

    def publish_motion_outside(self, detected: bool):
        topic = MQTTConfig.topics["motion_outside_state"]
        state = "detected" if detected else "not_detected"
        if self.mqtt_client.connected:
            self.mqtt_client.client.publish(topic, state, retain=True)

    def publish_motion_inside(self, detected: bool):
        topic = MQTTConfig.topics["motion_inside_state"]
        state = "detected" if detected else "not_detected"
        if self.mqtt_client.connected:
            self.mqtt_client.client.publish(topic, state, retain=True)

    def publish_prey_detected(self, detected: bool):
        topic = MQTTConfig.topics["prey_detected"]
        state = "detected" if detected else "not_detected"
        if self.mqtt_client.connected:
            self.mqtt_client.client.publish(topic, state, retain=True)
        
    def register_manual_override_handler(self, callback_function):
        """Register a callback function to handle manual override commands
        
        Args:
            callback_function: Function that will be called when a manual override message 
                              is received. The function should accept one parameter (the message payload).
        """
        topic = MQTTConfig.topics["manual_override"]
        self.mqtt_client.subscribe(topic, callback_function)

    def publish_image(self, image_data, retain=False, max_size=1280):
        """Publish an image to the camera image topic
        
        Args:
            image_data (numpy.ndarray or bytes): The image data to publish
            retain (bool): Whether to retain the message
            max_size (int): Maximum size for the largest dimension of the image
        """
        try:            
            # If image_data is a numpy array (cv2 image), use it directly
            if isinstance(image_data, np.ndarray):
                img = image_data
            elif isinstance(image_data, bytes):
                # Otherwise assume it's bytes and load as cv2 image
                nparr = np.frombuffer(image_data, np.uint8)
                img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            else:
                logging.warning("[MQTT] Unsupported image format for publishing")
                return
            
            # Resize the image to reduce MQTT payload size
            height, width = img.shape[:2]
            if width > max_size or height > max_size:
                ratio = max(width, height) / max_size
                new_width = int(width / ratio)
                new_height = int(height / ratio)
                img = cv2.resize(img, (new_width, new_height), interpolation=cv2.INTER_AREA)
            
            # Convert to JPEG and base64 encode
            success, buffer = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if not success:
                logging.warning("[MQTT] Failed to encode image")
                return
                
            img_str = base64.b64encode(buffer).decode()
            
            # Publish the image
            topic = MQTTConfig.topics["camera_image"]
            self.mqtt_client.client.publish(topic, img_str, retain)
            
        except Exception as e:
            logging.warning(f"[MQTT] Could not publish image: {e}")
    
    def start_periodic_image_publishing(self, interval=5.0):
        """Start a thread that periodically publishes the latest camera image
        
        Args:
            interval (float): The interval in seconds between image publications
        """
        from src.model import ModelHandler  # Import here to avoid circular imports
        
        if self.image_publish_thread and self.image_publish_thread.is_alive():
            logging.warning("[MQTT] Periodic image publishing thread already running")
            return
            
        self.stop_image_thread = False
        
        def publish_image_periodically():
            while not self.stop_image_thread:
                try:
                    # Get the model handler instance from the backend
                    from src.backend import model_handler
                    
                    # Check if model_handler exists and has initialized the videostream
                    if model_handler and model_handler.check_videostream_status():
                        # Get frame directly through model_handler
                        frame = model_handler.get_camera_frame()
                        if frame is not None:
                            self.publish_image(frame, retain=True)
                        else:
                            logging.debug("[MQTT] No frame available from model_handler")
                    else:
                        logging.debug("[MQTT] Waiting for videostream to be initialized")
                except Exception as e:
                    logging.error(f"[MQTT] Error publishing periodic image: {e}")
                    
                # Sleep for the specified interval
                for _ in range(int(interval * 10)):  # Check for stop flag every 0.1 seconds
                    if self.stop_image_thread:
                        break
                    tm.sleep(0.1)
        
        self.image_publish_thread = threading.Thread(target=publish_image_periodically, daemon=True)
        self.image_publish_thread.start()
        logging.info(f"[MQTT] Started periodic image publishing thread (interval: {interval}s)")
    
    def stop_periodic_image_publishing(self):
        """Stop the periodic image publishing thread"""
        self.stop_image_thread = True
        if self.image_publish_thread and self.image_publish_thread.is_alive():
            self.image_publish_thread.join(timeout=1.0)
            logging.info("[MQTT] Stopped periodic image publishing thread")

    def publish_discovery_topics(self):
        """Publish Home Assistant MQTT discovery topics for auto-configuration"""
        device_id = CONFIG['MQTT_DEVICE_ID']
        discovery_prefix = "homeassistant"
        
        # Basic device info used in all configs
        device_info = {
            "identifiers": [device_id],
            "name": f"KittyHack Cat Flap '{device_id}'",
            "model": "KittyHack",
            "manufacturer": "KittyHack",
            "sw_version": get_git_version()
        }
        
        # Define all entities to create
        entities = {
            # Binary sensors
            "binary_sensor": [
                {
                    "name": "Motion Outside",
                    "unique_id": f"{device_id}_motion_outside",
                    "state_topic": f"kittyhack/{device_id}/motion/outside",
                    "payload_on": "detected",
                    "payload_off": "not_detected",
                    "device_class": "motion"
                },
                {
                    "name": "Motion Inside",
                    "unique_id": f"{device_id}_motion_inside",
                    "state_topic": f"kittyhack/{device_id}/motion/inside",
                    "payload_on": "detected",
                    "payload_off": "not_detected",
                    "device_class": "motion"
                },
                {
                    "name": "Prey Detected",
                    "unique_id": f"{device_id}_prey_detected",
                    "state_topic": f"kittyhack/{device_id}/prey/detected",
                    "payload_on": "detected",
                    "payload_off": "not_detected",
                    "device_class": "motion",
                    "icon": "mdi:rodent"
                },
                {
                    "name": "Outside Lock",
                    "unique_id": f"{device_id}_outside_lock",
                    "state_topic": f"kittyhack/{device_id}/locks/outside",
                    "payload_on": "unlocked",
                    "payload_off": "locked",
                    "device_class": "lock"
                }
            ],
            # Locks
            "lock": [
                {
                    "name": "Inside Lock",
                    "unique_id": f"{device_id}_inside_lock",
                    "state_topic": f"kittyhack/{device_id}/locks/inside",
                    "command_topic": f"kittyhack/{device_id}/manual/override",
                    "payload_lock": "toggle_inside",
                    "payload_unlock": "toggle_inside",
                    "state_locked": "locked",
                    "state_unlocked": "unlocked"
                }
            ],
            # Camera
            "camera": [
                {
                    "name": "Camera",
                    "unique_id": f"{device_id}_camera",
                    "topic": f"kittyhack/{device_id}/camera/image",
                    "image_encoding": "b64"
                }
            ]
        }
        
        # Publish discovery messages
        for component, configs in entities.items():
            for config in configs:
                # Add device info to each config
                config["device"] = device_info
                
                # Create discovery topic
                object_id = config["unique_id"]
                discovery_topic = f"{discovery_prefix}/{component}/{device_id}/{object_id}/config"
                
                # Publish with retain flag for persistence
                self.mqtt_client.publish(discovery_topic, config, retain=True)
                logging.info(f"[MQTT] Published discovery topic: {discovery_topic}")