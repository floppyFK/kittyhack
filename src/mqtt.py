from paho.mqtt import client as mqtt
import json
import logging
import threading
import time as tm
import base64
import cv2
import numpy as np
from src.baseconfig import CONFIG, AllowedToEnter, AllowedToExit, update_single_config_parameter
from src.helper import get_git_version, EventType

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
        "camera_image": f"kittyhack/{device_id}/camera/image",
        "allowed_to_exit": f"kittyhack/{device_id}/config/allowed_to_exit",
        "allowed_to_exit_set": f"kittyhack/{device_id}/config/allowed_to_exit/set",
        "allowed_to_enter": f"kittyhack/{device_id}/config/allowed_to_enter",
        "allowed_to_enter_set": f"kittyhack/{device_id}/config/allowed_to_enter/set"
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
            
        # Set the Last Will and Testament BEFORE connecting
        status_topic = f"kittyhack/{MQTTConfig.device_id}/status"
        self.client.will_set(status_topic, "offline", retain=True)
        
        self.connected = False
        self.topic_callbacks = {}
        
        # Set up the message callback
        self.client.on_message = self.on_message

    def connect(self):
        try:
            self.client.connect(self.broker_address, self.broker_port, 60)
            self.client.loop_start()
            self.connected = True
            
            # Publish online status AFTER connecting
            status_topic = f"kittyhack/{MQTTConfig.device_id}/status"
            self.client.publish(status_topic, "online", retain=True)
            
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
        """Disconnect from the MQTT broker, publishing offline status first"""
        try:
            # First publish offline status
            if self.connected:
                status_topic = f"kittyhack/{MQTTConfig.device_id}/status"
                self.client.publish(status_topic, "offline", retain=True)
                logging.info("[MQTT] Published offline status before disconnecting")
                
            # Then disconnect properly
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False
            logging.info("[MQTT] Disconnected from MQTT broker")
        except Exception as e:
            logging.error(f"[MQTT] Error during graceful disconnect: {e}")

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
        
        # Publish initial config states
        self.publish_allowed_to_exit(CONFIG['ALLOWED_TO_EXIT'])
        self.publish_allowed_to_enter(CONFIG['ALLOWED_TO_ENTER'])
        
        # Subscribe to config set topics
        self.register_config_handlers()

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

    def publish_allowed_to_exit(self, allowed: AllowedToExit):
        """Publish the current state of ALLOWED_TO_EXIT config parameter (enum)"""
        topic = MQTTConfig.topics["allowed_to_exit"]
        translations = {
            AllowedToExit.ALLOW: {"en": "Allow exit", "de": "Ausgang erlauben"},
            AllowedToExit.DENY: {"en": "Do not allow exit", "de": "Ausgang verbieten"},
            AllowedToExit.CONFIGURE_PER_CAT: {"en": "Per-cat configuration", "de": "Separate Konfiguration pro Katze"}
        }
        friendly = translations.get(allowed, {}).get(CONFIG['LANGUAGE'], allowed.value)
        if self.mqtt_client.connected:
            self.mqtt_client.client.publish(topic, friendly, retain=True)
            logging.info(f"[MQTT] Published ALLOWED_TO_EXIT: {friendly} (raw: {allowed.value})")
    
    def register_config_handlers(self):
        """Register handlers for configuration topics"""
        # Subscribe to config set topics
        self.mqtt_client.subscribe(MQTTConfig.topics["allowed_to_exit_set"], self.handle_allowed_to_exit_change)
        self.mqtt_client.subscribe(MQTTConfig.topics["allowed_to_enter_set"], self.handle_allowed_to_enter_change)
    
    def handle_allowed_to_exit_change(self, payload):
        """Handle changes to the ALLOWED_TO_EXIT configuration parameter"""
        try:
            # Accept dict or str payload
            raw = payload.get('state') if isinstance(payload, dict) else payload
            raw = "" if raw is None else str(raw)
            logging.info(f"[MQTT] Received ALLOWED_TO_EXIT change request: {raw}")

            mapping = {
                'ON': AllowedToExit.ALLOW, 'OFF': AllowedToExit.DENY,
                'Allow exit': AllowedToExit.ALLOW, 'Do not allow exit': AllowedToExit.DENY, 'Per-cat configuration': AllowedToExit.CONFIGURE_PER_CAT,
                'Ausgang erlauben': AllowedToExit.ALLOW, 'Ausgang verbieten': AllowedToExit.DENY, 'Separate Konfiguration pro Katze': AllowedToExit.CONFIGURE_PER_CAT,
                'allow': AllowedToExit.ALLOW, 'deny': AllowedToExit.DENY, 'configure_per_cat': AllowedToExit.CONFIGURE_PER_CAT
            }
            new_value = mapping.get(raw, None)
            if new_value is None:
                low = raw.lower()
                if low in ["true", "1", "on"]:
                    new_value = AllowedToExit.ALLOW
                elif low in ["false", "0", "off"]:
                    new_value = AllowedToExit.DENY
                else:
                    new_value = AllowedToExit.CONFIGURE_PER_CAT

            if new_value != CONFIG['ALLOWED_TO_EXIT']:
                CONFIG['ALLOWED_TO_EXIT'] = new_value
                update_single_config_parameter('ALLOWED_TO_EXIT')
                self.publish_allowed_to_exit(new_value)
                logging.info(f"[MQTT] Updated ALLOWED_TO_EXIT to: {new_value.value}")
        except Exception as e:
            logging.error(f"[MQTT] Error handling ALLOWED_TO_EXIT change: {e}")
    
    def handle_allowed_to_enter_change(self, payload):
        try:
            # Get the received value
            if isinstance(payload, dict):
                new_value = payload.get('state', '')
            elif isinstance(payload, str):
                new_value = payload
            else:
                logging.warning(f"[MQTT] Invalid payload type for ALLOWED_TO_ENTER: {type(payload)}")
                return
                
            logging.info(f"[MQTT] Received ALLOWED_TO_ENTER change request: {new_value}")
            
            # Map from friendly names back to raw values
            translations = {
                # English translations
                "All cats": "all",
                "All cats with RFID": "all_rfids",
                "Only registered cats": "known",
                "No cats": "none",
                "Separate configuration per cat": "configure_per_cat",
                
                # German translations
                "Alle Katzen": "all",
                "Alle Katzen mit RFID-Chip": "all_rfids",
                "Nur registrierte Katzen": "known",
                "Keine Katzen": "none",
                "Separate Konfiguration pro Katze": "configure_per_cat",
                
                # Raw values for backward compatibility
                "all": "all",
                "all_rfids": "all_rfids",
                "known": "known", 
                "none": "none",
                "configure_per_cat": "configure_per_cat"
            }
            
            # Convert from friendly name to raw value if needed
            raw_value = translations.get(new_value, new_value)
            
            # The rest of the function remains the same
            try:
                new_enum_value = AllowedToEnter(raw_value)
                if new_enum_value != CONFIG['ALLOWED_TO_ENTER']:
                    CONFIG['ALLOWED_TO_ENTER'] = new_enum_value
                    update_single_config_parameter('ALLOWED_TO_ENTER')
                    self.publish_allowed_to_enter(new_enum_value)
                    logging.info(f"[MQTT] Updated ALLOWED_TO_ENTER to: {raw_value}")
            except ValueError:
                logging.error(f"[MQTT] Invalid ALLOWED_TO_ENTER value: {raw_value}")
                logging.error(f"[MQTT] Valid values are: {[e.value for e in AllowedToEnter]}")
        except Exception as e:
            logging.error(f"[MQTT] Error handling ALLOWED_TO_ENTER change: {e}")

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
    
    def start_periodic_image_publishing(self, interval=None):
        """Start a thread that periodically publishes the latest camera image
        
        Args:
            interval (float, optional): The interval in seconds between image publications. 
                                    If None, use the value from CONFIG.
        """
        from src.model import ModelHandler  # Import here to avoid circular imports
        
        if interval is None:
            interval = CONFIG['MQTT_IMAGE_PUBLISH_INTERVAL']
        
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

    def cleanup_old_discovery_topics(self):
        """Publish empty retained payloads to remove deprecated HA discovery entries"""
        try:
            device_id = CONFIG['MQTT_DEVICE_ID']
            discovery_prefix = "homeassistant"

            # Old entity: switch for allow_exit (now replaced by select)
            old_topics = [
                f"{discovery_prefix}/switch/{device_id}/{device_id}_allow_exit/config"
            ]

            for t in old_topics:
                # Empty retained payload removes the entity from Home Assistant
                self.mqtt_client.client.publish(t, "", retain=True)
                logging.info(f"[MQTT] Cleaned old discovery topic: {t}")
        except Exception as e:
            logging.warning(f"[MQTT] Could not clean old discovery topics: {e}")

    def publish_discovery_topics(self):
        """Publish Home Assistant MQTT discovery topics for auto-configuration"""
        # First, clean up deprecated discovery topics (e.g., old switch for allow_exit)
        self.cleanup_old_discovery_topics()

        device_id = CONFIG['MQTT_DEVICE_ID']
        discovery_prefix = "homeassistant"
        
        # Basic device info used in all configs
        device_info = {
            "identifiers": [device_id],
            "name": f"{device_id}",
            "model": "KittyHack",
            "manufacturer": "FloppyFK",
            "sw_version": get_git_version()
        }
        
        # Availability configuration to add to all entities
        availability_config = {
            "availability_topic": f"kittyhack/{device_id}/status",
            "payload_available": "online",
            "payload_not_available": "offline"
        }

        # Language-dependent labels
        is_de = CONFIG['LANGUAGE'] == "de"
        allowed_exit_options = (
            [
                "Ausgang erlauben",
                "Ausgang verbieten",
                "Separate Konfiguration pro Katze"
            ]
            if is_de
            else [
                "Allow exit",
                "Do not allow exit",
                "Per-cat configuration"
            ]
        )
        allowed_enter_options = (
            [
                "Alle Katzen",
                "Alle Katzen mit RFID-Chip",
                "Nur registrierte Katzen",
                "Keine Katzen",
                "Separate Konfiguration pro Katze",
            ]
            if is_de
            else [
                "All cats",
                "All cats with RFID",
                "Only registered cats",
                "No cats",
                "Separate configuration per cat",
            ]
        )
        
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
            ],
            # Selects for ALLOWED_TO_EXIT and ALLOWED_TO_ENTER
            "select": [
                {
                    "name": "Allow Exit",
                    "unique_id": f"{device_id}_allow_exit",
                    "state_topic": f"kittyhack/{device_id}/config/allowed_to_exit",
                    "command_topic": f"kittyhack/{device_id}/config/allowed_to_exit/set",
                    "options": allowed_exit_options,
                    "icon": "mdi:arrow-up-bold-circle"
                },
                {
                    "name": "Allow Enter",
                    "unique_id": f"{device_id}_allow_enter",
                    "state_topic": f"kittyhack/{device_id}/config/allowed_to_enter",
                    "command_topic": f"kittyhack/{device_id}/config/allowed_to_enter/set",
                    "options": allowed_enter_options,
                    "icon": "mdi:arrow-down-bold-circle"
                }
            ],
            "sensor": [
                {
                    "name": "Last Event",
                    "unique_id": f"{device_id}_events",
                    "default_entity_id": f"sensor.{device_id}_events",
                    "state_topic": f"kittyhack/{device_id}/events",
                    "value_template": "{{ value_json.event }}",
                    "json_attributes_topic": f"kittyhack/{device_id}/events",
                    "icon": "mdi:cat",
                    "force_update": True,
                    "availability_topic": f"kittyhack/{device_id}/status",
                    "payload_available": "online",
                    "payload_not_available": "offline"
                }
            ]
        }
        
        # Publish discovery messages
        for component, configs in entities.items():
            for config in configs:
                # Add device info to each config
                config["device"] = device_info
                
                # Add availability config to entities (except those that already have it)
                if "availability_topic" not in config:
                    config.update(availability_config)
                
                # Create discovery topic
                object_id = config["unique_id"]
                discovery_topic = f"{discovery_prefix}/{component}/{device_id}/{object_id}/config"
                
                # Publish with retain flag for persistence
                self.mqtt_client.publish(discovery_topic, config, retain=True)
                logging.info(f"[MQTT] Published discovery topic: {discovery_topic}")

    def publish_allowed_to_enter(self, allowed: AllowedToEnter):
        """Publish the current state of ALLOWED_TO_ENTER config parameter with friendly name"""
        topic = MQTTConfig.topics["allowed_to_enter"]
        
        # Map enum values to friendly names
        translations = {
            "all": {
                "en": "All cats",
                "de": "Alle Katzen"
            },
            "all_rfids": {
                "en": "All cats with RFID",
                "de": "Alle Katzen mit RFID-Chip"
            },
            "known": {
                "en": "Only registered cats",
                "de": "Nur registrierte Katzen"
            },
            "none": {
                "en": "No cats",
                "de": "Keine Katzen"
            },
            "configure_per_cat": {
                "en": "Separate configuration per cat",
                "de": "Separate Konfiguration pro Katze"
            }
        }
        
        # Get translated friendly name based on current language
        friendly_name = translations.get(allowed.value, {}).get(CONFIG['LANGUAGE'], allowed.value)
        
        if self.mqtt_client.connected:
            self.mqtt_client.client.publish(topic, friendly_name, retain=True)
            logging.info(f"[MQTT] Published ALLOWED_TO_ENTER: {friendly_name} (raw: {allowed.value})")

    def update_language_dependent_topics(self):
        """Update all language-dependent MQTT topics after language change"""
        logging.info("[MQTT] Updating language-dependent MQTT topics")
        
        # Republish discovery topics with new language-specific labels
        self.publish_discovery_topics()
        
        # Republish current states with translations
        self.publish_allowed_to_enter(CONFIG['ALLOWED_TO_ENTER'])
        
        logging.info("[MQTT] Language-dependent MQTT topics updated")

    def publish_event_type(self, event_type, cat_name=None):
        """
        Publish an event type to MQTT
        
        Args:
            event_type (str): The event type to publish (can contain multiple event types separated by commas)
            cat_name (str, optional): The detected cat name
        """
        from src.helper import EventType
        topic = MQTTConfig.topics["event_type"]
        
        # First publish a dummy event with retain=False to force a state change
        dummy_payload = {
            "event": "New Event",
            "raw_event_type": "new_event",
            "additional_events": [],
            "raw_additional_events": [],
            "detected_cat": cat_name if cat_name else ""
        }
        
        if self.mqtt_client.connected:
            # Publish dummy event WITHOUT retain flag
            self.mqtt_client.client.publish(topic, json.dumps(dummy_payload), retain=False)
            # Small delay to ensure events are processed in order
            tm.sleep(0.1)
        
        # Handle multiple event types separated by commas
        event_types = event_type.split(",")
        pretty_events = []
        
        for ev_type in event_types:
            ev_type = ev_type.strip()
            # Convert each event type to its pretty string representation
            pretty_events.append(EventType.to_pretty_string(ev_type))
        
        # Create the message payload for the real event
        payload = {
            "event": pretty_events[0],  # Primary event
            "raw_event_type": event_types[0],
            "additional_events": pretty_events[1:] if len(pretty_events) > 1 else [],
            "raw_additional_events": event_types[1:] if len(event_types) > 1 else [],
            "detected_cat": cat_name if cat_name else ""
        }
        
        if self.mqtt_client.connected:
            # Now publish the actual event with retain=True
            self.mqtt_client.client.publish(topic, json.dumps(payload), retain=True)
            logging.info(f"[MQTT] Published event type: {pretty_events}")