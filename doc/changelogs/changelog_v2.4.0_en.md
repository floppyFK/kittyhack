# v2.4.0

## New Features
- **Entry and exit per cat** (BETA): Entry and exit can now be controlled separately for each cat.
  - Note for Home Assistant: Due to this enhancement, some entities have changed. Please use the updated dashboard configuration from the configuration tab.
- **Prey detection per cat**: Prey detection can now be enabled or disabled individually for each cat.

## Bug Fixes
- **MQTT**: Deprecated `object_id` values for Home Assistant have been removed.
- **Logging**: Log files were partially not written correctly; this behavior has been fixed.
- **Configuration file**: Invalid entries in `config.ini` no longer cause crashes.
- **Image view**: The classic (ungrouped) image view works again.
- **Inner lock:** After a cat went outside, the inside was incorrectly briefly unlocked and then locked again; this behavior has now been fixed.

## Minor Changes
- **Disable RFID reader**: In the configuration tab, the RFID reader can optionally be completely disabled (e.g., in case of hardware defects that would otherwise cause the cat flap to restart).
- **Backup & restore**: Both database and configuration backups can now be created and restored.

## Improvements
- **Database**: An adjusted database concept reduces the required free storage space by about 50%.
- **Faster startup**: The application's startup time has been significantly reduced, especially with large databases.
- **Configuration**: The configuration tab is clearer. Detailed descriptions are now grouped in collapsible elements.
- **Decision logic**: The logic for granting entry and exit can be displayed as a diagram in the configuration tab.
- **RFID validation**: In the "Manage cats / Add cats" tab, the entered RFID is checked for correct length and allowed characters.
- **Restart notice**: For changes that require a restart (e.g., selecting a different model), a clear notice is now displayed.
- **Database backup**: The backup concept has been fundamentally revised. Nightly backups are now significantly faster and cause less system load.