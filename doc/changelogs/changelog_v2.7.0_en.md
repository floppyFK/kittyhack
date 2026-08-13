# v2.7.0

## New Features

- **Beta updates**: In *Configuration* you can now choose **Beta** as the update source to receive beta releases early.

## Improvements

- **MQTT reliability**: The MQTT connection recovers more reliably after interruptions (e.g. Home Assistant integration).
- **Update source changes**: Changing the update repository (Standard / Beta / Custom) no longer starts an update automatically. Kittyhack checks the new source and you can start the update yourself on the *Info* tab when you are ready.
- **Database in log download**: The log file download on the *Info* tab now also includes a snapshot of the Kittyhack database.

## Bugfixes

- **Immediate locking after passage**: With *Immediate locking after passage* enabled, the flap no longer reopens incorrectly for the same cat that just went outside.
- **Safer configuration saving**: Settings are not lost anymore if a reboot is triggered while a configuration change is being saved.