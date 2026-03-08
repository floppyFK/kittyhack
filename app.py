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

# ---------------------------------------------------------------------------
# Tab routing middleware
# ---------------------------------------------------------------------------
# Each UI tab has a dedicated URL path (e.g. /pictures/, /system/).  The Shiny
# SPA always runs at "/", so this ASGI middleware transparently rewrites any
# request whose first path segment is a known tab slug back to "/" (or strips
# the prefix for sub-resources like /pictures/shared/shiny.js -> /shared/shiny.js).
# This works for both HTTP and WebSocket upgrade requests.
# ---------------------------------------------------------------------------

TAB_PATHS = frozenset({
    "live-view", "pictures", "manage-cats", "add-new-cat",
    "ai-training", "system", "configuration", "wlan-configuration", "info",
})


class TabRoutingMiddleware:
    """ASGI middleware: strip known tab-slug prefix so Shiny always sees '/'."""

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "/")
            # Split off the first segment: "/pictures/foo" -> ("pictures", "foo")
            stripped = path.lstrip("/")
            first_seg, _, remainder = stripped.partition("/")
            if first_seg in TAB_PATHS:
                new_path = "/" + remainder  # e.g. "/foo" or just "/"
                scope = dict(scope, path=new_path)
                if "raw_path" in scope:
                    scope["raw_path"] = new_path.encode("latin-1")
        await self.app(scope, receive, send)


shiny_app = App(
	app_ui,
	server,
	static_assets={
		"/thumb": path_thumbs,
		"/orig": path_originals,
		"/diagrams": path_doc_diagrams,
		"/": path_www,
	},
)

app = TabRoutingMiddleware(shiny_app)