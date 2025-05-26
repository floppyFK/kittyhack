# v2.1.0

## New Features
- **PWA Support**: The Kittyflap user interface can now be installed as a Progressive Web App on mobile devices and desktops. This gives you faster access to the cat flap without having to open the browser (This feature requires an HTTPS connection)  
  > ℹ️ More information can be found in the **`Info`** tab.
- **Hostname Configuration**: The hostname of the Kittyflap can now be customized through the settings. This enables easier access to the cat flap via a custom name in the local network (e.g., `http://kittyflap.local`).
- **Motion Detection via Camera**: As an alternative to the PIR sensor on the outside, the camera image can now be used to detect motion. This significantly reduces false triggers caused by moving trees or people in the image. However, a well-trained, custom detection model is required.  
  > ℹ️ You can find this option under **`Configuration`** -> **`Use camera for motion detection`**.

## Improvements
- **Additional Event Info**: The event list now displays additional icons for manual unlocking/locking during an event.
- **Configuration tab**: Changed input fields in the configuration tab are now visually highlighted before saving. This makes input errors (e.g., when scrolling on smartphones) easier to spot.

## Bugfixes
- **RFID-Reader**: The reading behavior of the RFID reader has been further improved