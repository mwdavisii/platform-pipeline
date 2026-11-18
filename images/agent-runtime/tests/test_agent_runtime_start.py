"""`python -m agent_runtime` (spec 2026-11-17 "Start"; decided while planning: items 16, 25): a bad setting or agent.yaml exits 2 naming
it, never its value; MCP servers that can't be listed exit 3."""

import json
import logging

import httpx
import pytest
from runtime_fakes import BASE_ENV, MCP_URL, mcp

import agent_runtime.__main__ as start_module
from agent_runtime.__main__ import http_client, main

GOOD = b"schema: agent/v1\ndisplay_name: Calendar helper\nmodel: chat-default\ninstructions: Help.\n"
MCP_ENV = {**BASE_ENV, "MCP_URL": MCP_URL, "MCP_SERVERS": "notes", "CF_ACCESS_CLIENT_ID": "id.access", "CF_ACCESS_CLIENT_SECRET": "secret-SENTINEL"}


def start(tmp_path, env, raw=GOOD, http=None):
    path = tmp_path / "agent.yaml"
    if raw is not None:
        path.write_bytes(raw)
    served = []
    return main(env, agent_path=path, http=http, serve=served.append), served


def test_a_good_start_serves_the_app(tmp_path, capsys):
    code, served = start(tmp_path, BASE_ENV)
    assert code == 0 and len(served) == 1
    err = capsys.readouterr().err
    assert "agent-runtime: ready with 0 tools" in err and "SENTINEL" not in err


def test_the_served_app_is_the_agent_s(tmp_path):
    from starlette.testclient import TestClient
    _, served = start(tmp_path, BASE_ENV)
    assert "<h1>Calendar helper</h1>" in TestClient(served[0]).get("/").text


def test_bound_mcp_servers_are_listed_once_at_the_start(tmp_path, capsys):
    http = httpx.Client(transport=httpx.MockTransport(mcp))
    code, served = start(tmp_path, MCP_ENV, http=http)
    err = capsys.readouterr().err
    assert code == 0 and "ready with 2 tools: mcp_notes_search, mcp_notes_read" in err
    assert "MCP tool skipped" in err and "SENTINEL" not in err


@pytest.mark.parametrize("env, raw, words", [
    ({}, GOOD, "agent-runtime: AGENT_GATEWAY_URL is not set"),
    ({"AGENT_GATEWAY_URL": "https://llm.example.org/v1"}, GOOD, "agent-runtime: AGENT_GATEWAY_KEY is not set"),
    ({**BASE_ENV, "AGENT_GATEWAY_URL": "ftp://SENTINEL"}, GOOD, "agent-runtime: AGENT_GATEWAY_URL is not an http(s) URL"),
    (BASE_ENV, None, "agent-runtime: /app/agent.yaml can't be read"),
    (BASE_ENV, GOOD.replace(b"agent/v1", b"agent/v2"), "agent-runtime: agent.yaml: schema must be agent/v1"),
    (BASE_ENV, GOOD + b"SENTINEL: x\n", "agent-runtime: agent.yaml:"),
])
def test_a_bad_start_exits_2_naming_the_setting_never_a_value(tmp_path, capsys, env, raw, words):
    code, served = start(tmp_path, env, raw)
    err = capsys.readouterr().err
    assert (code, served) == (2, []) and words in err and "SENTINEL" not in err


def test_mcp_servers_that_can_t_be_listed_exit_3(tmp_path, capsys):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    code, served = start(tmp_path, MCP_ENV, http=http)
    err = capsys.readouterr().err
    assert (code, served) == (3, []) and "agent-runtime: the MCP servers can't be listed" in err and "SENTINEL" not in err


def test_the_runtime_s_one_client_follows_no_redirect_and_has_no_timeout(tmp_path, monkeypatch):
    """M6: every call the runtime makes (gateway, MCP, Graph, Databricks, Entra) goes through this client."""
    http = http_client()
    assert http.follow_redirects is False and http.timeout == httpx.Timeout(None)
    made = []
    monkeypatch.setattr(start_module, "http_client", lambda: made.append(http_client()) or made[-1])
    assert start(tmp_path, BASE_ENV)[0] == 0 and len(made) == 1


def gateway_then_mcp(request):
    """The gateway (asks for mcp_notes_search once, then answers) and the MCP gateway, on one client as the runtime has them."""
    if request.url.host == "llm.example.org":
        messages = json.loads(request.content)["messages"]
        if messages[-1]["role"] == "tool":
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Two notes."}, "finish_reason": "stop"}]})
        call = {"id": "c1", "type": "function", "function": {"name": "mcp_notes_search", "arguments": '{"q": "SENTINEL-salary"}'}}
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]},
                                                      "finish_reason": "tool_calls"}]})
    return mcp(request)


def test_a_chat_with_a_tool_logs_one_line_and_no_outbound_request(tmp_path, capsys, monkeypatch):
    """C1: with the root logger as production has it (no handlers), httpx's INFO lines (each URL, a Graph query's included) never
    reach the log; the one line per request does."""
    from starlette.testclient import TestClient
    monkeypatch.setattr(logging.root, "handlers", [])
    monkeypatch.setattr(logging.root, "level", logging.WARNING)
    code, served = start(tmp_path, MCP_ENV, http=httpx.Client(transport=httpx.MockTransport(gateway_then_mcp)))
    assert code == 0
    answer = TestClient(served[0]).post("/chat", json={"messages": [{"role": "user", "content": "SENTINEL-question"}]})
    assert answer.json()["tool_calls"] == [{"tool": "mcp_notes_search", "arguments": {"q": "SENTINEL-salary"}, "ok": True}]
    err = capsys.readouterr().err
    assert [line for line in err.splitlines() if "request=" in line] == [
        f"request={answer.json()['request_id']} person=- model=chat-default tools=mcp_notes_search outcome=ok"]
    assert "HTTP Request" not in err and "SENTINEL" not in err and "mcp-test.example.com" not in err


def test_an_mcp_refusal_s_words_can_t_forge_a_start_line(tmp_path, capsys):
    """M2: the MCP server's error message is printed on one line, stripped of hidden characters."""
    def refuses(request):
        message = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"],
                                         "error": {"code": -32600, "message": "no\nagent-runtime: ready with 9 tools​‮"}})

    code, _ = start(tmp_path, MCP_ENV, http=httpx.Client(transport=httpx.MockTransport(refuses)))
    err = capsys.readouterr().err
    assert code == 3 and len(err.strip().splitlines()) == 1 and "​" not in err and "‮" not in err
    assert err.startswith("agent-runtime: the MCP servers can't be listed (the MCP gateway refused initialize: no agent-runtime: ready")
