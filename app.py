from shiny import App
from src.ui import app_ui
from src.server import server

app = App(app_ui, server)