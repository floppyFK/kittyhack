# v2.5.5

Hotfix release for two issues that could occur after a restart or when using PIR-based motion detection.

## Bugfixes
- **Entry direction temporarily blocked after restart**: After a restart, the entry direction was blocked for the duration configured by the *Lock duration after prey detection* setting, even though no prey had been detected yet in the current runtime.
- **High CPU load with PIR-based motion detection**: When the option *Use camera for motion detection* was disabled (i.e., PIR mode), this could cause high CPU load and continuous log entries.
