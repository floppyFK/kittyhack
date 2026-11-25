# v3.0.2

## Bugfixes

- **Camera after a network outage**: In remote mode, a dropped connection could leave the Kittyflap no longer listening on port 80. The remote computer then received no camera stream and did not unlock the flap inwards until `kittyhack_control` was restarted. The info page now closes stuck video connections and starts listening again on its own.
- **Page after a reboot or update**: An open browser tab reconnected and kept the content of the old page, so the *Info* tab could still show the previous version. The page now reloads once when the Kittyflap has been rebooted.
- **Settings on the first remote sync**: On the first sync, events and pictures arrived, but the settings stayed at their defaults. The database is now transferred completely, and a leftover `config.ini` no longer overwrites them.
- **Detections on black frames**: When the light goes off, the camera delivers a few almost black frames while it switches to night vision. The model could report a cat on those empty frames. That created a *cat at the flap* event with blank pictures and could influence the entry decision. Those frames are now ignored.
