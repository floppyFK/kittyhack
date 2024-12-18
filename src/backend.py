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

TAG_TIMEOUT = 30.0               # after 30 seconds, a detected tag is considered invalid
RFID_READER_OFF_DELAY = 15.0     # Turn the RFID reader off 15 seconds after the last detected motion outside
OPEN_OUTSIDE_TIMEOUT = 5.0       # Keep the magnet to the outside open for 5 seconds after the last motion on the inside

def backend_main(database: str, simulate_kittyflap = False):

    tag_id = None
    tag_timestamp = 0.0
    motion_outside = 0
    motion_inside = 0
    last_motion_outside_tm = 0.0
    last_motion_inside_tm = 0.0

    # Initialize PIRs, Magnets and RFID
    pir = Pir(simulate_kittyflap=simulate_kittyflap)
    pir.init()
    rfid = Rfid(simulate_kittyflap=simulate_kittyflap)
    rfid.init()
    magnets = Magnets(simulate_kittyflap=simulate_kittyflap)
    magnets.init()
    
    logging.info("[BACKEND] Wait for the sensors to stabilize...")
    tm.sleep(5.0)

    # Initialize TfLite
    tflite = TfLite(modeldir = "/root/AIContainer/app/",
                   graph = "cv-lite-model.tflite",
                   labelfile = "labels.txt",
                   threshold = 0.5,
                   resolution = "800x600",
                   framerate = 10,
                   jpeg_quality = 75,
                   simulate_kittyflap = simulate_kittyflap)

    # Start RFID reader to clear the buffer
    rfid.set_power(True)
    rfid_thread = threading.Thread(target=rfid.run, args=(), daemon=True)
    rfid_thread.start()
    tm.sleep(1.0)
    rfid.stop_read(wait_for_stop=True)

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

    while True:
        last_outside = motion_outside
        last_inside = motion_inside
        motion_outside, motion_inside = pir.get_states()

        tag_id, tag_timestamp = rfid.get_tag()
        last_rfid_read = rfid.time_delta_to_last_read()

        if last_outside == 1 and motion_outside == 0: # Outside motion stopped
            last_motion_outside_tm = tm.time()
            logging.info(f"[BACKEND] Motion stopped OUTSIDE")
        
        if last_inside == 1 and motion_inside == 0: # Inside motion stopped
            last_motion_inside_tm = tm.time()
            logging.info(f"[BACKEND] Motion stopped INSIDE")
        
        if last_outside == 0 and motion_outside == 1: # Outside motion detected
            logging.info("[BACKEND] Motion detected OUTSIDE")
            tflite.resume()
        
        if last_inside == 0 and motion_inside == 1: # Inside motion detected
            logging.info("[BACKEND] Motion detected INSIDE")
            magnets.unlock_outside()

        if ( (motion_outside == 0) and 
             ((tm.time() - last_motion_outside_tm) > RFID_READER_OFF_DELAY) ):
            if rfid.get_run_state() == RfidRunState.running:
                # No motion outside, turn off RFID reader
                logging.info(f"[BACKEND] No motion outside since {RFID_READER_OFF_DELAY} seconds after the last motion. Stopping RFID reader.")
                rfid.stop_read(wait_for_stop=False)
            # Pause the TFLite model
            if tflite.get_run_state() == True:
                tflite.pause()
            
        if motion_outside == 1:
            # Motion detected outside, enable RFID and check for tag
            # Start the RFID thread with infinite read cycles, if it is not running
            if ( (last_rfid_read > TAG_TIMEOUT) and 
                 (rfid.get_run_state() == RfidRunState.stopped) ):
                rfid_thread = threading.Thread(target=rfid.run, args=(), daemon=True)
                rfid_thread.start()
            
        if ( (motion_inside == 0) and
             (magnets.get_outside_state() == True) and
             ((tm.time() - last_motion_inside_tm) > OPEN_OUTSIDE_TIMEOUT) ):
                # No motion inside, close the magnet to the outside
                magnets.lock_outside()
            
        if ( tag_id and 
             (last_rfid_read <= TAG_TIMEOUT) and 
             (rfid.get_run_state() == RfidRunState.running) ):
            logging.info(f"[BACKEND] Valid RFID tag detected: '{tag_id}'. Stopping RFID reader.")
            rfid.stop_read(wait_for_stop=False)

        if ( (tag_id is not None) and 
             (tm.time() > (tag_timestamp+TAG_TIMEOUT)) ):
            # Forgetting the tag after the timeout:
            rfid.set_tag(None, 0.0)
            logging.info("[BACKEND] Tag timeout reached. Forget the tag.")

        # TODO: Process the image buffer
        if image_buffer.size() > 0:
            # Process all elements in the buffer
            while image_buffer.size() > 0:
                element = image_buffer.pop()
                #cv2.imwrite(f"detected_{element.timestamp}_overlay.jpg", element.modified_image)
                logging.info(f"[BACKEND] Image buffer size: {image_buffer.size()} | New image with timestamp: {element.timestamp}, Mouse probability: {element.mouse_probability}, No mouse probability: {element.no_mouse_probability}")
            
        tm.sleep(0.1)