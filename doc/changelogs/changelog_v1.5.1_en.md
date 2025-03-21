# v1.5.1

## Improvements
- **Images for Event View are now cached**: The images displayed in the Event View are a smaller version of the images stored in the database. These were previously generated on-the-fly as soon as the magnifying glass button was clicked. For events with many images, this could lead to significant waiting times. These smaller versions are now created directly when new images are added.
> Note: For images in the database that were added with v1.5.0 or earlier, these preview variants are still missing. 
> They will be created gradually in the background, so immediately after the update to v1.5.1, it may still take a little longer once when you want to view the images of an event.
- **Fallback to object recognition on a single CPU core**: If there are restarts or system freezes when object recognition starts (triggered by one of the motion detectors), you can now try switching the calculation to a single CPU core instead of all cores in the configuration panel (restart required). More details in Issue #72.

## Bugfixes
- **Limited number of images per event**: The maximum number of images per event is now limited. The limit can be adjusted in the configuration panel (separately for motion with recognized RFID and for motion without recognized RFID).
- **Internal limit for image cache**: The maximum number of internally buffered images is now limited to prevent memory overflow.
- **Lock time after prey recognition**: The value for the lock time after prey recognition is now correctly saved in the configuration panel.
- **Favicon on Android and iOS**: If you add a shortcut to the Kittyhack site to your smartphone's home screen, the correct favicon will now be displayed.

---------

## Known Issues:
- **Display on iOS**: The display of the event overview on iOS devices is broken and the buttons to display the events do not work.