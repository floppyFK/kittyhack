# v2.3.1

## New Features
- **Quick Access to Input and Output Control**: The configuration of input and output direction is now available directly on the live-view tab. Switching to the configuration tab is no longer necessary.

## Bugfixes
- **Watchdog for WiFi Connection**: If the WiFi connection is lost, the system now automatically attempts to restore the connection. After several unsuccessful attempts, the Kittyflap will automatically restart (enabled by default, but can be disabled in the advanced settings).
- **Event Window**: An event can now also be closed by clicking outside the event window.

## Improvements
- **IP Camera Watchdog**: The connection to the IP camera is now automatically re-established as soon as faulty or corrupted h264 streams are detected (enabled by default, but can be disabled in the advanced settings).
