# v3.0.0

**Important:** This release requires at least **v2.7.0**. If you are still on an older version, please update using the install script (the same one as for the initial setup - see [Run the setup script](https://github.com/floppyFK/kittyhack#2-run-the-setup-script)) instead of the in-app update.

## New Features

- **Statistics**: New *Statistics* tab with entries, exits, prey detections, time inside/outside and typical activity - also per cat and for different time ranges.
- **Motion sensor confirmation for camera entry**: Optional setting under *Configuration*. When the camera is used to detect cats, the flap opens only if the outside motion sensor also triggers. Useful if sunlight or reflections sometimes cause a false detection.
- **Hide warnings**: Some warnings can be hidden with *Do not notify again*. You can restore them later under *Configuration*.

## Improvements

- **Simpler menu**: Adding a new cat is now on the *Manage cats* page. WLAN settings are on the *System* tab.
- **Mobile optimization**: The Web UI stays reliably connected on smartphones even if the browser is briefly sent to the background.
- **Faster detection**: Cat and prey detection on the Kittyflap is now a bit faster.
- **Model training API**: Kittyhack now authenticates to the model training server. Older v2.x versions keep working for a short transition period; after that, the update to v3 is mandatory to train models.

## Bugfixes

- **Average FPS**: The display of the average FPS per model in the model management interface was no longer being updated. This has now been fixed.