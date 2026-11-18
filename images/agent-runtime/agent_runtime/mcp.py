"""The environment's MCP servers through the shared gateway (spec 2026-11-17 "Tools"; decided while planning: item 16): one Streamable
HTTP endpoint (MCP_URL), the app's Cloudflare service token on every call, JSON-RPC 2.0. A reply may be JSON or an event stream.
After initialize every request carries the session id the gateway gave (Mcp-Session-Id) and the negotiated MCP-Protocol-Version; a
404 to a request with a session means the session ended, so the client initializes again and sends the request once more (the
protocol's rule, not a retry budget). A repeated tools/list cursor is an error (it would loop).

Decided (review M1): no reply here, in databricks.py or in graph.py has a size or memory cap of the runtime's own (no budgets, A10):
a reply is read whole, and the pod's memory limit is the bound."""

import itertools
import json
import re
import threading

import httpx

from . import NAME
from .tools import ToolError

PROTOCOL = "2025-06-18"
SPOKEN = frozenset({"2025-06-18", "2025-03-26"})  # the versions whose Streamable HTTP this client follows
SESSION_ID = re.compile(r"[\x21-\x7e]+")  # visible ASCII, as the transport requires
LINE_END = re.compile(r"\r\n|\r|\n")  # the SSE format's line ends, and only those (a U+2028 in the JSON is text)


class McpError(Exception):
    """The gateway didn't answer as MCP does: at the start this stops the runtime; during a call it is the tool's error."""


class _SessionEnded(Exception):
    pass


def _events(text: str) -> list:
    """The data of each event in a text/event-stream (data lines joined by a line feed, as the SSE format says)."""
    found, data = [], []
    for line in LINE_END.split(text) + [""]:
        if not line:
            if data:
                found.append("\n".join(data))
            data = []
        elif line.startswith("data:"):
            data.append(line[5:][1:] if line[5:].startswith(" ") else line[5:])
    return found


def _messages(response) -> list:
    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
    try:
        if kind == "application/json":
            doc = response.json()
            return doc if isinstance(doc, list) else [doc]
        if kind == "text/event-stream":
            return [json.loads(data) for data in _events(response.text) if data.strip()]
    except ValueError:
        raise McpError("the MCP gateway's answer is not valid JSON") from None
    raise McpError("the MCP gateway answered neither JSON nor an event stream")


def _content(item) -> str:
    if item.get("type") == "text":
        return str(item.get("text", ""))
    resource = item.get("resource")
    if item.get("type") == "resource" and isinstance(resource, dict) and isinstance(resource.get("text"), str):
        return resource["text"]
    # A content type's name in a placeholder, not data: the cut is not a token or time budget (no budgets, A10).
    return f"[{str(item.get('type') or 'unknown')[:40]} content omitted]"


class McpClient:
    def __init__(self, settings, http):
        self.url, self.http = settings.url, http
        self.headers = {"CF-Access-Client-Id": settings.client_id, "CF-Access-Client-Secret": settings.client_secret,
                        "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        self.session, self.version = None, None
        self._ids, self._lock = itertools.count(1), threading.Lock()

    def _post(self, message: dict, session, version):
        headers = dict(self.headers)
        if session:
            headers["Mcp-Session-Id"] = session
        if version:
            headers["MCP-Protocol-Version"] = version
        try:
            response = self.http.post(self.url, json=message, headers=headers, timeout=None)
        except httpx.HTTPError as exc:
            raise McpError(f"the MCP gateway could not be reached ({type(exc).__name__})") from None
        if response.status_code == 404 and session:
            raise _SessionEnded()
        if not 200 <= response.status_code < 300:
            raise McpError(f"the MCP gateway answered {response.status_code}")
        return response

    def _request(self, method: str, params: dict, session, version) -> tuple:
        number = next(self._ids)
        response = self._post({"jsonrpc": "2.0", "id": number, "method": method, "params": params}, session, version)
        reply = next((m for m in _messages(response) if isinstance(m, dict) and m.get("id") == number and "method" not in m), None)
        if reply is None:
            raise McpError(f"the MCP gateway's answer to {method} has no reply")
        if "error" in reply:
            error = reply["error"] if isinstance(reply["error"], dict) else {}
            # A short error summary, not data: the cut is not a token or time budget (no budgets, A10).
            raise McpError(f"the MCP gateway refused {method}: {str(error.get('message', ''))[:200]}")
        if not isinstance(reply.get("result"), dict):
            raise McpError(f"the MCP gateway's reply to {method} has no result")
        return reply["result"], response

    def start(self) -> None:
        with self._lock:
            self._initialize()

    def _initialize(self) -> None:
        """initialize, then notifications/initialized; the caller holds the lock."""
        result, response = self._request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                                        "clientInfo": {"name": NAME, "version": "1"}}, None, None)
        version = result.get("protocolVersion")
        if version not in SPOKEN:
            raise McpError("the MCP gateway answered a protocol version this runtime doesn't speak")
        session = response.headers.get("mcp-session-id")
        if session is not None and not SESSION_ID.fullmatch(session):
            raise McpError("the MCP gateway's session id is not visible ASCII")
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session, version)
        except _SessionEnded:
            raise McpError("the MCP gateway ended the session it had just started") from None
        self.session, self.version = session, version

    def request(self, method: str, params: dict) -> dict:
        session, version = self.session, self.version
        try:
            return self._request(method, params, session, version)[0]
        except _SessionEnded:
            with self._lock:
                if self.session == session:  # another request may have started the new session already
                    self._initialize()
            try:
                return self._request(method, params, self.session, self.version)[0]
            except _SessionEnded:
                raise McpError("the MCP gateway ended the new session too") from None

    def tools(self) -> list:
        """Every tool the gateway lists, every page (no cap)."""
        found, cursor, seen = [], None, set()
        while True:
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            found += [t for t in result.get("tools") or [] if isinstance(t, dict)]
            cursor = result.get("nextCursor")
            if not cursor:
                return found
            if not isinstance(cursor, str):
                raise McpError("the MCP gateway's nextCursor is not text")
            if cursor in seen:  # protocol correctness, not a cap: a cursor seen before would loop forever
                raise McpError("the MCP gateway named a tools/list cursor again: the list can't be read to its end")
            seen.add(cursor)

    def call(self, name: str, arguments):
        if not isinstance(arguments, dict):
            raise ToolError("the tool's arguments must be a JSON object")
        try:
            result = self.request("tools/call", {"name": name, "arguments": arguments})
        except McpError as exc:
            raise ToolError(str(exc)) from None
        texts = [_content(c) for c in result.get("content") or [] if isinstance(c, dict)]
        if result.get("isError"):
            # A short error summary, not data: the cut is not a token or time budget (no budgets, A10).
            raise ToolError("; ".join(texts)[:500] or "the MCP tool failed")
        return result["structuredContent"] if isinstance(result.get("structuredContent"), dict) else {"content": texts}
