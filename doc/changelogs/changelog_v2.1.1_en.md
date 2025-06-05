# v2.1.1

## Bugfixes
- **New installation**: After a new installation, crashes could occur if no database or configuration was present.
- **(Mobile) Navigation bar**: On smartphones, the navigation bar now automatically collapses after switching to another tab in the web interface.
- **PWA**: When using the web interface as a PWA (Progressive Web App), the app now automatically tries to restore the connection if it is interrupted. (This change requires clearing the browser cache once for it to take effect.)

## Minor changes
- **Maximum lock duration increased**: The maximum configurable lock duration after prey detection has been increased from 10 to 30 minutes.