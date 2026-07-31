"""Shiny server entrypoint — boots the app and wires UI tab modules."""
from src.startup import run as run_startup

run_startup()

from src.server_ui import register_all


def server(input, output, session):
    """Shiny session entry: register all UI tab handlers."""
    register_all(input, output, session)
