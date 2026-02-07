from shiny import App
from src.ui import app_ui
from src.server import server
import os

path_www = os.path.join(os.path.dirname(__file__), "www")

# Serve event thumbnails/originals directly from disk to reduce server RAM/CPU load
# (no base64 embedding for the event modal).
path_thumbs = "/root/pictures/thumbnails"
path_originals = "/root/pictures/original_images"

app = App(
	app_ui,
	server,
	static_assets={
		"/thumb": path_thumbs,
		"/orig": path_originals,
		"/": path_www,
	},
)