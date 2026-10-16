"""Session-scoped UI state shared across server_ui register_* modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from shiny import reactive

from src.baseconfig import CONFIG
from src.mode import is_remote_mode
import src.startup as startup


@dataclass
class SessionContext:
    """Per-browser-session state. Pass this object; use ``ctx.attr`` (do not unpack)."""

    reload_trigger_cats: Any
    reload_trigger_info: Any
    version_mismatch_warning_shown: bool
    last_uploaded_db_path: Any
    last_uploaded_cfg_path: Any
    remote_setup_active: Any
    remote_sync_status: Any
    remote_sync_modal_open: Any
    remote_restart_modal_open: Any
    remote_sync_finalized: Any
    remote_disconnect_modal_open: Any
    remote_connected_once: Any
    live_status: Any
    live_view_warning_html: Any
    live_view_warning_dismissed: Any
    live_view_warning_signature: Any
    _remote_synced_marker_path: Callable[[], str]
    _remote_restart_pending_marker_path: Callable[[], str]
    _remote_connection_state: Callable[[], tuple[str, bool, bool]]
    # Set by session_setup after its nested handlers exist
    show_user_notifications: Callable[[], None] | None = None
    live_view_image: Any = None


def create_session_context() -> SessionContext:
    """Build a fresh SessionContext with reactive defaults for one browser session."""

    def _remote_synced_marker_path() -> str:
        return (
            str(CONFIG.get("KITTYHACK_DATABASE_PATH", "kittyhack.db"))
            + ".remote_synced"
        )

    def _remote_restart_pending_marker_path() -> str:
        return (
            str(CONFIG.get("KITTYHACK_DATABASE_PATH", "kittyhack.db"))
            + ".remote_restart_pending"
        )

    def _remote_connection_state() -> tuple[str, bool, bool]:
        """Return (host, is_ready, manual_disconnect)."""
        host = (CONFIG.get("REMOTE_TARGET_HOST") or "").strip()
        if not host:
            return "", False, False

        is_ready = False
        manual_disconnect = False
        try:
            from src.remote.control_client import RemoteControlClient

            client = RemoteControlClient.instance()
            client.ensure_started()
            manual_disconnect = bool(
                getattr(client, "is_manual_disconnect", lambda: False)()
            )
            is_ready = bool(client.wait_until_ready(timeout=0))
        except Exception:
            is_ready = False
            manual_disconnect = False
        return host, is_ready, manual_disconnect

    return SessionContext(
        reload_trigger_cats=reactive.Value(0),
        reload_trigger_info=reactive.Value(0),
        version_mismatch_warning_shown=False,
        last_uploaded_db_path=reactive.Value(None),
        last_uploaded_cfg_path=reactive.Value(None),
        remote_setup_active=reactive.Value(
            bool(is_remote_mode() and startup.remote_setup_required)
        ),
        remote_sync_status=reactive.Value({}),
        remote_sync_modal_open=reactive.Value(False),
        remote_restart_modal_open=reactive.Value(False),
        remote_sync_finalized=reactive.Value(False),
        remote_disconnect_modal_open=reactive.Value(False),
        remote_connected_once=reactive.Value(False),
        live_status=reactive.Value(
            {
                "ok": False,
                "ts": "",
                "remote_waiting": False,
                "inside_lock": False,
                "outside_lock": False,
                "inside_motion": False,
                "outside_motion": False,
                "forced_lock_due_prey": False,
                "time_until_release": 0.0,
                "delta_to_last_prey_detection": 0.0,
            }
        ),
        live_view_warning_html=reactive.Value(""),
        live_view_warning_dismissed=reactive.Value(False),
        live_view_warning_signature=reactive.Value(""),
        _remote_synced_marker_path=_remote_synced_marker_path,
        _remote_restart_pending_marker_path=_remote_restart_pending_marker_path,
        _remote_connection_state=_remote_connection_state,
    )
