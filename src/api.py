"""Kittyhack REST API.

Exposes a small JSON API under `/api/v1/*` for remote control and integration
with home-automation tooling (shell scripts, curl, iOS shortcuts, webhooks …).

Authentication: all endpoints require a Bearer token in the `Authorization`
header (or `X-API-Key` as a fallback). Tokens are managed with
`tools/api_token.py` — the clear-text token is only shown once at creation;
the token store persists only a SHA-256 hash.

Both GET and POST are accepted for mutating endpoints (open/close/mode).

Authentication methods, in order of preference:
    1. `Authorization: Bearer <token>` header (safest)
    2. `X-API-Key: <token>` header
    3. `?token=<token>` or `?api_key=<token>` query parameter

The query-parameter method exists specifically for fire-and-forget URL
clients like Stream Deck, browser bookmarks, or iOS Shortcuts that cannot
set headers. Note that tokens in URLs may appear in web-server access
logs and browser history — use a dedicated, easily revocable token per
such device, and never log API URLs to shared systems.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Router

from src.paths import kittyhack_root


TOKEN_FILE = os.path.join(kittyhack_root(), "api_tokens.json")
TOKEN_PREFIX = "khk_"
TOKEN_BYTES = 24  # 24 random bytes => 32 chars base64url => ~192 bits

# Rate limit: N failed auth attempts per window (per source IP) before we 429.
_AUTH_FAIL_WINDOW_S = 60.0
_AUTH_FAIL_MAX = 10
_auth_fail_log: dict[str, list[float]] = {}
_auth_fail_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Token store
# ---------------------------------------------------------------------------


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_tokens() -> list[dict[str, Any]]:
    if not os.path.exists(TOKEN_FILE):
        return []
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception as e:
        logging.warning(f"[API] Failed to read {TOKEN_FILE}: {e}")
    return []


def _save_tokens(tokens: Iterable[dict[str, Any]]) -> None:
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(list(tokens), f, indent=2)
    os.replace(tmp, TOKEN_FILE)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass


def create_token(label: str) -> tuple[str, dict[str, Any]]:
    """Generate a new token, persist its hash, return (clear_text, record)."""
    raw = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    record = {
        "id": secrets.token_hex(8),
        "label": label,
        "hash": _hash(raw),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_used_at": None,
    }
    tokens = _load_tokens()
    tokens.append(record)
    _save_tokens(tokens)
    return raw, record


def list_tokens() -> list[dict[str, Any]]:
    """Return token metadata (never the hash or clear-text)."""
    return [
        {k: v for k, v in t.items() if k != "hash"}
        for t in _load_tokens()
    ]


def revoke_token(token_id: str) -> bool:
    tokens = _load_tokens()
    new = [t for t in tokens if t.get("id") != token_id]
    if len(new) == len(tokens):
        return False
    _save_tokens(new)
    return True


def _find_token(clear_text: str) -> dict[str, Any] | None:
    h = _hash(clear_text)
    for t in _load_tokens():
        if hmac.compare_digest(t.get("hash", ""), h):
            return t
    return None


def _mark_used(token_id: str) -> None:
    try:
        tokens = _load_tokens()
        for t in tokens:
            if t.get("id") == token_id:
                t["last_used_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                _save_tokens(tokens)
                return
    except Exception as e:
        logging.debug(f"[API] mark_used failed: {e}")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    key = request.headers.get("x-api-key")
    if key:
        return key.strip()
    # Fallback for URL-only clients (Stream Deck, bookmarks, iOS Shortcuts).
    # Accept both ?token=... and ?api_key=... for convenience.
    for qp in ("token", "api_key"):
        qv = request.query_params.get(qp)
        if qv:
            return qv.strip()
    return None


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with _auth_fail_lock:
        recent = [t for t in _auth_fail_log.get(ip, []) if now - t < _AUTH_FAIL_WINDOW_S]
        _auth_fail_log[ip] = recent
        return len(recent) >= _AUTH_FAIL_MAX


def _record_fail(ip: str) -> None:
    with _auth_fail_lock:
        _auth_fail_log.setdefault(ip, []).append(time.monotonic())


def _authenticate(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    ip = _client_ip(request)
    if _rate_limited(ip):
        return None, JSONResponse({"error": "too_many_failed_attempts"}, status_code=429)

    token = _extract_token(request)
    if not token:
        return None, JSONResponse(
            {"error": "unauthorized", "detail": "missing Authorization: Bearer <token>"},
            status_code=401,
        )
    record = _find_token(token)
    if record is None:
        _record_fail(ip)
        logging.warning(f"[API] Rejected request from {ip}: invalid token")
        return None, JSONResponse({"error": "unauthorized"}, status_code=401)
    # Fire-and-forget last_used_at update
    threading.Thread(target=_mark_used, args=(record["id"],), daemon=True).start()
    return record, None


# ---------------------------------------------------------------------------
# Endpoint helpers
# ---------------------------------------------------------------------------


def _ok(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, **payload}, status_code=status_code)


def _err(detail: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": detail}, status_code=status_code)


def _door_state() -> dict[str, Any]:
    """Read current door state. Magnets.instance is initialised by backend startup."""
    try:
        from src.magnets_rfid import Magnets  # lazy import – avoids startup ordering
        inst = Magnets.instance
        if inst is None:
            return {"inside_unlocked": None, "outside_unlocked": None, "available": False}
        return {
            "inside_unlocked": bool(inst.get_inside_state()),
            "outside_unlocked": bool(inst.get_outside_state()),
            "available": True,
        }
    except Exception as e:
        logging.debug(f"[API] door_state: {e}")
        return {"inside_unlocked": None, "outside_unlocked": None, "available": False}


def _set_manual_override(key: str) -> None:
    """Set a flag in the backend's manual_door_override dict. Backend loop picks it up."""
    from src import backend  # lazy import
    if key not in backend.manual_door_override:
        raise ValueError(f"unknown override key: {key}")
    backend.manual_door_override[key] = True
    logging.info(f"[API] Manual door override set: {key}")


def _current_mode() -> dict[str, Any]:
    from src.baseconfig import CONFIG, AllowedToEnter, AllowedToExit
    entry = CONFIG.get("ALLOWED_TO_ENTER")
    exit_ = CONFIG.get("ALLOWED_TO_EXIT")
    return {
        "entry": entry.value if isinstance(entry, AllowedToEnter) else str(entry),
        "exit": exit_.value if isinstance(exit_, AllowedToExit) else str(exit_),
    }


def _apply_mode(entry: str | None, exit_: str | None) -> dict[str, Any]:
    """Persist new entry/exit modes and mirror to MQTT. Returns new mode dict."""
    from src.baseconfig import CONFIG, AllowedToEnter, AllowedToExit, save_config
    changed = []
    if entry is not None:
        try:
            CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter(entry)
            changed.append("ALLOWED_TO_ENTER")
        except ValueError:
            valid = [m.value for m in AllowedToEnter]
            raise ValueError(f"entry must be one of {valid}")
    if exit_ is not None:
        try:
            CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit(exit_)
            changed.append("ALLOWED_TO_EXIT")
        except ValueError:
            valid = [m.value for m in AllowedToExit]
            raise ValueError(f"exit must be one of {valid}")
    if changed:
        save_config()
        try:
            from src import backend
            for k in changed:
                backend.update_mqtt_config(k)
        except Exception as e:
            logging.debug(f"[API] mqtt mirror failed: {e}")
    return _current_mode()


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def _auth_or_fail(request: Request):
    record, err = _authenticate(request)
    return record, err


async def status(request: Request):
    _, err = await _auth_or_fail(request)
    if err:
        return err
    return _ok({"door": _door_state(), "mode": _current_mode()})


async def _door_action(request: Request, override_key: str, success_msg: str):
    _, err = await _auth_or_fail(request)
    if err:
        return err
    try:
        _set_manual_override(override_key)
    except Exception as e:
        return _err(str(e), status_code=500)
    return _ok({"action": override_key, "detail": success_msg, "door": _door_state()})


async def door_open(request: Request):
    # "open" = let the cat in from outside
    return await _door_action(request, "unlock_inside", "inside door unlock queued")


async def door_close(request: Request):
    return await _door_action(request, "lock_inside", "inside door lock queued")


async def door_unlock_outside(request: Request):
    return await _door_action(request, "unlock_outside", "outside door unlock queued")


async def door_lock_outside(request: Request):
    return await _door_action(request, "lock_outside", "outside door lock queued")


async def mode_get(request: Request):
    _, err = await _auth_or_fail(request)
    if err:
        return err
    return _ok({"mode": _current_mode()})


async def mode_set(request: Request):
    """Accept JSON body or query params: entry=..., exit=..."""
    _, err = await _auth_or_fail(request)
    if err:
        return err
    entry: str | None = None
    exit_: str | None = None
    # Prefer JSON body if provided
    if request.method in ("PUT", "POST") and request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
            if isinstance(body, dict):
                entry = body.get("entry")
                exit_ = body.get("exit")
        except Exception:
            return _err("invalid JSON body")
    # Fall back to query params (useful for GET-driven integrations)
    entry = entry or request.query_params.get("entry")
    exit_ = exit_ or request.query_params.get("exit")
    if entry is None and exit_ is None:
        return _err("provide 'entry' and/or 'exit'")
    try:
        new = _apply_mode(entry, exit_)
    except ValueError as e:
        return _err(str(e))
    return _ok({"mode": new})


# Combined preset modes — set both directions at once
_MODE_SHORTCUTS = {
    "open":   ("all",   "allow"),  # all cats in, all out
    "normal": ("known", "allow"),  # only known RFID cats may enter, all may leave
    "closed": ("none",  "deny"),   # nobody in or out
}


async def mode_shortcut(request: Request):
    _, err = await _auth_or_fail(request)
    if err:
        return err
    name = request.path_params.get("name", "")
    if name not in _MODE_SHORTCUTS:
        return _err(f"unknown mode '{name}', valid: {list(_MODE_SHORTCUTS)}", 404)
    entry, exit_ = _MODE_SHORTCUTS[name]
    try:
        new = _apply_mode(entry, exit_)
    except ValueError as e:
        return _err(str(e), status_code=500)
    return _ok({"mode": new, "shortcut": name})


async def mode_entry_set(request: Request):
    """GET /api/v1/mode/entry/{value} — only changes entry direction."""
    _, err = await _auth_or_fail(request)
    if err:
        return err
    value = request.path_params.get("value", "")
    try:
        new = _apply_mode(entry=value, exit_=None)
    except ValueError as e:
        return _err(str(e), status_code=400)
    return _ok({"mode": new, "changed": "entry"})


async def mode_exit_set(request: Request):
    """GET /api/v1/mode/exit/{value} — only changes exit direction."""
    _, err = await _auth_or_fail(request)
    if err:
        return err
    value = request.path_params.get("value", "")
    try:
        new = _apply_mode(entry=None, exit_=value)
    except ValueError as e:
        return _err(str(e), status_code=400)
    return _ok({"mode": new, "changed": "exit"})


async def cats_list(request: Request):
    _, err = await _auth_or_fail(request)
    if err:
        return err
    try:
        from src.baseconfig import CONFIG
        from src.database import db_get_cats, ReturnDataCatDB
        df = db_get_cats(CONFIG["KITTYHACK_DATABASE_PATH"], ReturnDataCatDB.all_except_photos)
        cats = df.to_dict(orient="records") if df is not None and not df.empty else []
        # Normalise booleans / stringify datetimes
        for c in cats:
            for k, v in list(c.items()):
                if hasattr(v, "isoformat"):
                    c[k] = v.isoformat()
        return _ok({"cats": cats, "count": len(cats)})
    except Exception as e:
        logging.exception("[API] cats_list failed")
        return _err(f"database error: {e}", status_code=500)


async def cat_update(request: Request):
    """PUT /api/v1/cats/{rfid_or_name} with JSON {allow_entry, allow_exit, enable_prey_detection}."""
    _, err = await _auth_or_fail(request)
    if err:
        return err
    ident = request.path_params.get("ident", "").strip()
    if not ident:
        return _err("missing cat identifier")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
    except Exception as e:
        return _err(f"invalid JSON body: {e}")
    allowed_keys = {"allow_entry", "allow_exit", "enable_prey_detection"}
    updates = {k: bool(v) for k, v in body.items() if k in allowed_keys}
    if not updates:
        return _err(f"no valid fields (allowed: {sorted(allowed_keys)})")
    try:
        import sqlite3
        from src.baseconfig import CONFIG
        db = CONFIG["KITTYHACK_DATABASE_PATH"]
        with sqlite3.connect(db) as conn:
            cur = conn.cursor()
            # Match by RFID first, fall back to case-insensitive name match.
            cur.execute("SELECT id FROM cats WHERE rfid = ? OR LOWER(name) = LOWER(?) LIMIT 1", (ident, ident))
            row = cur.fetchone()
            if not row:
                return _err(f"cat not found: {ident}", status_code=404)
            cat_id = row[0]
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            params = [int(updates[k]) for k in updates] + [cat_id]
            cur.execute(f"UPDATE cats SET {set_clause} WHERE id = ?", params)
            conn.commit()
        logging.info(f"[API] Updated cat {ident} ({cat_id}): {updates}")
        return _ok({"cat_id": cat_id, "updated": updates})
    except Exception as e:
        logging.exception("[API] cat_update failed")
        return _err(f"database error: {e}", status_code=500)


async def events_list(request: Request):
    _, err = await _auth_or_fail(request)
    if err:
        return err
    try:
        limit_raw = request.query_params.get("limit", "50")
        try:
            limit = max(1, min(500, int(limit_raw)))
        except ValueError:
            return _err("limit must be an integer")
        from src.baseconfig import CONFIG
        from src.database import db_get_motion_blocks
        df = db_get_motion_blocks(CONFIG["KITTYHACK_DATABASE_PATH"], block_count=limit)
        events = df.to_dict(orient="records") if df is not None and not df.empty else []
        for ev in events:
            for k, v in list(ev.items()):
                if hasattr(v, "isoformat"):
                    ev[k] = v.isoformat()
        return _ok({"events": events, "count": len(events)})
    except Exception as e:
        logging.exception("[API] events_list failed")
        return _err(f"database error: {e}", status_code=500)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_router() -> Router:
    """Build the Starlette Router that the ASGI middleware dispatches to."""
    routes = [
        Route("/api/v1/status", status, methods=["GET"]),

        # Door control – GET + POST for each action
        Route("/api/v1/door/open", door_open, methods=["GET", "POST"]),
        Route("/api/v1/door/close", door_close, methods=["GET", "POST"]),
        Route("/api/v1/door/unlock_inside", door_open, methods=["GET", "POST"]),
        Route("/api/v1/door/lock_inside", door_close, methods=["GET", "POST"]),
        Route("/api/v1/door/unlock_outside", door_unlock_outside, methods=["GET", "POST"]),
        Route("/api/v1/door/lock_outside", door_lock_outside, methods=["GET", "POST"]),

        # Mode / settings — separate entry/exit + combined presets
        Route("/api/v1/mode", mode_get, methods=["GET"]),
        Route("/api/v1/mode", mode_set, methods=["PUT", "POST"]),
        Route("/api/v1/mode/entry/{value}", mode_entry_set, methods=["GET", "POST"]),
        Route("/api/v1/mode/exit/{value}", mode_exit_set, methods=["GET", "POST"]),
        Route("/api/v1/mode/{name}", mode_shortcut, methods=["GET", "POST"]),

        # Cats
        Route("/api/v1/cats", cats_list, methods=["GET"]),
        Route("/api/v1/cats/{ident}", cat_update, methods=["PUT", "POST"]),

        # Events
        Route("/api/v1/events", events_list, methods=["GET"]),
    ]
    return Router(routes=routes)


class ApiMiddleware:
    """ASGI middleware that dispatches `/api/v1/*` to the Starlette API router.

    Non-API requests pass through untouched to the wrapped app (Shiny SPA).
    """

    def __init__(self, asgi_app):
        self.app = asgi_app
        self.router = build_router()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path.startswith("/api/"):
                await self.router(scope, receive, send)
                return
        await self.app(scope, receive, send)
