# v2.5.0

This release adds theme support (light/dark/auto), a major rework of the event view (scrubber/markers/downloads), plus many performance improvements and small bugfixes.

## New Features
- **Theme support (light/dark/auto)**: You can now switch between light/dark/auto in the WebGUI.
- **Reworked Event View**:
  - **Scrubber/timeline** in the event modal for quick navigation inside an event.
  - **Marker overlay** on the timeline for frames with detected labels (e.g. prey in red, otherwise green).
  - **Single picture download**: When playback is paused, the currently shown picture can be downloaded directly.
- **Reworked Pictures section**: Replaced the previous tab UI with a navigation bar (if not grouped to events).

## Improvements
- **Performance & RAM**: High-RAM tasks were pushed from kittyflap towards the web client to reduce server memory usage.
- **UI/UX**: Many small UI tweaks (styling, icons, tooltips, dark-theme details).
