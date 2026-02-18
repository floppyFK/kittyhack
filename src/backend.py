import threading
import time as tm
import logging
import multiprocessing
from threading import Lock
from src.baseconfig import AllowedToEnter, AllowedToExit, CONFIG, set_language
from src.mode import is_remote_mode
from src.database import *

if is_remote_mode():
    from src.remote.hardware import Pir, Magnets, Rfid, RfidRunState  # type: ignore
else:
    from src.pir import Pir
    from src.magnets_rfid import Magnets, Rfid, RfidRunState
from src.camera import image_buffer
from src.helper import sigterm_monitor, EventType, check_allowed_to_exit
from src.model import ModelHandler, YoloModel
from src.mqtt import MQTTClient, StatePublisher

TAG_TIMEOUT = 30.0               # after 30 seconds, a detected tag is considered invalid
RFID_READER_OFF_DELAY = 15.0     # Turn the RFID reader off 15 seconds after the last detected motion outside
OPEN_OUTSIDE_TIMEOUT = 6.0 + CONFIG['PIR_INSIDE_THRESHOLD'] # Keep the magnet to the outside open for 6 + PIR_INSIDE_THRESHOLD seconds after the last motion on the inside
MAX_UNLOCK_TIME = 60.0           # Maximum time the door is allowed to stay open
LAZY_CAT_DELAY_PIR_MOTION = 6.0  # Keep the PIR active for an additional 6 seconds after the last detected motion when using PIR-based motion detection
LAZY_CAT_DELAY_CAM_MOTION = 12.0 # Keep the PIR active for an additional 12 seconds after the last detected motion when using camera-based motion detection

# Prepare gettext for translations based on the configured language
_ = set_language(CONFIG['LANGUAGE'])

# Initialize Model
if is_remote_mode() or CONFIG['USE_ALL_CORES_FOR_IMAGE_PROCESSING']:
    threads = multiprocessing.cpu_count()
else:
    threads = 1

if CONFIG['TFLITE_MODEL_VERSION']:
    logging.info(f"[BACKEND] Using TFLite model version {CONFIG['TFLITE_MODEL_VERSION']}")
    model_handler = ModelHandler(model="tflite",
                                 modeldir = f"./tflite/{CONFIG['TFLITE_MODEL_VERSION']}",
                                  graph = "cv-lite-model.tflite",
                                  labelfile = "labels.txt",
                                  model_image_size = 320,
                                  num_threads=threads)
else:
    logging.info(f"[BACKEND] Using YOLO model {YoloModel.get_model_path(CONFIG['YOLO_MODEL'])}")
    model_handler = ModelHandler(model="yolo",
                                 modeldir = YoloModel.get_model_path(CONFIG['YOLO_MODEL']),
                                  graph = "",
                                  labelfile = "labels.txt",
                                  resolution = "800x600",
                                  framerate = 10,
                                  jpeg_quality = 75,
                                  model_image_size = YoloModel.get_model_image_size(CONFIG['YOLO_MODEL']),
                                  num_threads=threads)

# Global variable for manual door control
manual_door_override = {'unlock_inside': False, 'unlock_outside': False, 'lock_inside': False, 'lock_outside': False}

# Global variables for MQTT client
mqtt_client = None
mqtt_publisher = None

# Global variable for motion states
motion_state = {"outside": 0, "inside": 0}
motion_state_lock = Lock()

def handle_manual_override(payload):
    """Handle manual override commands from MQTT"""
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
    """
    Update MQTT with the latest configuration values.
    
    Args:
        config_key (str, optional): Specific configuration key to update. 
            If None, update all publishable configurations.
    """
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
    """
    Update MQTT topics that depend on the current language setting.
    This is called when the language changes to ensure MQTT topics are updated accordingly.
    """
    global mqtt_publisher
    
    if not mqtt_publisher or not CONFIG['MQTT_ENABLED']:
        return
        
    try:
        mqtt_publisher.update_language_dependent_topics()
        logging.info("[BACKEND] Updated MQTT language-dependent topics")
        
    except Exception as e:
        logging.error(f"[BACKEND] Error updating MQTT language-dependent topics: {e}")

def init_mqtt_client(magnets_instance=None, motion_outside=0, motion_inside=0):
    """Initialize and start the MQTT client with current configuration settings"""
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
                if hasattr(backend_main, 'prey_detection_tm'):
                    prey_detected_state = (tm.time() - backend_main.prey_detection_tm) <= CONFIG['LOCK_DURATION_AFTER_PREY_DETECTION']
                
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
    """Clean up MQTT client resources"""
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

# Capture startup state (does not change at runtime)
DISABLE_RFID_READER_STARTUP = CONFIG.get('DISABLE_RFID_READER', False)

def backend_main(simulate_kittyflap = False):

    global manual_door_override

    tag_id = None
    tag_id_valid = False
    tag_id_from_video = None
    tag_timestamp = 0.0
    motion_outside = 0
    motion_inside = 0
    motion_outside_raw = 0
    motion_inside_raw = 0
    unlock_inside_decision_made = False
    motion_outside_tm = 0.0
    motion_inside_tm = 0.0
    motion_inside_raw_tm = 0.0
    last_motion_outside_tm = 0.0
    last_motion_inside_tm = 0.0
    last_motion_inside_raw_tm = 0.0
    first_motion_outside_tm = 0.0
    first_motion_inside_tm = 0.0
    first_motion_inside_raw_tm = 0.0
    motion_block_id = 0
    ids_with_mouse = []
    ids_of_current_motion_block = []
    known_rfid_tags = []
    cat_rfid_name_dict = get_cat_name_rfid_dict(CONFIG['KITTYHACK_DATABASE_PATH'])
    cat_settings_map = get_cat_settings_map(CONFIG['KITTYHACK_DATABASE_PATH'])
    unlock_inside_tm = 0.0
    unlock_inside = False
    unlock_outside_tm = 0.0
    # When per-cat exit is configured and no RFID is known yet while inside motion is active,
    # keep checking for RFID until we can decide.
    pending_exit_rfid_check = False
    inside_manually_unlocked = False
    inside_manually_locked_tm = 0.0
    backend_main.prey_detection_tm = 0.0
    additional_verdict_infos = []
    previous_use_camera_for_motion = None
    exit_in_progress = False

    # Register task in the sigterm_monitor object
    sigterm_monitor.register_task()

    # Start the camera
    logging.info("[BACKEND] Start the camera...")
    def run_camera():
        model_handler.run()

    # Run the camera in a separate thread
    camera_thread = threading.Thread(target=run_camera, daemon=True)
    camera_thread.start()
    model_handler.pause()

    # Initialize PIRs, Magnets and RFID
    pir = Pir(simulate_kittyflap=simulate_kittyflap)
    pir.init()
    if DISABLE_RFID_READER_STARTUP:
        rfid = Rfid(simulate_kittyflap=True)
    else:
        rfid = Rfid(simulate_kittyflap=simulate_kittyflap)
    magnets = Magnets(simulate_kittyflap=simulate_kittyflap)
    magnets.init()
    
    logging.info("[BACKEND] Wait for the sensors to stabilize...")
    tm.sleep(5.0)

    # Start the magnet control thread
    magnets.start_magnet_control()

    # Start PIR monitoring thread
    pir_thread = threading.Thread(target=pir.read, args=(), daemon=True)
    pir_thread.start()

    # Start the RFID reader (without the field enabled)
    rfid_thread = threading.Thread(target=rfid.run, args=(), daemon=True)
    rfid_thread.start()

    # Start the MQTT client
    init_mqtt_client(magnets_instance=magnets, motion_outside=motion_outside, motion_inside=motion_inside)

    def lazy_cat_workaround(current_motion_state: int | bool, last_motion_state: int | bool, current_motion_timestamp: float, delay=LAZY_CAT_DELAY_PIR_MOTION) -> int | bool:
        """
        Helps to keep a PIR sensor active for an additional configurable seconds after the last detected motion.

        Args:
            current_motion_state (int): The current state of motion detection (1 for motion detected, 0 for no motion).
            last_motion_state (int): The previous state of motion detection (1 for motion detected, 0 for no motion).
            current_motion_timestamp (float): The timestamp of the current motion detection event.
            delay (float): The additional delay in seconds to keep the PIR active after the last detected motion outside.

        Returns:
            int: The possibly modified current motion state, ensuring the PIR remains active for an additional configurable seconds 
            if the conditions are met.
        """
        if ( (current_motion_state == 0) and 
                (last_motion_state == 1) and
                ((tm.time() - current_motion_timestamp) < delay) ):
            current_motion_state = 1
            logging.debug(f"[BACKEND] Lazy cat workaround: Keep the PIR active for {delay-(tm.time()-current_motion_timestamp):.1f} seconds.")
        return current_motion_state
    
    def get_cat_name(rfid_tag):
        if rfid_tag:
            return cat_rfid_name_dict.get(rfid_tag, f"{_('Unknown RFID')}: {rfid_tag}")
        else:
            return _("No RFID found")
        
    while not sigterm_monitor.stop_now:
        try:
            tm.sleep(0.1)  # sleep to reduce CPU load

            # Decide if the camera or the PIR should be used for motion detection
            use_camera_for_motion = CONFIG['USE_CAMERA_FOR_MOTION_DETECTION']

            # Log if the configuration has changed
            if use_camera_for_motion != previous_use_camera_for_motion and model_handler.check_videostream_status():
                previous_use_camera_for_motion = use_camera_for_motion
                if use_camera_for_motion:
                    motion_source = "Camera"
                    model_handler.set_videostream_buffer_size(1)
                    # Check whether the model handler is running. If not, start it.
                    if model_handler.get_run_state() == False:
                        logging.info("[BACKEND] Starting model handler for camera-based motion detection.")
                        model_handler.resume()
                        tm.sleep(0.5)
                else:
                    motion_source = "PIR"
                    model_handler.set_videostream_buffer_size(30)
                    if motion_outside == 0 and model_handler.get_run_state() == True:
                        logging.info("[BACKEND] Currently no motion outside detected. Pausing model handler for PIR-based motion detection.")
                        model_handler.pause()
                logging.info(f"[BACKEND] Outside motion detection mode changed to {motion_source}.")
                image_buffer.clear()  # Clear the image buffer when switching motion detection mode

            last_outside = motion_outside
            last_inside = motion_inside
            last_inside_raw = motion_inside_raw

            if use_camera_for_motion:
                # Decide if motion occured currently. Look up to 5 seconds into the past for images with cats
                min_ts = tm.time() - 5.0
                cat_imgs = image_buffer.get_filtered_ids(min_timestamp=min_ts, min_own_cat_probability=CONFIG['CAT_THRESHOLD'])
                motion_outside = 1 if len(cat_imgs) > 0 else 0
                # Motion raw does not exist for the camera, so we set it to the same value as motion_outside
                motion_outside_raw = motion_outside
                # Still use PIR for inside motion
                __, motion_inside, __, motion_inside_raw = pir.get_states()
            else:
                motion_outside, motion_inside, motion_outside_raw, motion_inside_raw = pir.get_states()

            # Update the motion timestamps
            if motion_outside == 1:
                motion_outside_tm = tm.time()
            if motion_inside == 1:
                motion_inside_tm = tm.time()
            if motion_inside_raw == 1:
                motion_inside_raw_tm = tm.time()

            if use_camera_for_motion:
                # If we use the camera for motion detection, keep the outside motion-indicator longer active, since normally
                # no motion is detected anymore by the camera, when the cat is very close to the flap.
                motion_outside = lazy_cat_workaround(motion_outside, last_outside, motion_outside_tm, LAZY_CAT_DELAY_CAM_MOTION)
            else:
                # Since the PIR tracks motion in a wider area (and even directly in front of the flap), we do not need to keep
                # the motion active as long as with the camera motion detection
                motion_outside = lazy_cat_workaround(motion_outside, last_outside, motion_outside_tm, LAZY_CAT_DELAY_PIR_MOTION)
            
            motion_inside = lazy_cat_workaround(motion_inside, last_inside, motion_inside_tm, LAZY_CAT_DELAY_PIR_MOTION)
            motion_inside_raw = lazy_cat_workaround(motion_inside_raw, last_inside_raw, motion_inside_raw_tm, LAZY_CAT_DELAY_PIR_MOTION)

            # Update the shared motion state
            with motion_state_lock:
                motion_state["outside"] = motion_outside
                motion_state["inside"] = motion_inside

            previous_tag_id = tag_id
            tag_id, tag_timestamp = rfid.get_tag()

            # Check if the RFID reader is still running. Otherwise restart it.
            if rfid.get_run_state() == RfidRunState.stopped:
                logging.warning("[BACKEND] RFID reader stopped unexpectedly. Restarting RFID reader.")
                rfid_thread = threading.Thread(target=rfid.run, args=(), daemon=True)
                rfid_thread.start()

            # Outside motion stopped
            if last_outside == 1 and motion_outside == 0:
                if not use_camera_for_motion:
                    if model_handler.get_run_state() == True:
                        model_handler.pause()
                        # Wait for the last image to be processed
                        tm.sleep(0.5)
                unlock_inside_decision_made = False
                tag_id_valid = False
                last_motion_outside_tm = tm.time()
                logging.info(f"[BACKEND] {motion_source}-based motion detection: Motion stopped OUTSIDE (Block ID: '{motion_block_id}')")
                # Reset exit flag on block end
                exit_in_progress = False
                if (magnets.get_inside_state() == True and magnets.check_queued("lock_inside") == False and inside_manually_unlocked == False):
                    magnets.queue_command("lock_inside")

                # Decide if the cat went in or out:
                if first_motion_inside_raw_tm == 0.0 or (first_motion_outside_tm - first_motion_inside_raw_tm) > 60.0:
                    if unlock_inside_tm > first_motion_outside_tm and tag_id is not None:
                        logging.info("[BACKEND] Motion event conclusion: No motion inside detected but the inside was unlocked. Cat went probably to the inside (PIR interference issue).")
                        event_type = EventType.CAT_WENT_PROBABLY_INSIDE
                    elif mouse_check_conditions["no_mouse_detected"]:
                        logging.info("[BACKEND] Motion event conclusion: No one went inside.")
                        event_type = EventType.MOTION_OUTSIDE_ONLY
                    else:
                        logging.info("[BACKEND] Motion event conclusion: Motion outside with mouse detected and entry blocked.")
                        event_type = EventType.MOTION_OUTSIDE_WITH_MOUSE
                elif first_motion_outside_tm < first_motion_inside_raw_tm:
                    if mouse_check_conditions["no_mouse_detected"]:
                        logging.info("[BACKEND] Motion event conclusion: Cat went inside.")
                        event_type = EventType.CAT_WENT_INSIDE
                    else:
                        logging.info("[BACKEND] Motion event conclusion: Cat went inside with mouse detected.")
                        event_type = EventType.CAT_WENT_INSIDE_WITH_MOUSE
                else:
                    logging.info("[BACKEND] Motion event conclusion: Cat went outside.")
                    event_type = EventType.CAT_WENT_OUTSIDE

                if use_camera_for_motion:
                    if event_type == EventType.CAT_WENT_OUTSIDE:
                        # Use either the first_motion_outside_tm or the first_motion_inside_tm+2.5, whichever is earlier, as the timestamp for the event
                        log_start_tm = min(first_motion_outside_tm, first_motion_inside_tm + 2.5)
                    else:
                        # Log 2.5 seconds earlier, since the camera might not detect the cat immediately
                        log_start_tm = first_motion_outside_tm - 2.5
                else:
                    # Don't log earlier than the first motion outside timestamp when using PIR-based motion detection
                    log_start_tm = first_motion_outside_tm


                all_events = str(event_type)
                # Add all additional verdict information to the event type
                if additional_verdict_infos:
                    for info in additional_verdict_infos:
                        all_events += "," + str(info)
                additional_verdict_infos = []

                # Update the motion_block_id and the tag_id for for all elements between log_start_tm and last_motion_outside_tm
                img_ids_for_motion_block = image_buffer.get_filtered_ids(log_start_tm, last_motion_outside_tm)
                ids_exceeding_mouse_th = image_buffer.get_filtered_ids(log_start_tm, last_motion_outside_tm, min_mouse_probability=CONFIG['MIN_THRESHOLD'])
                ids_exceeding_nomouse_th = image_buffer.get_filtered_ids(log_start_tm, last_motion_outside_tm, min_no_mouse_probability=CONFIG['MIN_THRESHOLD'])
                ids_exceeding_own_cat_th = image_buffer.get_filtered_ids(log_start_tm, last_motion_outside_tm, min_own_cat_probability=CONFIG['MIN_THRESHOLD'])
                logging.info(f"""[BACKEND] {motion_source}-based motion detection: Detection summary:
                                                            - {len(img_ids_for_motion_block)} elements in current motion block (between {first_motion_outside_tm} and {last_motion_outside_tm})
                                                            - {len(ids_exceeding_mouse_th)} elements where "mouse" detection exceeded the min. logging threshold of {CONFIG['MIN_THRESHOLD']}
                                                            - {len(ids_exceeding_nomouse_th)} elements where "no-mouse" detection exceeded the min. logging threshold of {CONFIG['MIN_THRESHOLD']}
                                                            - {len(ids_exceeding_own_cat_th)} elements where "own cat" detection exceeded the min. logging threshold of {CONFIG['MIN_THRESHOLD']}
                                                            Event type: {all_events}
                                                            RFID tag: {tag_id or 'None'} 
                                                            Video tag: {tag_id_from_video or 'None'}""")
                # Log all events to the database, where either the mouse threshold is exceeded, the no-mouse threshold is exceeded,
                # or the own cat threshold is exceeded or a tag id was detected
                # as well as all outgoing events
                if ((len(ids_exceeding_mouse_th) + len(ids_exceeding_nomouse_th) +len(ids_exceeding_own_cat_th) > 0) or 
                    (event_type in [EventType.CAT_WENT_OUTSIDE]) or 
                    (tag_id is not None) or 
                    (tag_id_from_video is not None)):
                    for element in img_ids_for_motion_block:
                        image_buffer.update_block_id(element, motion_block_id)
                        # Prefer the tag_id from the RFID reader. If this is not available, fall back to the detected id from the video
                        if tag_id is not None:
                            image_buffer.update_tag_id(element, tag_id)
                        elif tag_id_from_video is not None:
                            image_buffer.update_tag_id(element, tag_id_from_video)
                    logging.info(f"[BACKEND] Minimal threshold exceeded or tag ID detected. Images will be written to the database. Updated block ID for {len(img_ids_for_motion_block)} elements to '{motion_block_id}' and tag ID to '{tag_id if tag_id is not None else ''}'")
                    # Write to the database in a separate thread
                    db_thread = threading.Thread(target=write_motion_block_to_db, args=(CONFIG['KITTYHACK_DATABASE_PATH'], motion_block_id, all_events), daemon=True)
                    db_thread.start()
                else:
                    logging.info(f"[BACKEND] No elements found that exceed the minimal threshold '{CONFIG['MIN_THRESHOLD']}' and no tag ID was detected. No database entry will be created.")
                    if len(img_ids_for_motion_block) > 0:
                        for element in img_ids_for_motion_block:
                            image_buffer.delete_by_id(element)
                
                # Reset the first motion timestamps
                first_motion_outside_tm = 0.0
                first_motion_inside_tm = 0.0
                first_motion_inside_raw_tm = 0.0

                # Publish the event to MQTT
                if mqtt_publisher:
                    if tag_id is not None:
                        cat_name = get_cat_name(tag_id)
                    else:
                        cat_name = get_cat_name(tag_id_from_video)
                    mqtt_publisher.publish_event_type(all_events, cat_name)
                    mqtt_publisher.publish_motion_outside(False)

                # Forget the video tag id
                tag_id_from_video = None
                if tag_id is not None:
                    rfid.set_tag(None, 0.0)
                    logging.info("[BACKEND] Forget the tag ID from the RFID reader.")

            if last_inside_raw == 1 and motion_inside_raw == 0: # Inside motion stopped (raw)
                last_motion_inside_raw_tm = motion_inside_raw_tm
                logging.debug(f"[BACKEND] Motion stopped INSIDE (raw)")
            
            if last_inside == 1 and motion_inside == 0: # Inside motion stopped
                last_motion_inside_tm = motion_inside_tm
                logging.info(f"[BACKEND] Motion stopped INSIDE")
                # Publish the inside motion state to MQTT
                if mqtt_publisher:
                    mqtt_publisher.publish_motion_inside(False)

            # Start the RFID thread with infinite read cycles, if it is not running and motion is detected outside or inside
            # Note: Check this here to enable the RFID reader as soon as motion is detected
            if motion_outside == 1 or motion_inside == 1:
                if rfid.get_field() == False and (tag_id == None or tag_id not in known_rfid_tags):
                    rfid.set_field(True)
                    logging.info(f"[BACKEND] Enabled RFID field.")
            
            # Outside motion detected
            if last_outside == 0 and motion_outside == 1:
                motion_block_id += 1
                # If we use the camera for motion detection, set the first motion timestamp a bit earlier to avoid missing the first motion
                if use_camera_for_motion:
                    first_motion_outside_tm = tm.time() - 0.5
                    additional_log_info = f"| configured cat detection threshold: {CONFIG['CAT_THRESHOLD']} "
                else:
                    first_motion_outside_tm = tm.time()
                    model_handler.resume()
                    additional_log_info = ""
                logging.info(f"[BACKEND] {motion_source}-based motion detection: Motion detected OUTSIDE {additional_log_info}(Block ID: {motion_block_id})")
                known_rfid_tags = db_get_all_rfid_tags(CONFIG['KITTYHACK_DATABASE_PATH'])
                cat_rfid_name_dict = get_cat_name_rfid_dict(CONFIG['KITTYHACK_DATABASE_PATH'])
                cat_settings_map = get_cat_settings_map(CONFIG['KITTYHACK_DATABASE_PATH'])
                logging.info(f"[BACKEND] cat_settings_map: {cat_settings_map}")
                if mqtt_publisher:
                    mqtt_publisher.publish_motion_outside(True)
            
            if last_inside_raw == 0 and motion_inside_raw == 1: # Inside motion detected
                logging.debug("[BACKEND] Motion detected INSIDE (raw)")
                first_motion_inside_raw_tm = tm.time()

            if last_inside == 0 and motion_inside == 1: # Inside motion detected
                logging.info("[BACKEND] Motion detected INSIDE")
                cat_settings_map = get_cat_settings_map(CONFIG['KITTYHACK_DATABASE_PATH'])
                logging.info(f"[BACKEND] cat_settings_map: {cat_settings_map}")
                first_motion_inside_tm = tm.time()
                # Determine if exit is allowed globally and per-cat (if identifiable and configured per-cat)
                per_cat_exit_allowed = False
                try:
                    # In per-cat mode we must base the decision on RFID only (camera is outside-facing)
                    if CONFIG['ALLOWED_TO_EXIT'] == AllowedToExit.CONFIGURE_PER_CAT:
                        if tag_id is None:
                            # No RFID yet: keep checking while inside motion remains active
                            pending_exit_rfid_check = True
                            if not rfid.get_field():
                                rfid.set_field(True)
                            logging.info("[BACKEND] Per-cat exit: waiting for RFID tag while inside motion is active (camera tag ignored).")
                        else:
                            if tag_id in cat_settings_map:
                                per_cat_exit_allowed = cat_settings_map[tag_id].get('allow_exit', True)
                            # Add per-cat exit note once tag is known in per-cat mode
                            try:
                                flag = str(EventType.EXIT_PER_CAT_ALLOWED) if per_cat_exit_allowed else str(EventType.EXIT_PER_CAT_DENIED)
                                if flag not in additional_verdict_infos:
                                    additional_verdict_infos.append(flag)
                                    logging.info(f"[BACKEND] Per-cat exit: added verdict info '{flag}' for RFID tag '{tag_id}'.")
                            except Exception:
                                pass
                    else:
                        # Non per-cat modes behave as before
                        per_cat_exit_allowed = True
                except Exception:
                    per_cat_exit_allowed = True

                # Only attempt unlock now if we are not waiting for an RFID tag
                if not pending_exit_rfid_check:
                    if check_allowed_to_exit() == True and per_cat_exit_allowed:
                        if magnets.get_inside_state() == True:
                            logging.info("[BACKEND] Inside magnet is already unlocked. Only one magnet is allowed. --> Outside magnet will not be unlocked.")
                        else:
                            logging.info("[BACKEND] Allow cats to exit.")
                            if magnets.check_queued("unlock_outside") == False:
                                magnets.queue_command("unlock_outside")
                                unlock_outside_tm = tm.time()
                                # Mark this motion block as exit
                                exit_in_progress = True
                    else:
                        logging.info("[BACKEND] No cats are allowed to exit. YOU SHALL NOT PASS!")
                
                # Publish the inside motion state to MQTT
                if mqtt_publisher:
                    mqtt_publisher.publish_motion_inside(True)

            # Turn off the RFID reader if no motion outside and inside
            if ( (motion_outside == 0) and (motion_inside == 0) and
                ((tm.time() - last_motion_outside_tm) > RFID_READER_OFF_DELAY) and
                ((tm.time() - last_motion_inside_tm) > RFID_READER_OFF_DELAY) ):
                if rfid.get_field():
                    logging.info(f"[BACKEND] No motion outside since {RFID_READER_OFF_DELAY} seconds after the last motion. Stopping RFID reader.")
                    rfid.set_field(False)
            
            # Close the magnet to the outside after the timeout
            if ( (motion_inside == 0) and
                (magnets.get_outside_state() == True) and
                ((tm.time() - last_motion_inside_tm) > OPEN_OUTSIDE_TIMEOUT) and
                (magnets.check_queued("lock_outside") == False) ):
                    magnets.queue_command("lock_outside")

            # Check also for a cat via the camera, if the option is enabled and no RFID tag is detected
            if CONFIG['USE_CAMERA_FOR_CAT_DETECTION'] and tag_id_from_video is None and motion_outside == 1:
                imgs_with_cats = image_buffer.get_filtered_ids(first_motion_outside_tm, min_own_cat_probability=CONFIG['CAT_THRESHOLD'])
                if len(imgs_with_cats) > 0:
                    # Find the element with the highest probability
                    max_prob = 0.0
                    detected_cat = ""
                    for element in imgs_with_cats:
                        img = image_buffer.get_by_id(element)
                        for obj in getattr(img, 'detected_objects', []):
                            obj_name = getattr(obj, 'object_name', '').lower()
                            obj_probability = getattr(obj, 'probability', 0.0)
                            if obj_name not in ["prey", "beute"] and obj_probability > max_prob:
                                max_prob = obj_probability
                                detected_cat = obj_name
                    if detected_cat != "":
                        logging.info(f"[BACKEND] Detected cat '{detected_cat}' by video stream with probability {max_prob:.2f} in image ID {element}")
                        # Look for the cat name in the values of the dictionary
                        matching_tag = next((rfid for rfid, name in cat_rfid_name_dict.items() if name.lower() == detected_cat), None)
                        if matching_tag:
                            tag_id_from_video = matching_tag
                            logging.info(f"[BACKEND] Detected cat '{detected_cat}' matches RFID tag '{tag_id_from_video}'")
                
            # Check for a valid RFID tag
            if ( tag_id and
                tag_id != previous_tag_id and 
                rfid.get_field() ):
                logging.info(f"[BACKEND] RFID tag detected: '{tag_id}'.")
                if tag_id in known_rfid_tags:
                    rfid.set_field(False)
                    logging.info(f"[BACKEND] Detected RFID tag {tag_id} matches a known tag. Disabled RFID field.")

            # Handle pending per-cat exit decision while inside motion remains active
            if pending_exit_rfid_check:
                if motion_inside == 0:
                    pending_exit_rfid_check = False
                    logging.info("[BACKEND] Inside motion ended before RFID tag was detected; outside will remain locked.")
                elif tag_id is not None:
                    try:
                        per_cat_exit_allowed2 = False
                        if tag_id in cat_settings_map:
                            per_cat_exit_allowed2 = cat_settings_map[tag_id].get('allow_exit', True)

                        if magnets.get_inside_state() == True:
                            logging.info("[BACKEND] Inside magnet is already unlocked. Only one magnet is allowed. --> Outside magnet will not be unlocked.")
                        else:
                            flag = str(EventType.EXIT_PER_CAT_ALLOWED) if per_cat_exit_allowed2 else str(EventType.EXIT_PER_CAT_DENIED)
                            if check_allowed_to_exit() and per_cat_exit_allowed2:
                                logging.info("[BACKEND] Allow cats to exit (per-cat: RFID tag detected).")
                                if magnets.check_queued("unlock_outside") == False:
                                    magnets.queue_command("unlock_outside")
                                    unlock_outside_tm = tm.time()
                                    # Mark this motion block as exit
                                    exit_in_progress = True
                            else:
                                logging.info("[BACKEND] No cats are allowed to exit (per-cat decision). YOU SHALL NOT PASS!")

                            if flag not in additional_verdict_infos:
                                additional_verdict_infos.append(flag)
                                logging.info(f"[BACKEND] Per-cat exit: added verdict info '{flag}' for RFID tag '{tag_id}'.")
                                
                    except Exception as _e:
                        logging.error(f"[BACKEND] Error while deciding per-cat exit after RFID detection: {_e}")
                    finally:
                        pending_exit_rfid_check = False

            # Check if we are allowed to open the inside direction
            if motion_outside and not unlock_inside_decision_made:
                # Skip entry decision if this motion block represents an exit
                if exit_in_progress:
                    unlock_inside_decision_made = True
                    logging.info("[BACKEND] Skipping entry decision because exit is in progress for this motion block.")
                else:
                    # Determine identified tag (RFID preferred, else camera tag)
                    identified_tag = tag_id if tag_id in known_rfid_tags else (tag_id_from_video if tag_id_from_video in known_rfid_tags else None)

                    # Decide by mode
                    if CONFIG['ALLOWED_TO_ENTER'] == AllowedToEnter.CONFIGURE_PER_CAT and identified_tag is not None:
                        per_cat_entry_allowed = False
                        if identified_tag in cat_settings_map:
                            per_cat_entry_allowed = cat_settings_map[identified_tag].get('allow_entry', True)
                        # Only allow entry if identified and per-cat says True
                        tag_id_valid = bool(identified_tag) and per_cat_entry_allowed
                        unlock_inside_decision_made = True
                        # Annotate per-cat entry decision
                        try:
                            flag = str(EventType.ENTRY_PER_CAT_ALLOWED) if tag_id_valid else str(EventType.ENTRY_PER_CAT_DENIED)
                            if flag not in additional_verdict_infos:
                                additional_verdict_infos.append(flag)
                                logging.info(f"[BACKEND] Per-cat entry: added verdict info '{flag}' for RFID tag '{identified_tag}'.")
                        except Exception:
                            pass
                        if tag_id_valid:
                            logging.info("[BACKEND] Per-cat mode: entry allowed for this cat.")
                        else:
                            logging.info("[BACKEND] Per-cat mode: entry not allowed (unknown or disabled).")
                    elif CONFIG['ALLOWED_TO_ENTER'] == AllowedToEnter.KNOWN and identified_tag is not None:
                        tag_id_valid = True
                        unlock_inside_decision_made = True
                        logging.info("[BACKEND] Detected RFID tag is in the database. Kitty is allowed to enter...")
                    elif CONFIG['ALLOWED_TO_ENTER'] == AllowedToEnter.ALL_RFIDS and tag_id is not None:
                        tag_id_valid = True
                        unlock_inside_decision_made = True
                        logging.info("[BACKEND] All RFID tags are allowed. Kitty is allowed to enter...")
                    elif CONFIG['ALLOWED_TO_ENTER'] == AllowedToEnter.NONE:
                        tag_id_valid = False
                        unlock_inside_decision_made = True
                        logging.info("[BACKEND] No cats are allowed to enter. The door stays closed.")
                    elif CONFIG['ALLOWED_TO_ENTER'] == AllowedToEnter.ALL:
                        tag_id_valid = True
                        unlock_inside_decision_made = True
                        logging.info("[BACKEND] All cats are allowed to enter. Kitty is allowed to enter...")

            # Forget the tag after the tag timeout and no motion outside:
            if ( (tag_id is not None) and 
                (tm.time() > (tag_timestamp+TAG_TIMEOUT)) and
                (motion_outside == 0) ):
                rfid.set_tag(None, 0.0)
                logging.info("[BACKEND] Tag timeout reached. Forget the tag.")

            if image_buffer.size() > 0 and first_motion_outside_tm > 0.0:
                # Process all elements in the buffer
                ids_of_current_motion_block = image_buffer.get_filtered_ids(min_timestamp=first_motion_outside_tm)
                ids_with_mouse = image_buffer.get_filtered_ids(min_timestamp=first_motion_outside_tm, min_mouse_probability=CONFIG['MOUSE_THRESHOLD'])
            else:
                ids_of_current_motion_block = []
                ids_with_mouse = []

            # Check if the inside magnet should be unlocked
            # Apply per-cat prey detection override (disable prey detection for this cat if configured)
            prey_detection_enabled = CONFIG['MOUSE_CHECK_ENABLED']
            per_cat_prey_detection_disabled = False
            try:
                current_tag_any = tag_id if tag_id else tag_id_from_video
                if current_tag_any and current_tag_any in cat_settings_map:
                    if cat_settings_map[current_tag_any].get('enable_prey_detection', True) is False:
                        prey_detection_enabled = False
                        per_cat_prey_detection_disabled = True
                        try:
                            flag = str(EventType.PER_CAT_PREY_DISABLED)
                            if flag not in additional_verdict_infos:
                                additional_verdict_infos.append(flag)
                                logging.info(f"[BACKEND] Per-cat prey detection: added verdict info '{flag}' for RFID tag '{current_tag_any}'.")
                        except Exception:
                            pass
            except Exception:
                pass

            analysis_elapsed_s = 0.0
            try:
                if first_motion_outside_tm > 0.0:
                    analysis_elapsed_s = max(0.0, tm.time() - float(first_motion_outside_tm))
            except Exception:
                analysis_elapsed_s = 0.0

            mouse_check_conditions = {
                "mouse_check_disabled": prey_detection_enabled == False,
                "no_mouse_detected": len(ids_with_mouse) == 0,
                "sufficient_analysis_time": analysis_elapsed_s >= float(CONFIG.get('MIN_SECONDS_TO_ANALYZE', 0.0) or 0.0),
            }

            mouse_check = mouse_check_conditions["mouse_check_disabled"] or (
                mouse_check_conditions["no_mouse_detected"] and mouse_check_conditions["sufficient_analysis_time"]
            )

            # If per-cat prey detection is disabled for the currently identified cat,
            # ignore the global prey timeout gate for unlocking. This ensures that
            # late RFID identification can still disable prey gating even if prey was
            # detected a few seconds earlier by the camera.
            no_prey_within_timeout_effective = (
                True if per_cat_prey_detection_disabled
                else (tm.time() - backend_main.prey_detection_tm) > CONFIG['LOCK_DURATION_AFTER_PREY_DETECTION']
            )

            unlock_inside_conditions = {
                "motion_outside": motion_outside == 1,
                "tag_id_valid": tag_id_valid,
                "inside_locked": magnets.get_inside_state() == False,
                "mouse_check": mouse_check,
                "outside_locked": magnets.get_outside_state() == False,
                "no_unlock_queued": magnets.check_queued("unlock_inside") == False,
                "no_prey_within_timeout": no_prey_within_timeout_effective,
                "not_manually_locked": inside_manually_locked_tm == 0.0 or (tm.time() - inside_manually_locked_tm) > MAX_UNLOCK_TIME
            }

            if not hasattr(backend_main, "previous_mouse_check_conditions"):
                backend_main.previous_mouse_check_conditions = mouse_check_conditions
            else:
                for key, value in mouse_check_conditions.items():
                    if backend_main.previous_mouse_check_conditions[key] != value:
                        logging.info(f"[BACKEND] Mouse check condition '{key}' changed to {value}.")
                
                # If the prey detection is enabled, check if this is the first iteration with detected prey
                if mouse_check == False and mouse_check_conditions["no_mouse_detected"] == False and backend_main.previous_mouse_check_conditions["no_mouse_detected"] == True:
                    backend_main.prey_detection_tm = tm.time()
                    logging.info(f"[BACKEND] Detected prey in the images. Set the timestamp for prey detection to {backend_main.prey_detection_tm}.")

                if backend_main.previous_mouse_check_conditions != mouse_check_conditions:
                    logging.info(f"[BACKEND] Mouse check conditions: {mouse_check_conditions}")
                    backend_main.previous_mouse_check_conditions = mouse_check_conditions

            if not hasattr(backend_main, "previous_unlock_inside_conditions"):
                backend_main.previous_unlock_inside_conditions = unlock_inside_conditions
            else:
                for key, value in unlock_inside_conditions.items():
                    if backend_main.previous_unlock_inside_conditions[key] != value:
                        logging.info(f"[BACKEND] Unlock inside Condition '{key}' changed to {value}. ({sum(unlock_inside_conditions.values())}/{len(unlock_inside_conditions)} conditions fulfilled)")
                        if key == "inside_locked" and mqtt_publisher:
                            mqtt_publisher.publish_lock_inside(value)
                        elif key == "outside_locked" and mqtt_publisher:
                            mqtt_publisher.publish_lock_outside(value)
                        elif key == "no_prey_within_timeout" and mqtt_publisher:
                            mqtt_publisher.publish_prey_detected(not value)
                if backend_main.previous_unlock_inside_conditions != unlock_inside_conditions:
                    logging.info(f"[BACKEND] Unlock inside conditions: {unlock_inside_conditions}")
                    backend_main.previous_unlock_inside_conditions = unlock_inside_conditions

            unlock_inside = all(unlock_inside_conditions.values())

            # Lock the inside if there was a mouse detected after the door was already unlocked
            if (mouse_check == False and magnets.get_inside_state() and magnets.check_queued("lock_inside") == False and inside_manually_unlocked == False):
                    magnets.queue_command("lock_inside")
                    unlock_inside_tm = 0.0

            if unlock_inside or manual_door_override['unlock_inside']:
                logging.info(f"[BACKEND] Door unlock requested {'(manual override)' if manual_door_override['unlock_inside'] else ''}")
                logging.debug(f"[BACKEND] Motion outside: {motion_outside}, Motion inside: {motion_inside}, Tag ID: {tag_id}, Tag valid: {tag_id_valid}, Motion block ID: {motion_block_id}, Images with mouse: {len(ids_with_mouse)}, Images in current block: {len(ids_of_current_motion_block)} ({ids_of_current_motion_block})")
                if manual_door_override['unlock_inside'] and magnets.get_inside_state():
                    logging.info("[BACKEND] Manual override: Inside door is already open.")
                else:
                    magnets.empty_queue()
                    magnets.queue_command("unlock_inside")
                    unlock_inside_tm = tm.time()
                    if manual_door_override['unlock_inside']:
                        inside_manually_unlocked = True
                        flag = str(EventType.MANUALLY_UNLOCKED)
                        if flag not in additional_verdict_infos:
                            additional_verdict_infos.append(flag)
                            logging.info(f"[BACKEND] Added verdict info '{flag}' due to manual inside unlock.")
                    else:
                        inside_manually_unlocked = False
                
                manual_door_override['unlock_inside'] = False

            if manual_door_override['unlock_outside']:
                if magnets.get_outside_state():
                    logging.info("[BACKEND] Manual override: Outside door is already open.")
                else:
                    logging.info("[BACKEND] Manual override: Opening outside door")
                    magnets.empty_queue()
                    magnets.queue_command("unlock_outside")
                    unlock_outside_tm = tm.time()
                
                manual_door_override['unlock_outside'] = False

            if manual_door_override['lock_inside']:
                if magnets.get_inside_state():
                    logging.info("[BACKEND] Manual override: Locking inside door")
                    magnets.empty_queue()
                else:
                    logging.info("[BACKEND] Manual override: Inside door is already locked.")
                inside_manually_unlocked = False
                inside_manually_locked_tm = tm.time()  # Set the timestamp when manually locked
                manual_door_override['lock_inside'] = False
                flag = str(EventType.MANUALLY_LOCKED)
                if flag not in additional_verdict_infos:
                    additional_verdict_infos.append(flag)
                    logging.info(f"[BACKEND] Added verdict info '{flag}' due to manual inside lock.")
                
            # Check if maximum unlock time is exceeded
            if magnets.get_inside_state() and (tm.time() - unlock_inside_tm > MAX_UNLOCK_TIME) and magnets.check_queued("lock_inside") == False:
                logging.warning("[BACKEND] Maximum unlock time exceeded for inside door. Forcing lock.")
                magnets.queue_command("lock_inside")
                if inside_manually_unlocked:
                    inside_manually_unlocked = False
                flag = str(EventType.MAX_UNLOCK_TIME_EXCEEDED)
                if flag not in additional_verdict_infos:
                     additional_verdict_infos.append(flag)
                     logging.info(f"[BACKEND] Added verdict info '{flag}' due to maximum inside unlock time exceeded.")
                
            if magnets.get_outside_state() and (tm.time() - unlock_outside_tm > MAX_UNLOCK_TIME) and magnets.check_queued("lock_outside") == False:
                logging.warning("[BACKEND] Maximum unlock time exceeded for outside door. Forcing lock.")
                magnets.queue_command("lock_outside")
                
        except Exception as e:
            logging.error(f"[BACKEND] Exception in backend occured: {e}")

    # RFID Cleanup on shutdown:
    rfid.stop_read(wait_for_stop=True)
    rfid.set_field(False)
    rfid.set_power(False)

    # MQTT Cleanup on shutdown:
    cleanup_mqtt()

    # Ensure all magnets are locked before exit
    if Magnets.instance:
        Magnets.instance.empty_queue(shutdown=True)

    logging.info("[BACKEND] Stopped backend.")
    sigterm_monitor.signal_task_done()

def restart_mqtt():
    """Restart the MQTT client with the current configuration settings"""
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
        return False