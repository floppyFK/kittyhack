"""Process-instance probe used to reload the page after a reboot."""

from __future__ import annotations

import asyncio
import json

from src.api import ApiMiddleware, HtmlNoStoreMiddleware, instance_status
from src.runtime_flags import PROCESS_INSTANCE_ID


def _header(messages, name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for message in messages:
        if message.get("type") != "http.response.start":
            continue
        for key, value in message.get("headers") or []:
            if key.lower() == wanted:
                return value.decode("latin-1")
    return None


def _body(messages) -> bytes:
    chunks = [
        message.get("body") or b""
        for message in messages
        if message.get("type") == "http.response.body"
    ]
    return b"".join(chunks)


def _invoke(app, path: str, method: str = "GET"):
    messages = []

    async def scenario():
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)

    asyncio.run(scenario())
    return messages


def test_instance_status_reports_this_process(monkeypatch):
    import src.startup as startup

    monkeypatch.setattr(startup, "git_version", "v3.0.1", raising=False)
    payload = instance_status()
    assert payload["id"] == PROCESS_INSTANCE_ID
    assert payload["version"] == "v3.0.1"
    assert instance_status()["id"] == payload["id"]


def test_instance_endpoint_is_public_and_not_cached():
    called = {"n": 0}

    async def inner(scope, receive, send):
        called["n"] += 1
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = ApiMiddleware(inner)
    messages = _invoke(app, "/kh/instance")
    assert called["n"] == 0
    assert messages[0]["status"] == 200
    assert _header(messages, "cache-control") == "no-store"
    assert _header(messages, "content-type").startswith("application/json")
    payload = json.loads(_body(messages))
    assert payload["id"] == PROCESS_INSTANCE_ID
    assert payload["version"]

    messages = _invoke(app, "/kh/instance/")
    assert json.loads(_body(messages))["id"] == PROCESS_INSTANCE_ID
    assert called["n"] == 0


def test_instance_endpoint_rejects_post_without_falling_through():
    called = {"n": 0}

    async def inner(scope, receive, send):
        called["n"] += 1
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = _invoke(ApiMiddleware(inner), "/kh/instance", method="POST")
    assert messages[0]["status"] == 405
    assert _header(messages, "cache-control") == "no-store"
    assert called["n"] == 0


def test_other_paths_still_reach_the_inner_app():
    called = {"n": 0}

    async def inner(scope, receive, send):
        called["n"] += 1
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = _invoke(ApiMiddleware(inner), "/live-view/")
    assert messages[0]["status"] == 204
    assert called["n"] == 1


def test_html_responses_are_not_stored():
    async def inner(scope, receive, send):
        path = scope.get("path")
        if path == "/page":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"cache-control", b"max-age=60"),
                ],
            })
            await send({"type": "http.response.body", "body": b"<html></html>"})
            return
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"image/jpeg")],
        })
        await send({"type": "http.response.body", "body": b"jpeg"})

    app = HtmlNoStoreMiddleware(inner)
    html = _invoke(app, "/page")
    assert _header(html, "cache-control") == "no-store"
    assert _header(html, "content-type") == "text/html; charset=utf-8"
    image = _invoke(app, "/photo.jpg")
    assert _header(image, "cache-control") is None
    assert _header(image, "content-type") == "image/jpeg"
