#!/usr/bin/env python3
"""Minimal HTTP status page while ensure_venv.sh finishes a post-reboot update.

Uses only the stdlib so it can run with system python3 (not the Kittyhack venv).
User-facing text is intentionally non-technical.
"""
from __future__ import annotations

import argparse
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


TEXTS = {
    "en": {
        "title": "Update in progress",
        "heading": "Update in progress…",
        "body": (
            "Kittyhack is finishing the update. This can take several minutes."
        ),
        "warn": "Please do not switch off or reboot the Kittyflap.",
        "hint": "This page refreshes automatically. The normal interface will appear when the update is done.",
    },
    "de": {
        "title": "Aktualisierung läuft",
        "heading": "Aktualisierung läuft…",
        "body": (
            "Kittyhack schließt die Aktualisierung ab. Das kann einige Minuten dauern."
        ),
        "warn": "Bitte schalte die Kittyflap nicht aus und starte sie nicht neu.",
        "hint": "Diese Seite wird automatisch aktualisiert. Die normale Oberfläche erscheint, sobald die Aktualisierung fertig ist.",
    },
}


def _read_language(root: Path) -> str:
    for name in ("config.ini", "config.remote.ini"):
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = re.search(
            r"(?im)^\s*language\s*=\s*([a-zA-Z_]+)\s*$",
            text,
        )
        if match:
            lang = match.group(1).strip().lower()
            if lang.startswith("de"):
                return "de"
            if lang.startswith("en"):
                return "en"
    return "en"


def _html(lang: str) -> bytes:
    t = TEXTS.get(lang) or TEXTS["en"]
    # Keep markup minimal and readable; mirror the simple boot-wait look.
    page = f"""<!doctype html>
<html lang="{lang}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="15">
  <title>{t['title']}</title>
  <meta name="color-scheme" content="light dark">
  <style>
    :root {{
      --bg: #f6f7fb; --bg2: #eef2ff; --card: #ffffff; --text: #0b1220;
      --muted: #5b6475; --border: rgba(15,23,42,.14);
      --shadow: 0 12px 30px rgba(15,23,42,.10); --accent: #2563eb;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0b1020; --bg2: #111a33; --card: #0f172a; --text: #e5e7eb;
        --muted: #9aa3b2; --border: rgba(226,232,240,.14);
        --shadow: 0 18px 40px rgba(0,0,0,.35); --accent: #60a5fa;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, sans-serif;
      line-height: 1.55;
      background: radial-gradient(1200px 700px at 15% 0%, var(--bg2), var(--bg));
      background-repeat: no-repeat; background-size: cover; background-attachment: fixed;
      color: var(--text); min-height: 100svh;
    }}
    .wrap {{ max-width: 640px; margin: 0 auto; padding: 48px 18px; }}
    .card {{
      background: var(--card); border: 1px solid var(--border); border-radius: 18px;
      box-shadow: var(--shadow); padding: 28px 24px;
    }}
    h1 {{ font-size: 1.55rem; letter-spacing: -0.02em; margin: 0 0 14px; }}
    p {{ margin: 0.55rem 0; }}
    .warn {{ font-weight: 700; color: var(--text); }}
    .muted {{ color: var(--muted); font-size: 0.95rem; }}
    .dot {{
      display: inline-block; width: 0.55rem; height: 0.55rem; border-radius: 999px;
      background: var(--accent); margin-right: 0.45rem; vertical-align: middle;
      animation: pulse 1.2s ease-in-out infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 0.35; transform: scale(0.9); }}
      50% {{ opacity: 1; transform: scale(1); }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1><span class="dot" aria-hidden="true"></span>{t['heading']}</h1>
      <p>{t['body']}</p>
      <p class="warn">{t['warn']}</p>
      <p class="muted">{t['hint']}</p>
    </div>
  </div>
  <script>
    setInterval(function () {{
      try {{ window.location.reload(); }} catch (e) {{}}
    }}, 15000);
  </script>
</body>
</html>
"""
    return page.encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    html_bytes: bytes = b""

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # Keep journal noise low.
        return

    def do_GET(self) -> None:  # noqa: N802
        body = self.html_bytes
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(self.html_bytes)))
        self.end_headers()


def main() -> int:
    parser = argparse.ArgumentParser(description="Kittyhack post-reboot update status page")
    parser.add_argument("--root", required=True, help="Kittyhack install root")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--lang", default="", help="en|de (optional; otherwise read from config.ini)")
    args = parser.parse_args()

    root = Path(args.root)
    lang = (args.lang or "").strip().lower()
    if lang not in TEXTS:
        lang = _read_language(root)

    _Handler.html_bytes = _html(lang)

    try:
        server = ThreadingHTTPServer((args.bind, args.port), _Handler)
    except OSError as exc:
        print(f"[update_status_server] bind failed on {args.bind}:{args.port}: {exc}", file=sys.stderr)
        return 1

    print(f"[update_status_server] serving update status on {args.bind}:{args.port} (lang={lang})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
