"""MQTT client, topic map, and Home Assistant–oriented state publishing."""

from paho.mqtt import client as mqtt
import json
import logging
import threading
import time as tm
import base64
import cv2
import numpy as np
from src.baseconfig import CONFIG, AllowedToEnter, AllowedToExit, update_single_config_parameter
from src.helper import (
    Versioning,
    EventType,
)

class MQTTConfig:
    """Device id and topic path constants for Kittyhack MQTT."""

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
    """Thin paho-mqtt wrapper with LWT online/offline status and reconnect recovery."""

    def __init__(self, broker_address, broker_port, username=None, password=None, client_name=None):
        self.broker_address = broker_address
        self.broker_port = int(broker_port)
        self.username = username
        self.password = password
        self.client_name = client_name or MQTTConfig.device_id
        # VERSION1 keeps on_connect/on_disconnect signatures simple (rc: int).
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=self.client_name,
        )
        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)

        # Bound reconnect backoff so prolonged broker outages keep retrying.
        self.client.reconnect_delay_set(min_delay=1, max_delay=120)

        # Set the Last Will and Testament BEFORE connecting
        status_topic = f"kittyhack/{MQTTConfig.device_id}/status"
        self.client.will_set(status_topic, "offline", retain=True)

        self.connected = False
        self.topic_callbacks = {}
        self._reconnect_callbacks = []
        self._connect_count = 0
        self._intentional_disconnect = False

        self.client.on_message = self.on_message
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def register_reconnect_callback(self, callback):
        """Register a callback invoked after every successful reconnect (not the first connect)."""
        if callback and callback not in self._reconnect_callbacks:
            self._reconnect_callbacks.append(callback)

    def _status_topic(self):
        return f"kittyhack/{MQTTConfig.device_id}/status"

    def _publish_online_status(self):
        """Publish retained online status (must run after CONNACK)."""
        try:
            self.client.publish(self._status_topic(), "online", retain=True)
        except Exception as e:
            logging.warning(f"[MQTT] Could not publish online status: {e}")

    def _resubscribe_all(self):
        """Re-subscribe all registered topics after (re)connect."""
        for topic in list(self.topic_callbacks.keys()):
            try:
                self.client.subscribe(topic)
                logging.info(f"[MQTT] Re-subscribed to {topic}")
            except Exception as e:
                logging.warning(f"[MQTT] Could not re-subscribe to {topic}: {e}")

    def _on_connect(self, client, userdata, flags, rc):
        """Handle CONNACK: mark online, restore subscriptions, refresh HA after reconnect."""
        if rc != 0:
            self.connected = False
            logging.error(f"[MQTT] Connection refused by broker (rc={rc})")
            return

        self.connected = True
        self._connect_count += 1
        if isinstance(flags, dict):
            session_present = bool(flags.get("session_present", False))
        elif isinstance(flags, int):
            session_present = bool(flags & 0x01)
        else:
            session_present = bool(getattr(flags, "session_present", False))
        logging.info(
            f"[MQTT] Connected to broker at {self.broker_address}:{self.broker_port} "
            f"(connect=#{self._connect_count}, session_present={session_present})"
        )

        # Always republish availability — LWT may have set offline during the outage.
        self._publish_online_status()

        # Clean sessions drop subscriptions; always restore from our registry.
        self._resubscribe_all()

        # First connect: StatePublisher still runs its initial publish path.
        # Later connects: refresh discovery + last known states for Home Assistant.
        if self._connect_count > 1:
            logging.info("[MQTT] Reconnected — refreshing Home Assistant state")
            for callback in list(self._reconnect_callbacks):
                try:
                    callback()
                except Exception as e:
                    logging.error(f"[MQTT] Reconnect callback failed: {e}")

    def _on_disconnect(self, client, userdata, rc):
        """Clear connected flag; paho's loop thread will attempt automatic reconnect."""
        self.connected = False
        if self._intentional_disconnect:
            logging.info("[MQTT] Disconnected from MQTT broker")
            return
        if rc == 0:
            logging.info("[MQTT] Disconnected from MQTT broker (clean)")
        else:
            logging.warning(
                f"[MQTT] Unexpected disconnect (rc={rc}); "
                "waiting for automatic reconnect and online republish"
            )

    def connect(self, timeout=10.0):
        """Connect, start the network loop, and wait for CONNACK."""
        self._intentional_disconnect = False
        try:
            self.client.connect(self.broker_address, self.broker_port, 60)
            self.client.loop_start()

            deadline = tm.monotonic() + float(timeout)
            while tm.monotonic() < deadline:
                if self.connected:
                    return True
                tm.sleep(0.05)

            logging.warning(
                f"[MQTT] Timed out waiting for CONNACK from "
                f"{self.broker_address}:{self.broker_port}"
            )
            # Stop the background loop so a failed startup does not keep retrying forever
            # without an owning MQTTClient reference.
            self._intentional_disconnect = True
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
            self.connected = False
            return False
        except Exception as e:
            logging.warning(f"[MQTT] Could not connect to broker: {e}")
            self.connected = False
            return False

    def publish(self, topic, message, retain=False):
        """JSON-encode ``message`` and publish to ``topic``."""
        if not self.connected:
            logging.debug(f"[MQTT] Skipping publish to {topic}: not connected")
            return
        try:
            self.client.publish(topic, json.dumps(message), retain=retain)
        except Exception as e:
            logging.warning(f"[MQTT] Could not publish to {topic}: {e}")

    def subscribe(self, topic, callback=None):
        """Subscribe to ``topic`` and optionally register a per-topic callback.

        Callbacks are always stored so they can be restored after reconnect.
        The actual subscribe is deferred until the client is connected.
        """
        if callback:
            self.topic_callbacks[topic] = callback

        if not self.connected:
            logging.info(f"[MQTT] Deferred subscribe to {topic} until connected")
            return

        try:
            self.client.subscribe(topic)
            logging.info(f"[MQTT] Subscribed to {topic}")
        except Exception as e:
            logging.warning(f"[MQTT] Could not subscribe to {topic}: {e}")

    def on_message(self, client, userdata, message):
        """Dispatch an inbound message to the registered topic callback."""
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
        """Publish offline status, then stop the loop and disconnect."""
        self._intentional_disconnect = True
        try:
            # First publish offline status
            if self.connected:
                self.client.publish(self._status_topic(), "offline", retain=True)
                logging.info("[MQTT] Published offline status before disconnecting")

            # Then disconnect properly
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False
            logging.info("[MQTT] Disconnected from MQTT broker")
        except Exception as e:
            logging.error(f"[MQTT] Error during graceful disconnect: {e}")
            self.connected = False

class StatePublisher:
    """Publish flap/motion/config state and HA discovery; handle inbound sets."""

    def __init__(self, mqtt_client, inside_lock_state=None, outside_lock_state=None, 
                motion_inside_state=None, motion_outside_state=None, prey_detected_state=None):
        self.mqtt_client = mqtt_client
        self.image_publish_thread = None
        self.stop_image_thread = False

        # Remember last published states so reconnect can restore HA availability.
        self._last_inside_lock = inside_lock_state
        self._last_outside_lock = outside_lock_state
        self._last_motion_inside = motion_inside_state
        self._last_motion_outside = motion_outside_state
        self._last_prey_detected = prey_detected_state

        if not self.mqtt_client.connected:
            # Wait up to 3 seconds for connection
            for __ in range(30):
                if self.mqtt_client.connected:
                    break
                tm.sleep(0.1)

        # After broker outages / LWT offline, republish discovery + last states.
        self.mqtt_client.register_reconnect_callback(self.refresh_after_reconnect)

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

    def refresh_after_reconnect(self):
        """Republish discovery and last known states after an MQTT reconnect."""
        logging.info("[MQTT] Refreshing discovery and state topics after reconnect")
        try:
            self.publish_discovery_topics()
            if self._last_inside_lock is not None:
                self.publish_lock_inside(self._last_inside_lock)
            if self._last_outside_lock is not None:
                self.publish_lock_outside(self._last_outside_lock)
            if self._last_motion_inside is not None:
                self.publish_motion_inside(self._last_motion_inside)
            if self._last_motion_outside is not None:
                self.publish_motion_outside(self._last_motion_outside)
            if self._last_prey_detected is not None:
                self.publish_prey_detected(self._last_prey_detected)
            self.publish_allowed_to_exit(CONFIG['ALLOWED_TO_EXIT'])
            self.publish_allowed_to_enter(CONFIG['ALLOWED_TO_ENTER'])
        except Exception as e:
            logging.error(f"[MQTT] Failed to refresh state after reconnect: {e}")

    def publish_lock_inside(self, locked: bool):
        """Publish inside lock state (``locked`` / ``unlocked``)."""
        self._last_inside_lock = locked
        topic = MQTTConfig.topics["inside_lock_state"]
        state = "locked" if locked else "unlocked"
        if self.mqtt_client.connected:
            self.mqtt_client.client.publish(topic, state, retain=True)

    def publish_lock_outside(self, locked: bool):
        """Publish outside lock state (``locked`` / ``unlocked``)."""
        self._last_outside_lock = locked
        topic = MQTTConfig.topics["outside_lock_state"]
        state = "locked" if locked else "unlocked"
        if self.mqtt_client.connected:
            self.mqtt_client.client.publish(topic, state, retain=True)

    def publish_motion_outside(self, detected: bool):
        """Publish outside PIR motion state."""
        self._last_motion_outside = detected
        topic = MQTTConfig.topics["motion_outside_state"]
        state = "detected" if detected else "not_detected"
        if self.mqtt_client.connected:
            self.mqtt_client.client.publish(topic, state, retain=True)

    def publish_motion_inside(self, detected: bool):
        """Publish inside PIR motion state."""
        self._last_motion_inside = detected
        topic = MQTTConfig.topics["motion_inside_state"]
        state = "detected" if detected else "not_detected"
        if self.mqtt_client.connected:
            self.mqtt_client.client.publish(topic, state, retain=True)

    def publish_prey_detected(self, detected: bool):
        """Publish prey-detection state."""
        self._last_prey_detected = detected
        topic = MQTTConfig.topics["prey_detected"]
        state = "detected" if detected else "not_detected"
        if self.mqtt_client.connected:
            self.mqtt_client.client.publish(topic, state, retain=True)
        
    def register_manual_override_handler(self, callback_function):
        """Subscribe to the manual-override topic with ``callback_function``."""
        topic = MQTTConfig.topics["manual_override"]
        self.mqtt_client.subscribe(topic, callback_function)

    def publish_allowed_to_exit(self, allowed: AllowedToExit):
        """Publish the current ALLOWED_TO_EXIT value (localized label)."""
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
        """Subscribe to entry/exit config set topics."""
        # Subscribe to config set topics
        self.mqtt_client.subscribe(MQTTConfig.topics["allowed_to_exit_set"], self.handle_allowed_to_exit_change)
        self.mqtt_client.subscribe(MQTTConfig.topics["allowed_to_enter_set"], self.handle_allowed_to_enter_change)
    
    def handle_allowed_to_exit_change(self, payload):
        """Apply an inbound ALLOWED_TO_EXIT change from MQTT."""
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
        """Apply an inbound ALLOWED_TO_ENTER change from MQTT."""
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
        """Resize/encode an image and publish it to the camera topic."""
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
            if not self.mqtt_client.connected:
                logging.debug("[MQTT] Skipping image publish: not connected")
                return
            topic = MQTTConfig.topics["camera_image"]
            self.mqtt_client.client.publish(topic, img_str, retain)

        except Exception as e:
            logging.warning(f"[MQTT] Could not publish image: {e}")
    
    def start_periodic_image_publishing(self, interval=None):
        """Start a daemon thread that periodically publishes the latest camera frame."""
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
        """Stop the periodic image-publishing thread."""
        self.stop_image_thread = True
        if self.image_publish_thread and self.image_publish_thread.is_alive():
            self.image_publish_thread.join(timeout=1.0)
            logging.info("[MQTT] Stopped periodic image publishing thread")

    def cleanup_old_discovery_topics(self):
        """Clear deprecated Home Assistant discovery topics (empty retain)."""
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
        """Publish Home Assistant MQTT discovery configs for this device."""
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
            "sw_version": Versioning.get_git_version()
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
        """Publish the current ALLOWED_TO_ENTER value (localized label)."""
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
        """Republish discovery and localized config topics after a language change."""
        logging.info("[MQTT] Updating language-dependent MQTT topics")
        
        # Republish discovery topics with new language-specific labels
        self.publish_discovery_topics()
        
        # Republish current states with translations
        self.publish_allowed_to_enter(CONFIG['ALLOWED_TO_ENTER'])
        
        logging.info("[MQTT] Language-dependent MQTT topics updated")

    def publish_event_type(self, event_type, cat_name=None):
        """Publish a motion-block event type (and optional cat name) to MQTT."""
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