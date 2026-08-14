"""Timing constants for the backend control loop."""
from src.baseconfig import CONFIG

TAG_TIMEOUT = 30.0               # after 30 seconds, a detected tag is considered invalid
RFID_READER_OFF_DELAY = 15.0     # Turn the RFID reader off 15 seconds after the last detected motion outside
OPEN_OUTSIDE_TIMEOUT = 6.0 + CONFIG['PIR_INSIDE_THRESHOLD'] # Keep the magnet to the outside open for 6 + PIR_INSIDE_THRESHOLD seconds after the last motion on the inside
MAX_UNLOCK_TIME = 60.0           # Maximum time the door is allowed to stay open
LAZY_CAT_DELAY_PIR_MOTION = 6.0  # Keep the PIR active for an additional 6 seconds after the last detected motion when using PIR-based motion detection
LAZY_CAT_DELAY_CAM_MOTION = 12.0 # Keep the PIR active for an additional 12 seconds after the last detected motion when using camera-based motion detection
FAST_EXIT_POST_CAPTURE_SECONDS = 6.0  # Extra recording after fast-lock exit crossing before finalizing the event
EVENT_COOLDOWN_SECONDS = 3.0     # After an event is finalized, ignore all new motion triggers for this long (PIR settling)
MAX_MOTION_BLOCK_SECONDS = 90.0  # Finalize a motion block after this time even if outside motion never falls cleanly
INSIDE_UNLOCK_CONFIRM_QUIET_SECONDS = 15.0  # After max-unlock hold, resume camera-only unlocks once own-cat detections have been absent this long
INSIDE_UNLOCK_SAFETY_COOLDOWN_SECONDS = 60.0  # After a second max-unlock without passage, block camera/PIR auto-unlock this long
