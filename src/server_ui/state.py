"""Shared module-level reactive triggers and update-progress state for the UI."""

import logging
import os
import threading
import time as tm

from shiny import reactive

from src.paths import kittyhack_root

reload_trigger_wlan = reactive.Value(0)
reload_trigger_photos = reactive.Value(0)
reload_trigger_ai = reactive.Value(0)
reload_trigger_config = reactive.Value(0)
reload_trigger_api_tokens = reactive.Value(0)

ls_project_choices = reactive.Value({})

live_view_refresh_nonce = reactive.Value(0)
live_view_aspect = reactive.Value((4, 3))

update_progress_state = {
    "in_progress": False,
    "step": 0,
    "max_steps": 8,
    "message": "",
    "detail": "",
    "result": None,
    "error_msg": "",
}
update_progress_lock = threading.Lock()


def set_update_progress(**kwargs):
    """Thread-safe update of keys in ``update_progress_state``."""
    with update_progress_lock:
        update_progress_state.update(kwargs)


def get_update_progress():
    """Return a copy of the current update-progress snapshot."""
    with update_progress_lock:
        return update_progress_state.copy()


user_wlan_action_in_progress = False


def _wlan_action_marker_path() -> str:
    """Filesystem path for the cross-service WLAN-action-in-progress marker."""
    return os.path.join(kittyhack_root(), ".wlan-action-in-progress")


def _set_wlan_action_in_progress(active: bool) -> None:
    """Set/clear a cross-service marker for intentional WLAN reconfiguration."""
    global user_wlan_action_in_progress
    user_wlan_action_in_progress = bool(active)

    marker = _wlan_action_marker_path()
    if active:
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(str(tm.time()))
        except Exception as e:
            logging.warning(
                f"[WLAN ACTION] Failed to create marker file '{marker}': {e}"
            )
    else:
        try:
            if os.path.exists(marker):
                os.remove(marker)
        except Exception as e:
            logging.warning(
                f"[WLAN ACTION] Failed to remove marker file '{marker}': {e}"
            )
