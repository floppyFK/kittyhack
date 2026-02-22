from shiny import App
from src.ui import app_ui
from src.server import server
import os
from src.paths import pictures_thumbnails_dir, pictures_original_dir

path_www = os.path.join(os.path.dirname(__file__), "www")
path_doc_diagrams = os.path.join(os.path.dirname(__file__), "doc", "diagrams")

# Serve event thumbnails/originals directly from disk to reduce server RAM/CPU load
# (no base64 embedding for the event modal).
path_thumbs = pictures_thumbnails_dir()
path_originals = pictures_original_dir()

app = App(
	app_ui,
	server,
	static_assets={
		"/thumb": path_thumbs,
		"/orig": path_originals,
		"/diagrams": path_doc_diagrams,
		"/": path_www,
	},
)