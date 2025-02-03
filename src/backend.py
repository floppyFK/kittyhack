import os
import threading
import time as tm
import logging
import cv2
from src.pir import Pir
from src.rfid import Rfid, RfidRunState
from src.database import *
from src.magnets import Magnets
from src.camera import image_buffer, TfLite
from src.helper import CONFIG, sigterm_monitor

TAG_TIMEOUT = 30.0               # after 30 seconds, a detected tag is considered invalid
RFID_READER_OFF_DELAY = 15.0     # Turn the RFID reader off 15 seconds after the last detected motion outside
OPEN_OUTSIDE_TIMEOUT = 6.0 + CONFIG['PIR_INSIDE_THRESHOLD'] # Keep the magnet to the outside open for 6 + PIR_INSIDE_THRESHOLD seconds after the last motion on the inside
MAX_UNLOCK_TIME = 45.0           # Maximum time the door is allowed to stay open

# Initialize TfLite
tflite = TfLite(modeldir = "./tflite/",
                graph = "cv-lite-model.tflite",
                labelfile = "labels.txt",
                resolution = "800x600",
                framerate = 10,
                jpeg_quality = 75,
                simulate_kittyflap = CONFIG['SIMULATE_KITTYFLAP'])

# Global variable for manual door control
manual_door_override = {'unlock_inside': False, 'unlock_outside': False, 'lock_inside': False, 'lock_outside': False}

def backend_main(simulate_kittyflap = False):

    global manual_door_override

    tag_id = None
    tag_id_valid = False
    tag_timestamp = 0.0
    motion_outside = 0
    motion_inside = 0
    unlock_inside_decision_made = False
    motion_outside_tm = 0.0
    motion_inside_tm = 0.0
    last_motion_outside_tm = 0.0
    last_motion_inside_tm = 0.0
    motion_block_id = 0
    ids_with_mouse = []
    ids_of_current_motion_block = []
    known_rfid_tags = []
    unlock_inside_tm = 0.0
    unlock_outside_tm = 0.0
    inside_manually_unlocked = False

    # Register task in the sigterm_monitor object
    sigterm_monitor.register_task()

    # Start the camera
    logging.info("[BACKEND] Start the camera...")
    def run_camera():
        tflite.run()

    # Run the camera in a separate thread
    camera_thread = threading.Thread(target=run_camera, daemon=True)
    camera_thread.start()
    tflite.pause()

    # Initialize PIRs, Magnets and RFID
    pir = Pir(simulate_kittyflap=simulate_kittyflap)
    pir.init()
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

    def lazy_cat_workaround(motion_outside, last_outside, motion_outside_tm):
        # Lazy cat workaround: Keep the outside PIR active for 5 further seconds after the last detected motion outside
        if ( (motion_outside == 0) and 
             (last_outside == 1) and
             ((tm.time() - motion_outside_tm) < 5.0) ):
            motion_outside = 1
            logging.debug(f"[BACKEND] Lazy cat workaround: Keep the outside PIR active for {5.0-(tm.time()-motion_outside_tm):.1f} seconds.")
        return motion_outside

    while not sigterm_monitor.stop_now:
        try:
            tm.sleep(0.1)  # sleep to reduce CPU load

            last_outside = motion_outside
            last_inside = motion_inside
            motion_outside, motion_inside = pir.get_states()

            # Update the motion timestamps
            if motion_outside == 1:
                motion_outside_tm = tm.time()
            if motion_inside == 1:
                motion_inside_tm = tm.time()

            motion_outside = lazy_cat_workaround(motion_outside, last_outside, motion_outside_tm)

            previous_tag_id = tag_id
            tag_id, tag_timestamp = rfid.get_tag()

            # Check if the RFID reader is still running. Otherwise restart it.
            if rfid.get_run_state() == RfidRunState.stopped:
                logging.warning("[BACKEND] RFID reader stopped unexpectedly. Restarting RFID reader.")
                rfid_thread = threading.Thread(target=rfid.run, args=(), daemon=True)
                rfid_thread.start()

            # Outside motion stopped
            if last_outside == 1 and motion_outside == 0:
                if tflite.get_run_state() == True:
                    tflite.pause()
                    # Wait for the last image to be processed
                    tm.sleep(0.5)
                unlock_inside_decision_made = False
                tag_id_valid = False
                last_motion_outside_tm = tm.time()
                logging.info(f"[BACKEND] Motion stopped OUTSIDE (Block ID: '{motion_block_id}')")
                if (magnets.get_inside_state() == True and magnets.check_queued("lock_inside") == False and inside_manually_unlocked == False):
                    magnets.queue_command("lock_inside")

                # Update the motion_block_id and the tag_id for for all elements between first_motion_outside_tm and last_motion_outside_tm
                img_ids_for_motion_block = image_buffer.get_filtered_ids(first_motion_outside_tm, last_motion_outside_tm)
                logging.info(f"[BACKEND] Found {len(img_ids_for_motion_block)} elements between {first_motion_outside_tm} and {last_motion_outside_tm}")
                if len(img_ids_for_motion_block) > 0:
                    for element in img_ids_for_motion_block:
                        image_buffer.update_block_id(element, motion_block_id)
                        if tag_id is not None:
                            image_buffer.update_tag_id(element, tag_id)
                    logging.info(f"[BACKEND] Updated block ID for {len(img_ids_for_motion_block)} elements to '{motion_block_id}' and tag ID to '{tag_id if tag_id is not None else ''}'")
                    # Write to the database in a separate thread
                    db_thread = threading.Thread(target=write_motion_block_to_db, args=(CONFIG['KITTYHACK_DATABASE_PATH'], motion_block_id), daemon=True)
                    db_thread.start()

            # Just double check that the inside magnet is released ( == inside locked) if no motion is detected outside
            if (motion_outside == 0 and magnets.get_inside_state() == True and magnets.check_queued("lock_inside") == False and (tm.time() - unlock_inside_tm > MAX_UNLOCK_TIME)):
                    magnets.queue_command("lock_inside")
                    
            
            if last_inside == 1 and motion_inside == 0: # Inside motion stopped
                last_motion_inside_tm = motion_inside_tm
                logging.info(f"[BACKEND] Motion stopped INSIDE")
            
            if last_outside == 0 and motion_outside == 1: # Outside motion detected
                motion_block_id += 1
                logging.info(f"[BACKEND] Motion detected OUTSIDE (Block ID: {motion_block_id})")
                first_motion_outside_tm = tm.time()
                tflite.resume()
                known_rfid_tags = db_get_all_rfid_tags(CONFIG['KITTYHACK_DATABASE_PATH'])
            
            if last_inside == 0 and motion_inside == 1: # Inside motion detected
                logging.info("[BACKEND] Motion detected INSIDE")
                if CONFIG['ALLOWED_TO_EXIT'] == True:
                    if magnets.get_inside_state() == True:
                        logging.info("[BACKEND] Inside magnet is already unlocked. Only one magnet is allowed. --> Outside magnet will not be unlocked.")
                    else:
                        logging.info("[BACKEND] Allow cats to exit.")
                        if magnets.check_queued("unlock_outside") == False:
                            magnets.queue_command("unlock_outside")
                            unlock_outside_tm = tm.time()
                else:
                    logging.info("[BACKEND] No cats are allowed to exit. YOU SHALL NOT PASS!")

            # Turn off the RFID reader if no motion outside and inside
            if ( (motion_outside == 0) and (motion_inside == 0) and
                ((tm.time() - last_motion_outside_tm) > RFID_READER_OFF_DELAY) and
                ((tm.time() - last_motion_inside_tm) > RFID_READER_OFF_DELAY) ):
                if rfid.get_field():
                    logging.info(f"[BACKEND] No motion outside since {RFID_READER_OFF_DELAY} seconds after the last motion. Stopping RFID reader.")
                    rfid.set_field(False)
            
            # Start the RFID thread with infinite read cycles, if it is not running and motion is detected outside or inside
            if motion_outside == 1 or motion_inside == 1:
                if rfid.get_field() == False and (tag_id == None or tag_id not in known_rfid_tags):
                    rfid.set_field(True)
                    logging.info(f"[BACKEND] Enabled RFID field.")
            
            # Close the magnet to the outside after the timeout
            if ( (motion_inside == 0) and
                (magnets.get_outside_state() == True) and
                ((tm.time() - last_motion_inside_tm) > OPEN_OUTSIDE_TIMEOUT) and
                (magnets.check_queued("lock_outside") == False) ):
                    magnets.queue_command("lock_outside")
                
            # Check for a valid RFID tag
            if ( tag_id and
                tag_id != previous_tag_id and 
                rfid.get_field() ):
                logging.info(f"[BACKEND] RFID tag detected: '{tag_id}'.")
                if tag_id in known_rfid_tags:
                    rfid.set_field(False)
                    logging.info(f"[BACKEND] Detected RFID tag {tag_id} matches a known tag. Disabled RFID field.")

            # Check if we are allowed to open the inside direction
            if motion_outside and not unlock_inside_decision_made:
                if CONFIG['ALLOWED_TO_ENTER'] == AllowedToEnter.KNOWN and tag_id in known_rfid_tags:
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

            if image_buffer.size() > 0:
                # Process all elements in the buffer
                ids_of_current_motion_block = image_buffer.get_filtered_ids(min_timestamp=first_motion_outside_tm)
                ids_with_mouse = image_buffer.get_filtered_ids(min_timestamp=first_motion_outside_tm, min_mouse_probability=CONFIG['MOUSE_THRESHOLD'])
            else:
                ids_of_current_motion_block = []
                ids_with_mouse = []

            # Check if the inside magnet should be unlocked
            mouse_check_conditions = {
                "mouse_check_enabled": CONFIG['MOUSE_CHECK_ENABLED'] == False,
                "no_mouse_detected": len(ids_with_mouse) == 0,
                "sufficient_pictures": len(ids_of_current_motion_block) >= CONFIG['MIN_PICTURES_TO_ANALYZE']
            }

            mouse_check = mouse_check_conditions["mouse_check_enabled"] or (mouse_check_conditions["no_mouse_detected"] and mouse_check_conditions["sufficient_pictures"])

            unlock_inside_conditions = {
                "motion_outside": motion_outside == 1,
                "tag_id_valid": tag_id_valid,
                "inside_locked": magnets.get_inside_state() == False,
                "mouse_check": mouse_check,
                "outside_locked": magnets.get_outside_state() == False,
                "no_unlock_queued": magnets.check_queued("unlock_inside") == False
            }

            if not hasattr(backend_main, "previous_mouse_check_conditions"):
                backend_main.previous_mouse_check_conditions = mouse_check_conditions
            else:
                for key, value in mouse_check_conditions.items():
                    if backend_main.previous_mouse_check_conditions[key] != value:
                        logging.info(f"[BACKEND] Mouse check condition '{key}' changed to {value}.")
                if backend_main.previous_mouse_check_conditions != mouse_check_conditions:
                    logging.info(f"[BACKEND] Mouse check conditions: {mouse_check_conditions}")
                    backend_main.previous_mouse_check_conditions = mouse_check_conditions

            if not hasattr(backend_main, "previous_unlock_inside_conditions"):
                backend_main.previous_unlock_inside_conditions = unlock_inside_conditions
            else:
                total_unlock_inside_conditions = len(unlock_inside_conditions)
                for key, value in unlock_inside_conditions.items():
                    if backend_main.previous_unlock_inside_conditions[key] != value:
                        logging.info(f"[BACKEND] Unlock inside Condition '{key}' changed to {value}. ({sum(unlock_inside_conditions.values())}/{total_unlock_inside_conditions} conditions fulfilled)")
                if backend_main.previous_unlock_inside_conditions != unlock_inside_conditions:
                    logging.info(f"[BACKEND] Unlock inside conditions: {unlock_inside_conditions}")
                    backend_main.previous_unlock_inside_conditions = unlock_inside_conditions

            unlock_inside = all(unlock_inside_conditions.values())

            if unlock_inside or manual_door_override['unlock_inside']:
                logging.info(f"[BACKEND] Door unlock requested {'(manual override)' if manual_door_override['unlock_inside'] else ''}")
                logging.debug(f"[BACKEND] Motion outside: {motion_outside}, Motion inside: {motion_inside}, Tag ID: {tag_id}, Tag valid: {tag_id_valid}, Motion block ID: {motion_block_id}, Images with mouse: {len(ids_with_mouse)}, Images in current block: {len(ids_of_current_motion_block)} ({ids_of_current_motion_block})")
                if manual_door_override['unlock_inside'] and magnets.get_inside_state():
                    logging.info("[BACKEND] Manual override: Inside door is already open.")
                else:
                    magnets.empty_queue()
                    magnets.queue_command("unlock_inside")
                    unlock_inside_tm = tm.time()
                    inside_manually_unlocked = True if manual_door_override['unlock_inside'] else False
                
                manual_door_override['unlock_inside'] = False

            if manual_door_override['unlock_inside']:
                if magnets.get_outside_state():
                    logging.info("[BACKEND] Manual override: Outside door is already open.")
                else:
                    logging.info("[BACKEND] Manual override: Opening outside door")
                    magnets.empty_queue()
                    magnets.queue_command("unlock_outside")
                    unlock_outside_tm = tm.time()
                
                manual_door_override['unlock_inside'] = False

            if manual_door_override['lock_inside']:
                if magnets.get_inside_state():
                    logging.info("[BACKEND] Manual override: Locking inside door")
                    magnets.empty_queue()
                else:
                    logging.info("[BACKEND] Manual override: Inside door is already locked.")
                manual_door_override['lock_inside'] = False
                
            # Check if maximum unlock time is exceeded
            if magnets.get_inside_state() and (tm.time() - unlock_inside_tm > MAX_UNLOCK_TIME):
                logging.warning("[BACKEND] Maximum unlock time exceeded for inside door. Forcing lock.")
                magnets.queue_command("lock_inside")
                
            if magnets.get_outside_state() and (tm.time() - unlock_outside_tm > MAX_UNLOCK_TIME):
                logging.warning("[BACKEND] Maximum unlock time exceeded for outside door. Forcing lock.")
                magnets.queue_command("lock_outside")
                
        except Exception as e:
            logging.error(f"[BACKEND] Exception in backend occured: {e}")

    # RFID Cleanup on shutdown:
    rfid.stop_read(wait_for_stop=True)
    rfid.set_power(False)
    rfid.set_field(False)

    logging.info("[BACKEND] Stopped backend.")
    sigterm_monitor.signal_task_done()