"""Fakes for the runtime's tests. Each answers the real API's shape (decided while planning: items 15 to 18)."""

import json
from urllib.parse import parse_qs

import httpx

TENANT = "00000000-0000-4000-8000-00000000a2e0"
CLIENT = "11111111-1111-4111-8111-111111111111"
BASE_ENV = {"AGENT_GATEWAY_URL": "https://llm.example.org/v1", "AGENT_GATEWAY_KEY": "sk-gateway-SENTINEL"}
AGENT = {"schema": "agent/v1", "display_name": "Calendar helper", "model": "chat-default",
         "instructions": "You help the team find free time.", "starters": ["When is everyone free on Thursday?"]}


class Recorder:
    """An httpx transport handler that keeps every request it answers."""

    def __init__(self, handler):
        self.handler, self.requests = handler, []

    def __call__(self, request):
        self.requests.append(request)
        return self.handler(request)


def client(handler) -> tuple[httpx.Client, Recorder]:
    recorder = Recorder(handler)
    return httpx.Client(transport=httpx.MockTransport(recorder)), recorder


def body(request) -> dict:
    return json.loads(request.content) if request.content else {}


DBX_HOST = "adb-1111111111111111.1.azuredatabricks.net"


def entra(request):
    """Entra's token endpoint: client_credentials with a client assertion answers a bearer token for the scope asked."""
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert request.method == "POST" and request.url.path.endswith("/oauth2/v2.0/token")
    assert form["grant_type"] == "client_credentials" and form["client_assertion_type"].endswith(":jwt-bearer")
    return httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3599, "ext_expires_in": 3599,
                                     "access_token": "tok-" + form["scope"].split("/")[0]})


def databricks(request, *, pending=1, second_chunk=True):
    """The SQL Statement Execution API: a POST answers PENDING (its wait_timeout passed), then GETs answer RUNNING `pending` times,
    then SUCCEEDED with the manifest and the first inline chunk, which links the second (next_chunk_internal_link)."""
    path = request.url.path
    if request.method == "POST" and path == "/api/2.0/sql/statements":
        return httpx.Response(200, json={"statement_id": "01ef-0001", "status": {"state": "PENDING"}})
    if request.method == "GET" and path == "/api/2.0/sql/statements/01ef-0001":
        databricks.polls = getattr(databricks, "polls", 0) + 1
        if databricks.polls <= pending:
            return httpx.Response(200, json={"statement_id": "01ef-0001", "status": {"state": "RUNNING"}})
        result = {"chunk_index": 0, "row_offset": 0, "row_count": 1, "data_array": [["1", "Thursday"]]}
        if second_chunk:
            result.update(next_chunk_index=1, next_chunk_internal_link="/api/2.0/sql/statements/01ef-0001/result/chunks/1")
        return httpx.Response(200, json={"statement_id": "01ef-0001", "status": {"state": "SUCCEEDED"},
                                         "manifest": {"format": "JSON_ARRAY", "schema": {"column_count": 2, "columns": [
                                             {"name": "n", "type_name": "INT", "position": 0},
                                             {"name": "day", "type_name": "STRING", "position": 1}]},
                                             "total_chunk_count": 2 if second_chunk else 1, "total_row_count": 2 if second_chunk else 1,
                                             "truncated": False},
                                         "result": result})
    if request.method == "GET" and path == "/api/2.0/sql/statements/01ef-0001/result/chunks/1":
        return httpx.Response(200, json={"chunk_index": 1, "row_offset": 1, "row_count": 1, "data_array": [["2", "Friday"]]})
    raise AssertionError(f"no Databricks answer for {request.method} {path}")


MCP_URL = "https://mcp-test.example.com/mcp"
SESSION = "c0ffee00-1111-4222-8333-444455556666"
MCP_TOOLS = [[{"name": "notes.search", "description": "Search the team's notes.", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}],
             [{"name": "notes_read", "description": "Read one note.", "inputSchema": {"type": "object"}},
              {"name": "notes:search", "description": "A second tool whose name folds onto the first's.", "inputSchema": {"type": "object"}}]]


def sse(*messages, split=False) -> str:
    """An event stream of JSON-RPC messages; with split, each message's JSON spans several data lines (one event, joined by line feeds)."""
    events = []
    for number, message in enumerate(messages):
        text = json.dumps(message, indent=1 if split else None)
        lines = text.split("\n")
        events.append(f"id: {number}\nevent: message\n" + "".join(f"data: {line}\n" for line in lines) + "\n")
    return ": a comment line\n\n" + "".join(events)


def mcp(request, *, events=True, session=SESSION, protocol="2025-06-18", split=False):
    """The MCP gateway (Streamable HTTP, JSON-RPC 2.0): initialize answers JSON and a session id; the initialized notification 202;
    tools/list two pages (nextCursor); tools/call an event stream (a progress notification, then the reply) or JSON with
    events=False. Every call carries the service token, both Accept types and, after initialize, the session and the version."""
    assert request.method == "POST" and str(request.url) == MCP_URL
    assert request.headers["cf-access-client-id"] == "id.access" and request.headers["cf-access-client-secret"] == "secret-SENTINEL"
    assert "text/event-stream" in request.headers["accept"] and "application/json" in request.headers["accept"]
    assert request.headers["content-type"] == "application/json"
    message = body(request)
    assert message["jsonrpc"] == "2.0"
    method = message.get("method")
    if method == "initialize":
        assert "mcp-session-id" not in request.headers and "mcp-protocol-version" not in request.headers
        return httpx.Response(200, headers={"mcp-session-id": session}, json={"jsonrpc": "2.0", "id": message["id"], "result": {
            "protocolVersion": protocol, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "gateway", "version": "1"}}})
    assert request.headers["mcp-session-id"] == session and request.headers["mcp-protocol-version"] == protocol
    if method == "notifications/initialized":
        assert "id" not in message
        return httpx.Response(202)
    if method == "tools/list":
        page = 1 if (message.get("params") or {}).get("cursor") == "page-2" else 0
        result = {"tools": MCP_TOOLS[page], **({"nextCursor": "page-2"} if page == 0 else {})}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})
    if method == "tools/call":
        name = message["params"]["name"]
        result = ({"content": [{"type": "text", "text": "no such note"}], "isError": True} if name == "notes_read"
                  else {"content": [{"type": "text", "text": "2 notes match"}], "isError": False})
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        if not events:
            return httpx.Response(200, json=reply)
        progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": 1, "progress": 1}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse(progress, reply, split=split))
    raise AssertionError(f"no MCP answer for {method}")
