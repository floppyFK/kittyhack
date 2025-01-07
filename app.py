from shiny import App
from src.ui import app_ui
from src.server import server
import os

path_www = os.path.join(os.path.dirname(__file__), "www")

app = App(app_ui, server, static_assets={"/": path_www})