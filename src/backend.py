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
OPEN_OUTSIDE_TIMEOUT = 5.0       # Keep the magnet to the outside open for 5 seconds after the last motion on the inside

# Initialize TfLite
tflite = TfLite(modeldir = "/root/AIContainer/app/",
                graph = "cv-lite-model.tflite",
                labelfile = "labels.txt",
                resolution = "800x600",
                framerate = 10,
                jpeg_quality = 75,
                simulate_kittyflap = CONFIG['SIMULATE_KITTYFLAP'])

def backend_main(simulate_kittyflap = False):

    tag_id = None
    tag_id_valid = False
    tag_timestamp = 0.0
    motion_outside = 0
    motion_inside = 0
    motion_outside_tm = 0.0
    motion_inside_tm = 0.0
    last_motion_outside_tm = 0.0
    last_motion_inside_tm = 0.0
    motion_block_id = 0
    ids_with_mouse = []
    ids_of_current_motion_block = []
    known_rfid_tags = []

    # Register task in the sigterm_monitor object
    sigterm_monitor.register_task()

    # Initialize PIRs, Magnets and RFID
    pir = Pir(simulate_kittyflap=simulate_kittyflap)
    pir.init()
    rfid = Rfid(simulate_kittyflap=simulate_kittyflap)
    rfid.init()
    magnets = Magnets(simulate_kittyflap=simulate_kittyflap)
    magnets.init()
    
    logging.info("[BACKEND] Wait for the sensors to stabilize...")
    tm.sleep(5.0)

    # Start RFID reader to clear the buffer
    rfid.set_power(True)
    rfid_thread = threading.Thread(target=rfid.run, args=(), daemon=True)
    rfid_thread.start()
    tm.sleep(1.0)
    rfid.stop_read(wait_for_stop=True)

    # Start the magnet control thread
    magnets.start_magnet_control()

    # Start PIR monitoring thread
    pir_thread = threading.Thread(target=pir.read, args=(), daemon=True)
    pir_thread.start()

    # Start the camera
    logging.info("[BACKEND] Start the camera...")
    def run_camera():
        tflite.run()

    # Run the camera in a separate thread
    camera_thread = threading.Thread(target=run_camera, daemon=True)
    camera_thread.start()
    tflite.pause()

    def lazy_cat_workaround(motion_outside, last_outside, motion_outside_tm):
        # Lazy cat workaround: Keep the outside PIR active for 5 further seconds after the last detected motion outside
        if ( (motion_outside == 0) and 
             (last_outside == 1) and
             ((tm.time() - motion_outside_tm) < 5.0) ):
            motion_outside = 1
            logging.debug(f"[BACKEND] Lazy cat workaround: Keep the outside PIR active for {5.0-(tm.time()-motion_outside_tm):.1f} seconds.")
        return motion_outside

    while not sigterm_monitor.stop_now:
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

        tag_id, tag_timestamp = rfid.get_tag()
        last_rfid_read = rfid.time_delta_to_last_read()

        if last_outside == 1 and motion_outside == 0: # Outside motion stopped
            if tflite.get_run_state() == True:
                tflite.pause()
                tm.sleep(0.5)  # Wait for the last image to be processed
            last_motion_outside_tm = tm.time()
            logging.info(f"[BACKEND] Motion stopped OUTSIDE (Block ID: '{motion_block_id}')")
            if (magnets.get_inside_state() == True and magnets.check_queued("lock_inside") == False):
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
        if (motion_outside == 0 and magnets.get_inside_state() == True and magnets.check_queued("lock_inside") == False):
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
            else:
                logging.info("[BACKEND] No cats are allowed to exit. YOU SHALL NOT PASS!")

        # Turn off the RFID reader if no motion outside and pause the TFLite model
        if ( (motion_outside == 0) and 
             ((tm.time() - last_motion_outside_tm) > RFID_READER_OFF_DELAY) ):
            if rfid.get_run_state() == RfidRunState.running:
                logging.info(f"[BACKEND] No motion outside since {RFID_READER_OFF_DELAY} seconds after the last motion. Stopping RFID reader.")
                rfid.stop_read(wait_for_stop=False)
        
        if motion_outside == 1:
            # Motion detected outside, enable RFID and check for tag
            # Start the RFID thread with infinite read cycles, if it is not running
            if ( (last_rfid_read > TAG_TIMEOUT) and 
                 (rfid.get_run_state() == RfidRunState.stopped) ):
                rfid_thread = threading.Thread(target=rfid.run, args=(), daemon=True)
                rfid_thread.start()
        
        # Close the magnet to the outside after the timeout
        if ( (motion_inside == 0) and
             (magnets.get_outside_state() == True) and
             ((tm.time() - last_motion_inside_tm) > OPEN_OUTSIDE_TIMEOUT) and
             (magnets.check_queued("lock_outside") == False) ):
                magnets.queue_command("lock_outside")
            
        # Check for a valid RFID tag
        if ( (tag_id) and 
             (last_rfid_read <= TAG_TIMEOUT) and 
             (rfid.get_run_state() == RfidRunState.running) ):
            logging.info(f"[BACKEND] RFID tag detected: '{tag_id}'. Stopping RFID reader.")
            if CONFIG['ALLOWED_TO_ENTER'] == AllowedToEnter.KNOWN:
                if tag_id in known_rfid_tags:
                    tag_id_valid = True
                    logging.info("[BACKEND] Detected RFID tag is in the database. Kitty is allowed to enter...")
            elif CONFIG['ALLOWED_TO_ENTER'] == AllowedToEnter.ALL_RFIDS:
                tag_id_valid = True
                logging.info("[BACKEND] All RFID tags are allowed. Kitty is allowed to enter...")
            elif CONFIG['ALLOWED_TO_ENTER'] == AllowedToEnter.NONE:
                tag_id_valid = False
                logging.info("[BACKEND] No cats are allowed to enter. The door stays closed.")
            rfid.stop_read(wait_for_stop=False)

        # Forget the tag after the tag timeout and no motion outside:
        if ( (tag_id is not None) and 
             (tm.time() > (tag_timestamp+TAG_TIMEOUT)) and
             (motion_outside == 0) ):
            rfid.set_tag(None, 0.0)
            tag_id_valid = False
            logging.info("[BACKEND] Tag timeout reached. Forget the tag.")

        if image_buffer.size() > 0:
            # Process all elements in the buffer
            ids_of_current_motion_block = image_buffer.get_filtered_ids(min_timestamp=first_motion_outside_tm)
            ids_with_mouse = image_buffer.get_filtered_ids(min_timestamp=first_motion_outside_tm, min_mouse_probability=CONFIG['MOUSE_THRESHOLD'])
        else:
            ids_of_current_motion_block = []
            ids_with_mouse = []

        # Now collect the sensor data and decide if we unlock the inside
        unlock_inside =  (motion_outside == 1)
        unlock_inside &= (tag_id_valid or CONFIG['ALLOWED_TO_ENTER'] == AllowedToEnter.ALL)
        unlock_inside &= (magnets.get_inside_state() == False)
        unlock_inside &= ( (CONFIG['MOUSE_CHECK_ENABLED'] == False) or ((len(ids_with_mouse) == 0) and (len(ids_of_current_motion_block) >= CONFIG['MIN_PICTURES_TO_ANALYZE'])) )
        # If the outside magnet is already unlocked, we are not allowed to unlock the inside (only one magnet is allowed to be open at the same time to avoid a potential overload of the electronics)
        unlock_inside &= (magnets.get_outside_state() == False)
        unlock_inside &= (magnets.check_queued("unlock_inside") == False)

        if unlock_inside:
            logging.info(f"[BACKEND] All checks are passed. Unlock the inside")
            logging.debug(f"[BACKEND] Motion outside: {motion_outside}, Motion inside: {motion_inside}, Tag ID: {tag_id}, Tag valid: {tag_id_valid}, Motion block ID: {motion_block_id}, Images with mouse: {len(ids_with_mouse)}, Images in current block: {len(ids_of_current_motion_block)} ({ids_of_current_motion_block})")
            magnets.queue_command("unlock_inside")
            

    logging.info("[BACKEND] Stopped backend.")
    sigterm_monitor.signal_task_done()