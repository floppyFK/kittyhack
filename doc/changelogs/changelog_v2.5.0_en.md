# v2.5.0

This release adds theme support (light/dark/auto), the ability to offload CPU-intensive tasks to a separate PC, a significantly revamped event view (scrubber/markers/downloads), as well as many performance optimizations and bug fixes.

## New Features
- **Theme support (light/dark/auto)**: You can now switch between light/dark/auto in the WebGUI.
- **Remote control mode**: Run Kittyhack on a more powerful remote PC for faster inference and cat/prey detection in real time, while the Kittyflap hardware stays connected for sensors/locks. Full setup & details: https://github.com/floppyFK/kittyhack/blob/main/doc/remote-mode.md
- **Reworked Event View**:
  - **Scrubber/timeline** in the event modal for quick navigation inside an event.
  - **Marker overlay** on the timeline for frames with detected labels (prey in red, cat in green).
  - **Single picture download**: When playback is paused, the currently shown picture can be downloaded directly.
- **Reworked Pictures section**: Replaced the previous tab UI with a navigation bar (if not grouped to events).
- **IP Camera Stream Downscaling**: Streams from high-resolution IP cameras can now be downscaled directly in the IP camera settings.

## Improvements
- **Performance & RAM**: High-RAM tasks were pushed from kittyflap towards the web client to reduce server memory usage.
- **UI/UX**: Many small UI tweaks (styling, icons, tooltips, dark-theme details).
- **Configuration (unlock decision timing)**: The "unlock decision delay" is now configured in **seconds** after a motion trigger (instead of a number of pictures).
