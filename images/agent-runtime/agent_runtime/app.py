"""The agent's three routes (spec 2026-11-17 A3; decided while planning: items 22 to 24): / (the chat page), POST /chat (JSON) and
/healthz. It runs behind the edge's sign-in; each /chat request is logged once with the person, never with its text, a tool's result,
a tool's arguments or the key. The chat page holds no inline script or style, under the portal's CSP
(bootstrap/golden-road-portal/portal/web/headers.py; a test holds the two equal)."""

import logging
import re
import secrets
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .loop import run
from .model import GatewayError

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")
HEADERS = {"content-security-policy": CSP, "x-content-type-options": "nosniff", "referrer-policy": "same-origin", "x-frame-options": "DENY",
           "strict-transport-security": "max-age=31536000", "cache-control": "no-store"}
PERSON_HEADER = "cf-access-authenticated-user-email"  # Cloudflare Access sets it at the edge (decided while planning: item 22)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}")
STATIC = Path(__file__).with_name("static")
ROLES = ("user", "assistant")
SURROGATE = re.compile("[\ud800-\udfff]")  # a lone one: JSON can escape it, but it isn't text and can't be sent on
log = logging.getLogger("agent_runtime")


class SecurityHeaders:
    """The headers on every answer, the errors' included (an ASGI wrapper, as the portal's)."""

    def __init__(self, app):
        self.app = app
        self.headers = [(k.encode(), v.encode()) for k, v in HEADERS.items()]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers", []) if k.decode("latin-1").lower() not in HEADERS]
                message = {**message, "headers": kept + self.headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def chat_page(agent: dict) -> str:
    name = escape(agent["display_name"])
    starters = "".join(f'<li><button type="button" class="starter">{escape(s)}</button></li>' for s in agent.get("starters") or [])
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{name}</title><link rel="stylesheet" href="/static/chat.css"><script src="/static/chat.js" defer></script></head>\n'
            f'<body><main><h1>{name}</h1>\n<ul class="starters">{starters}</ul>\n<ol id="messages" aria-live="polite"></ol>\n'
            '<form id="ask"><label for="text">Your message</label><textarea id="text" name="text" rows="3" required></textarea>'
            '<button type="submit">Send</button></form>\n'
            '<p class="hint">Answers come from a model and can be wrong. The tools it used are listed under each answer.</p></main></body></html>\n')


def conversation(body):
    """The messages, when the body is {"messages": [...]} of user and assistant text ending with the user's; else None. No count or
    size cap of the runtime's own (decided while planning: item 24)."""
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list) or not messages or set(body) != {"messages"}:
        return None
    for m in messages:
        if not isinstance(m, dict) or set(m) != {"role", "content"} or not isinstance(m["role"], str) or m["role"] not in ROLES \
                or not isinstance(m["content"], str) or SURROGATE.search(m["content"]):
            return None
    return messages if messages[-1]["role"] == "user" else None


def _same_origin(request) -> bool:
    """No Origin (not a browser's cross-site request), or one naming this host. The scheme isn't compared: the edge ends TLS, so the
    runtime sees http where the browser says https. "null" (an opaque origin) is another origin."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = urlsplit(origin).netloc.lower() if "://" in origin else ""
    return bool(host) and host == request.headers.get("host", "").lower()


def _person(request) -> str:
    value = request.headers.get(PERSON_HEADER, "")
    return value if EMAIL.fullmatch(value) else "-"


def create_app(agent: dict, system: str, tools: list, gateway, *, new_id=lambda: secrets.token_hex(8)) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityHeaders)
    page = chat_page(agent)
    known = {t.name for t in tools}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def chat_page_route():
        return page

    @app.get("/static/chat.js")
    def script():
        return FileResponse(STATIC / "chat.js", media_type="text/javascript")

    @app.get("/static/chat.css")
    def style():
        return FileResponse(STATIC / "chat.css", media_type="text/css")

    @app.post("/chat")
    async def chat(request: Request):
        request_id, person = new_id(), _person(request)

        def line(used, outcome):
            # A name the model made up is not one of the agent's tools and may hold anything: it is logged as "unknown".
            names = [u["tool"] if u["tool"] in known else "unknown" for u in used]
            log.info("request=%s person=%s model=%s tools=%s outcome=%s", request_id, person, agent["model"], ",".join(names) or "-", outcome)

        def failed(error, status):
            return JSONResponse({"error": error, "request_id": request_id}, status_code=status)

        try:
            if not _same_origin(request):
                line([], "refused")
                return failed("This agent answers its own page only.", 403)
            if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                line([], "refused")
                return failed("Send the conversation as application/json.", 415)
            try:
                body = await request.json()
            except (ValueError, RecursionError):  # not JSON, not UTF-8, or nested deeper than the parser goes
                body = None
            messages = conversation(body)
            if messages is None:
                line([], "refused")
                return failed('Send {"messages": [...]} of user and assistant text, the last one the user\'s.', 400)
            try:
                reply, used = await run_in_threadpool(run, gateway, agent["model"], system, tools, messages)
            except GatewayError:
                line([], "gateway_error")
                return failed("The model gateway did not answer. Try again.", 502)
            line(used, "ok")
            return {"reply": reply, "tool_calls": used, "request_id": request_id}
        except Exception:  # noqa: BLE001 - the request still gets its id, its one log line and the headers; the error's words may hold anything
            line([], "error")
            return failed("The agent failed. Try again.", 500)

    return app
