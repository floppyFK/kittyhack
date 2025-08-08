# v2.3.1

## New Features
- **Quick Access to Input and Output Control**: The configuration of input and output direction is now available directly on the live-view tab. Switching to the configuration tab is no longer necessary.

## Bugfixes
- **Event Window**: An event can now also be closed by clicking outside the event window.
- **Threshold for cat detection**: The minimum value for this setting is now coupled to the value of *Minimum detection threshold*, since lower values would not make sense here.
- **Live View**: The outside-motion status in the Live View tab now correctly displays the camera detection state when configured as the source for external motion detection (`Use camera for motion detection`). Previously, the PIR motion sensor status was incorrectly always displayed.

## Improvements
- **Watchdog for WiFi Connection**: If the WiFi connection is lost, the system now automatically attempts to restore the connection. After several unsuccessful attempts, the Kittyflap will automatically restart (enabled by default, but can be disabled in the advanced settings).
- **IP Camera Watchdog**: The connection to the IP camera is now automatically re-established as soon as faulty or corrupted h264 streams are detected (enabled by default, but can be disabled in the advanced settings).
