"""MQTT client lifecycle and manual door override bridge."""
import logging

from src.baseconfig import CONFIG
from src.clock import monotonic_time
from src.mode import is_remote_mode
from src.mqtt import MQTTClient, StatePublisher

if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir

# Global variable for manual door control
manual_door_override = {'unlock_inside': False, 'unlock_outside': False, 'lock_inside': False, 'lock_outside': False}

# Global variables for MQTT client
mqtt_client = None
mqtt_publisher = None


def handle_manual_override(payload):
    """Map MQTT toggle_inside commands onto ``manual_door_override`` flags."""
    global manual_door_override
    try:
        logging.info(f"[BACKEND] Received manual override: {payload}")
        
        # Simple toggle command handling
        if isinstance(payload, str) and payload.strip().lower() == "toggle_inside":
            # Toggle the inside lock state
            if Magnets.instance:
                current_inside_state = Magnets.instance.get_inside_state()
                if current_inside_state:
                    manual_door_override['lock_inside'] = True
                    logging.info("[BACKEND] Manual override: Locking inside door")
                else:
                    manual_door_override['unlock_inside'] = True
                    logging.info("[BACKEND] Manual override: Unlocking inside door")
            return
            
        # Handle JSON payload for toggle
        if isinstance(payload, dict) and payload.get("command", "").lower() == "toggle_inside":
            # Toggle the inside lock state
            if Magnets.instance:
                current_inside_state = Magnets.instance.get_inside_state()
                if current_inside_state:
                    manual_door_override['lock_inside'] = True
                    logging.info("[BACKEND] Manual override: Locking inside door")
                else:
                    manual_door_override['unlock_inside'] = True
                    logging.info("[BACKEND] Manual override: Unlocking inside door")
            return
            
    except Exception as e:
        logging.error(f"[BACKEND] Error processing manual override: {e}")


def update_mqtt_config(config_key=None):
    """Publish ALLOWED_TO_EXIT / ALLOWED_TO_ENTER (or one key if ``config_key`` is set)."""
    global mqtt_publisher
    
    if not mqtt_publisher or not CONFIG['MQTT_ENABLED']:
        return
        
    try:
        if config_key is None or config_key.upper() == 'ALLOWED_TO_EXIT':
            mqtt_publisher.publish_allowed_to_exit(CONFIG['ALLOWED_TO_EXIT'])
            logging.info("[BACKEND] Published updated ALLOWED_TO_EXIT to MQTT")
            
        if config_key is None or config_key.upper() == 'ALLOWED_TO_ENTER':
            mqtt_publisher.publish_allowed_to_enter(CONFIG['ALLOWED_TO_ENTER'])
            logging.info("[BACKEND] Published updated ALLOWED_TO_ENTER to MQTT")
    
    except Exception as e:
        logging.error(f"[BACKEND] Error updating MQTT configuration: {e}")


def update_mqtt_language():
    """Refresh MQTT topics that depend on the configured UI language."""
    global mqtt_publisher
    
    if not mqtt_publisher or not CONFIG['MQTT_ENABLED']:
        return
        
    try:
        mqtt_publisher.update_language_dependent_topics()
        logging.info("[BACKEND] Updated MQTT language-dependent topics")
        
    except Exception as e:
        logging.error(f"[BACKEND] Error updating MQTT language-dependent topics: {e}")


def init_mqtt_client(magnets_instance=None, motion_outside=0, motion_inside=0):
    """Connect MQTT (if enabled), attach StatePublisher, and register override handler."""
    global mqtt_client, mqtt_publisher
    
    # Clean up any existing MQTT connections
    cleanup_mqtt()
    
    if CONFIG['MQTT_ENABLED']:
        logging.info("[BACKEND] Starting MQTT client...")
        if not CONFIG['MQTT_BROKER_ADDRESS'] or not CONFIG['MQTT_BROKER_PORT']:
            logging.error("[BACKEND] MQTT broker address or port is not configured. MQTT client will not be started.")
            mqtt_client = None
            mqtt_publisher = None
            return False
        
        try:            
            mqtt_client = MQTTClient(
                broker_address=CONFIG['MQTT_BROKER_ADDRESS'],
                broker_port=CONFIG['MQTT_BROKER_PORT'],
                username=CONFIG['MQTT_USERNAME'],
                password=CONFIG['MQTT_PASSWORD'],
                client_name=CONFIG['MQTT_DEVICE_ID']
            )
            
            connected = mqtt_client.connect()
            if not connected:
                logging.error("[BACKEND] Failed to connect to MQTT broker")
                mqtt_client = None
                mqtt_publisher = None
                return False
            
            # Only set up the publisher if we have a magnets instance
            if magnets_instance:
                # Inside state is inverted in magnets (True means unlocked)
                # but in MQTT publishing True means locked
                inside_lock_state = not magnets_instance.get_inside_state()
                outside_lock_state = not magnets_instance.get_outside_state()
                
                # Get current motion states
                motion_outside_state = motion_outside == 1
                motion_inside_state = motion_inside == 1
                
                # Determine prey detection state
                prey_detected_state = False
                try:
                    # Lazy import: loop <-> mqtt_bridge would otherwise be circular at load time.
                    from src.backend.loop import backend_main
                    prey_mono = float(getattr(backend_main, "prey_detection_mono", 0.0) or 0.0)
                    if prey_mono > 0.0:
                        prey_detected_state = (monotonic_time() - prey_mono) <= float(CONFIG['LOCK_DURATION_AFTER_PREY_DETECTION'])
                except Exception:
                    prey_detected_state = False
                
                mqtt_publisher = StatePublisher(
                    mqtt_client,
                    inside_lock_state=inside_lock_state,
                    outside_lock_state=outside_lock_state,
                    motion_inside_state=motion_inside_state, 
                    motion_outside_state=motion_outside_state,
                    prey_detected_state=prey_detected_state
                )
                
                mqtt_publisher.register_manual_override_handler(handle_manual_override)
                logging.info("[BACKEND] Registered manual override handler for MQTT publisher.")
                
                # Start publishing images
                mqtt_publisher.start_periodic_image_publishing()
                logging.info("[BACKEND] Started periodic camera image publishing")
            
            logging.info("[BACKEND] Started MQTT client.")
            return True
            
        except Exception as e:
            logging.error(f"[BACKEND] Could not start MQTT client: {e}")
            mqtt_client = None
            mqtt_publisher = None
            return False
    else:
        mqtt_client = None
        mqtt_publisher = None
        logging.info("[BACKEND] MQTT client is disabled.")
        return True


def cleanup_mqtt():
    """Stop image publishing and disconnect the MQTT client."""
    global mqtt_client, mqtt_publisher
    
    if mqtt_publisher and hasattr(mqtt_publisher, 'stop_periodic_image_publishing'):
        mqtt_publisher.stop_periodic_image_publishing()
    
    if mqtt_client:
        try:
            mqtt_client.disconnect()
            logging.info("[BACKEND] MQTT client disconnected")
        except Exception as e:
            logging.error(f"[BACKEND] Error disconnecting MQTT client: {e}")
    
    mqtt_client = None
    mqtt_publisher = None


def restart_mqtt():
    """Tear down and re-init MQTT using current magnet/PIR state and CONFIG."""
    global mqtt_client, mqtt_publisher
    
    # Only attempt to get states if we have valid instances
    try:
        motion_outside = 0
        motion_inside = 0
        magnets_instance = None
        
        if Magnets.instance:
            magnets_instance = Magnets.instance
        
        if Pir.instance:
            motion_outside, motion_inside, __, __ = Pir.instance.get_states()
            
        logging.info("[BACKEND] Restarting MQTT client with new settings")
        return init_mqtt_client(magnets_instance=magnets_instance, 
                               motion_outside=motion_outside, 
                               motion_inside=motion_inside)
    except Exception as e:
        logging.error(f"[BACKEND] Error restarting MQTT client: {e}")
