# v2.3.0

## New Features
- **Home Assistant Integration**: The cat flap can now be integrated into Home Assistant via MQTT (an MQTT broker is required)!
  - Supported entities:
    - Motion sensor (inside/outside)
    - Lock state (inside/outside)
    - Lock control (inside)
    - Last event
    - Detected prey
    - Control of exit direction
    - Control of entry direction

## Minor Changes
- **Configuration Download**: In the `Info` tab, you can now download the current configuration in addition to the database.

## Improvements
- **Pictures Tab**: The settings "Show only detected cats" and "Show only detected mice" are now saved permanently.
- **Model Training**: If a model training fails, an error message is now displayed in the web interface.
- **Sensitive Data**: Sensitive information such as passwords is now masked in the log files.