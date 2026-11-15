"""Shiny UI tab modules — wired by register_all from the thin server entrypoint."""

from src.server_ui.context import create_session_context
from src.server_ui.session_setup import register_session_setup
from src.server_ui.photos import register_photos
from src.server_ui.statistics import register_statistics
from src.server_ui.live_view import register_live_view
from src.server_ui.system_tab import register_system_tab
from src.server_ui.cats import register_cats
from src.server_ui.ai_training import register_ai_training
from src.server_ui.configuration import register_configuration
from src.server_ui.wlan import register_wlan
from src.server_ui.info import register_info


def register_all(input, output, session):
    """Create session-local state and register all UI tab handlers."""
    ctx = create_session_context()

    register_session_setup(input, output, session, ctx)
    register_photos(input, output, session, ctx)
    register_statistics(input, output, session, ctx)
    register_live_view(input, output, session, ctx)
    register_system_tab(input, output, session, ctx)
    register_cats(input, output, session, ctx)
    register_ai_training(input, output, session, ctx)
    register_configuration(input, output, session, ctx)
    register_wlan(input, output, session, ctx)
    register_info(input, output, session, ctx)
