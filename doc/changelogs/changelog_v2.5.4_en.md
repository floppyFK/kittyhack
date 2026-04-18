# v2.5.4

This release adds the ability to point update checks and installs at a custom fork or feature branch — useful for testing pull requests on a real Kittyflap without waiting for an official release.

## New Features
- **Configurable update repository**: A new option "Update repository" was added under *Configuration → General settings*. It lets you switch between the default `floppyFK/kittyhack` releases and a custom source. The custom field accepts either `owner/repo` (uses the latest release tag from that fork) or `owner/repo@branch-or-tag` (tracks the HEAD of the given branch/tag). When a branch is selected, the latest version indicator shows `<branch>@<short-sha>` so the update notification still works as expected.
