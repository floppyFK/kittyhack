# v2.5.5

Hotfix for a lockout that could occur in the first ~30 minutes after any service restart (including the one triggered by the v2.5.4 update itself).

## Bugfixes
- **Cat could not enter for ~30 minutes after a restart**: The "no prey detected within timeout" gate of the inside-unlock logic used a naive time comparison that treated the uninitialised `prey_detection_mono` value (`0.0`) as "prey was just detected". Because the monotonic clock starts small right after boot, the resulting age was smaller than `LOCK_DURATION_AFTER_PREY_DETECTION` (default 1800 s), so the gate stayed closed for the first 30 minutes of uptime — even though no prey had ever been detected in that session. After a restart, RFID detection, motion detection and mouse-check could all succeed while the door still refused to unlock. The fix short-circuits the comparison when `prey_detection_mono` has never been set, mirroring the guard that already existed in the prey-state publisher.
