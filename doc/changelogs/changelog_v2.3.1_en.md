# v2.3.1

> ## 🚨⚠️🚨⚠️🚨
> ## ATTENTION, IMPORTANT UPDATE! PLEASE INSTALL IMMEDIATELY!
> ## 🚨⚠️🚨⚠️🚨

## New Features
- **Quick access to input and output control**: The configuration for input and output direction is now available directly on the home page. Switching to the configuration tab is no longer necessary.

## Bugfixes
- **Manual locking/unlocking**: 
  - 🔴 If the inner lock was manually unlocked in the Live View tab while the outer side was already unlocked, this could lead to a complete system crash - **potentially even causing hardware damage, resulting in permanent crashes during unlocking!** 🔴 - This has now been fixed.
  - If all conditions for unlocking the inner side were met, manual locking could not reliably lock the flap (emergency locking). This has now been fixed.
- **Event window**: An event can now also be closed by clicking outside the event window.
- **Threshold for cat detection**: The minimum value for this setting is now linked to the value *Minimum detection threshold*, since lower values would not make sense here.
- **Live View**: The outer motion status in the Live View tab now correctly displays the state of camera detection when this is configured as the source for outside motion detection (`Use camera for motion detection`). Previously, the PIR motion detector status was always incorrectly displayed.
- **Shutdown/restart**: When shutting down or restarting, any open locks are now properly closed beforehand.

## Improvements
- **Watchdog for Wi-Fi connection**: If the Wi-Fi connection is lost, the system now automatically attempts to restore the connection. After several unsuccessful attempts, the Kittyflap will automatically restart (enabled by default, can be disabled in the advanced settings).
- **IP camera watchdog**: The connection to the IP camera is now automatically re-established as soon as faulty or damaged h264 streams are detected (enabled by default, can be disabled in the advanced settings).