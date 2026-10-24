# v3.0.0

**Important:** This release requires at least **v2.7.0**. If you are still on an older version, please update using the install script (the same one as for the initial setup - see [Run the setup script](https://github.com/floppyFK/kittyhack#2-run-the-setup-script)) instead of the in-app update.

## New Features

- **Statistics**: New *Statistics* tab with entries, exits, prey detections, time inside/outside and typical activity - also per cat and for different time ranges.
- **Indoors/outdoors state**: The *Live view* shows where each cat is and for how long. Tap the state to change it manually. A cat can be hidden from this list under *Cats*.
- **Motion sensor confirmation for camera entry**: Optional setting under *Settings*. When the camera is used to detect cats, the flap opens only if the outside motion sensor also triggers. Useful if sunlight or reflections sometimes cause a false detection.
- **Hide warnings**: Some warnings can be hidden with *Do not notify again*. You can restore them later under *Settings*.

## Improvements

- **Redesigned interface**: The web interface has been redesigned. The wording is clearer, cards and buttons are easier to follow, and both the light and dark appearance are easier to read.
- **Simpler menu**: Adding a new cat is now on the *Cats* page. WLAN settings are on the *System* tab. The *Configuration* tab is now called *Settings*.
- **Pictures**: The *Pictures* tab has been rebuilt. Filters sit clearly above the pictures, more pictures appear as you scroll, and *Go to today* takes you back to the current day.
- **Events**: The timeline of an event shows when the flap unlocks or locks. Stretches without a picture are easier to see. Deleting an event asks for confirmation.
- **Decision steps**: Under *Settings*, Kittyhack shows the entry and exit steps as a clear sequence that follows the option you selected.
- **Lost connection**: If the connection drops, a clear notice appears. *Try now* reconnects right away.
- **Note about the included model**: While the original model that came with the Kittyflap is still in use, Kittyhack reminds you about it. It barely recognizes your own cats. A model you train yourself is much more accurate.
- **Mobile optimization**: The Web UI stays reliably connected on smartphones even if the browser is briefly sent to the background.
- **Faster detection**: Cat and prey detection on the Kittyflap is now a bit faster.
- **Model training**: Kittyhack now signs in to the model training server. Older 2.x versions keep working for a short transition period. After that, the update to v3 is required to train models.
- **Remote mode**: When Kittyhack runs on a separate computer, less is installed. Detection on the processor or a graphics card still works. When you switch, Kittyhack installs the required additional packages in the background.
- **Backup and restore**: Settings are no longer saved in a separate config.ini. Everything is stored in the Kittyhack database, which you can back up and restore under *Info*. A config.ini from an older version can still be restored there.

## Bugfixes

- **Average FPS**: The display of the average FPS per model in the model management interface was no longer being updated. This has now been fixed.
- **Remote update from a custom branch**: Updating the Kittyflap from a remote device now uses the update repository configured on the remote.
- **Update dialog on a remote device**: While an update is running, the "remote disconnected" notice no longer covers the update and reboot dialog.
- **Training file too large**: If a file for model training is too large, Kittyhack shows a clear message instead of an unclear error.
