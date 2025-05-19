# v2.0.1

## Bugfixes
- **Logging of events with RFID**: Events are now always saved when an RFID could be read - even if the `Minimum detection threshold` for object recognition has not been reached.
- **RFID Reader**: Incorrect RFIDs like `E54` are no longer read (The issue should have been fixed in v2.0.0, but another error had crept in)
- **Missing Button**: The missing `Changelogs` button in the Info tab is now available again