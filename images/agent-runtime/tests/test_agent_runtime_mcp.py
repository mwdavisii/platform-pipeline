"""MCP through the gateway (spec 2026-11-17 "Tools"; decided while planning: item 16): Streamable HTTP, JSON-RPC 2.0, the app's
Cloudflare service token on every call; a reply as JSON or an event stream; every tools/list page."""

import functools
import json

import httpx
import pytest
from runtime_fakes import MCP_URL, SESSION, body, client, mcp

from agent_runtime.mcp import McpClient, McpError
from agent_runtime.settings import Mcp
from agent_runtime.tools import ToolError

SETTINGS = Mcp(MCP_URL, ("notes",), "id.access", "secret-SENTINEL")


def started(handler=mcp):
    http, recorder = client(handler)
    found = McpClient(SETTINGS, http)
    found.start()
    return found, recorder


def test_the_start_initializes_and_says_so():
    _, recorder = started()
    sent = [body(r) for r in recorder.requests]
    assert [m["method"] for m in sent] == ["initialize", "notifications/initialized"]
    assert sent[0]["params"]["protocolVersion"] == "2025-06-18" and sent[0]["params"]["clientInfo"]["name"] == "golden-road-agent-runtime"
    assert sent[0]["params"]["capabilities"] == {} and isinstance(sent[0]["id"], int)


def test_every_page_of_tools_is_listed():
    found, recorder = started()
    assert [t["name"] for t in found.tools()] == ["notes.search", "notes_read", "notes:search"]
    lists = [body(r) for r in recorder.requests if body(r).get("method") == "tools/list"]
    assert [m.get("params") for m in lists] == [{}, {"cursor": "page-2"}]


def test_a_call_reads_an_event_stream_or_json():
    found, _ = started()
    assert found.call("notes.search", {"q": "thursday"}) == {"content": ["2 notes match"]}
    found, _ = started(functools.partial(mcp, events=False))
    assert found.call("notes.search", {}) == {"content": ["2 notes match"]}


def test_an_event_s_data_lines_are_one_message():
    found, _ = started(functools.partial(mcp, split=True))
    assert found.call("notes.search", {}) == {"content": ["2 notes match"]}


def test_structured_content_and_other_content_kinds():
    def answer(request):
        message = body(request)
        if message.get("method") == "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": {
                "content": [{"type": "text", "text": "see below"}, {"type": "image", "data": "AAAA", "mimeType": "image/png"},
                            {"type": "resource", "resource": {"uri": "note://1", "text": "the note"}}]}})
        return mcp(request)
    found, _ = started(answer)
    assert found.call("notes.search", {}) == {"content": ["see below", "[image content omitted]", "the note"]}

    def structured(request):
        message = body(request)
        if message.get("method") == "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": {
                "content": [{"type": "text", "text": "{\"n\": 2}"}], "structuredContent": {"n": 2}}})
        return mcp(request)
    found, _ = started(structured)
    assert found.call("notes.search", {}) == {"n": 2}


def test_a_tool_s_own_error_is_a_tool_error():
    found, _ = started()
    with pytest.raises(ToolError, match="no such note"):
        found.call("notes_read", {})


def test_the_negotiated_version_is_the_one_sent():
    found, recorder = started(functools.partial(mcp, protocol="2025-03-26"))
    found.tools()
    assert recorder.requests[-1].headers["mcp-protocol-version"] == "2025-03-26"


def test_a_version_the_runtime_doesn_t_speak_is_an_mcp_error():
    http, _ = client(functools.partial(mcp, protocol="1999-01-01"))
    with pytest.raises(McpError, match="protocol version"):
        McpClient(SETTINGS, http).start()


def test_an_expired_session_is_started_again():
    """A 404 to a request with a session means the session ended: the client initializes again and sends the request once more."""
    state = {"expired": False}
    new = "dead0000-1111-4222-8333-444455556666"

    def handler(request):
        message = body(request)
        if state["expired"]:
            return mcp(request, session=new)
        if message.get("method") == "tools/call":
            assert request.headers["mcp-session-id"] == SESSION
            state["expired"] = True
            return httpx.Response(404)
        return mcp(request)
    found, recorder = started(handler)
    assert found.call("notes.search", {}) == {"content": ["2 notes match"]}
    assert [body(r).get("method") for r in recorder.requests] == [
        "initialize", "notifications/initialized", "tools/call", "initialize", "notifications/initialized", "tools/call"]


def test_a_gateway_that_fails_a_call_is_a_tool_error_without_the_secret():
    def handler(request):
        if body(request).get("method") == "tools/call":
            return httpx.Response(500, text="secret-SENTINEL")
        return mcp(request)
    found, _ = started(handler)
    with pytest.raises(ToolError, match="answered 500") as caught:
        found.call("notes.search", {})
    assert "SENTINEL" not in str(caught.value)


def test_an_unreachable_gateway_is_an_mcp_error_at_the_start_and_a_tool_error_in_a_call():
    def down(request):
        raise httpx.ConnectError("refused", request=request)
    http, _ = client(down)
    with pytest.raises(McpError, match="could not be reached"):
        McpClient(SETTINGS, http).start()
    found, _ = started(lambda r: down(r) if body(r).get("method") == "tools/call" else mcp(r))
    with pytest.raises(ToolError, match="could not be reached"):
        found.call("notes.search", {})


def test_arguments_that_are_not_an_object_are_a_tool_error():
    found, _ = started()
    with pytest.raises(ToolError, match="object"):
        found.call("notes.search", ["q"])


@pytest.mark.parametrize("answer", [
    httpx.Response(401, json={}), httpx.Response(200, text="<html>", headers={"content-type": "text/html"}),
    httpx.Response(200, json={"jsonrpc": "2.0", "id": 99, "result": {}}),
    httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "bad"}}),
    httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "not an object"}),
    httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}),
    httpx.Response(200, text="data: {not json\n\n", headers={"content-type": "text/event-stream"}),
    httpx.Response(200, headers={"mcp-session-id": "bad session\x01"},
                   json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "capabilities": {}}}),
])
def test_a_gateway_that_won_t_answer_is_an_mcp_error(answer):
    http, _ = client(lambda r: answer)
    with pytest.raises(McpError):
        McpClient(SETTINGS, http).start()


def test_a_session_ended_at_once_is_an_mcp_error():
    def handler(request):
        if body(request).get("method") == "notifications/initialized":
            return httpx.Response(404)
        return mcp(request)
    http, _ = client(handler)
    with pytest.raises(McpError, match="just started"):
        McpClient(SETTINGS, http).start()


def test_a_cursor_seen_before_is_an_mcp_error_not_a_loop():
    def handler(request):
        message = body(request)
        if message.get("method") == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": {"tools": [], "nextCursor": "same"}})
        return mcp(request)
    found, _ = started(handler)
    with pytest.raises(McpError, match="again"):
        found.tools()


@pytest.mark.parametrize("newline", ["\r\n", "\r", "\n"])
def test_an_event_stream_s_lines_end_at_cr_lf_or_either(newline):
    """Only CR LF, CR and LF end a line (the SSE format): a U+2028 or a NEL inside the JSON is text."""
    def handler(request):
        message = body(request)
        if message.get("method") == "tools/call":
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": {"content": [{"type": "text", "text": "a\u2028b\x85c"}]}}
            text = f"event: message{newline}data: {json.dumps(reply, ensure_ascii=False)}{newline}{newline}"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)
        return mcp(request)
    found, _ = started(handler)
    assert found.call("notes.search", {}) == {"content": ["a\u2028b\x85c"]}
