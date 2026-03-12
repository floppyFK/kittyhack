# v2.5.3

This release expands the AI training workflow with Label Studio integration, adds bookmarkable tab URLs, and refines the pictures and event views.

## New Features
- **Label Studio project configuration**: An API token and a Label Studio project can now be configured directly in the WebGUI.
- **Training directly from a Label Studio project**: A configured Label Studio project can now be exported and submitted for model training directly from Kittyhack, without manually downloading a ZIP file first.
- **Send pictures to Label Studio**: The currently visible picture in an event or in the Pictures view can now be sent directly to the configured Label Studio project.
- **Bookmarkable tab URLs**: Main sections like Live View, Pictures, AI Training, System, or Configuration now have dedicated URL paths, so reloads, bookmarks, and shared links open the same tab again.

## Improvements
- **Event scrubber**: Switching between pictures in the event view now works noticeably faster.
- **Pictures section**: The non-grouped pictures view was reworked into a modern gallery with pager and quick actions for downloading, deleting, or sending pictures to Label Studio.

## Bugfixes
- **Mobile slider feedback**: Input sliders are now highlighted reliably after changes on mobile devices as well.
- **Tab restore after reload/reconnect**: The active tab is now restored more reliably after page reloads and reconnects.