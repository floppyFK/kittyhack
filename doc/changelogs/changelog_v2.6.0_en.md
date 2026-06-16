# v2.6.0

## Highlights

- (Remote mode only) Hardware acceleration for IP camera streams
- (Remote mode only) Experimental hardware support for inference on compatible devices
- Improved WLAN stability and automatic recovery mechanisms
- Better RFID and video detection logic

---------------

## New Features

- **Hardware decoding for IP cameras**: Added experimental option to use hardware acceleration for H264/H265 video streams from IP cameras in remote mode (not available, if kittyhack runs directly on the kittyflap!)
- **Hardware inference support**: Experimental feature to utilize hardware acceleration for object detection inference, available on supported platforms (not available, if kittyhack runs directly on the kittyflap!).
- **GitHub PR reference shorthand**: Custom update repository field now accepts GitHub pull request head references in the format `owner:branch` (e.g., `FabulousGee:feat/xyz`), making it easier to test contributor branches directly from PR pages.
- **Custom repository validation**: The configuration system now verifies that custom update repositories exist on GitHub before saving, preventing silent failures during auto-update.

## Improvements

- **IP camera stability**: Enhanced frame capture timing and buffer management for H264/H265 streams to improve decoding stability, especially with GPU inference.
- **FPS limits and buffering**: Added configurable FPS limits and adaptive buffering for IP camera streams to prevent resource exhaustion.
- **Event timeline**: Improved visualization of the details for each event (only available for new events after this update).

## Bugfixes

- **RFID detection priority**: In the entry modes "Only registered cats" and "Individual configuration per cat", detected RFID tags now always take precedence over a (potentially incorrect) video-based identification (#171).
- **WLAN watchdog**: Added an extra layer of protection to the Wi-Fi watchdog. If the retry mechanism fails to restore a lost WLAN connection, the device will automatically reboot after 120 seconds at the latest.

## Minor Changes

- **Translation workflow**: Updated German translation generation process and documentation.
- **WLAN settings**: Improved WLAN stability by re-applying runtime settings (txpower, power_save) after reconnection.