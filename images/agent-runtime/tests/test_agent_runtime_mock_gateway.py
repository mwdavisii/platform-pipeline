"""The mock model gateway (spec 2026-11-17 "Mock only"; decided while planning: item 21): deterministic OpenAI chat completions, used by
the tests, the docker test and the local demo, never by the runtime itself."""

import re
from pathlib import Path

from starlette.testclient import TestClient

from agent_runtime import mock_gateway

TOOLS = [{"type": "function", "function": {"name": n, "description": "", "parameters": {"type": "object"}}}
         for n in ("databricks_query", "graph_read", "mcp_notes_search")]


def ask(messages, tools=None, key="mock-key"):
    with TestClient(mock_gateway.app) as test:
        return test.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                         json={"model": "chat-default", "messages": messages, **({"tools": tools} if tools else {})})


def test_the_wrong_key_is_a_401():
    assert ask([{"role": "user", "content": "hi"}], key="other").status_code == 401


def test_the_key_can_be_set(monkeypatch):
    monkeypatch.setenv("MOCK_GATEWAY_KEY", "other")
    assert ask([{"role": "user", "content": "hi"}], key="other").status_code == 200
    assert ask([{"role": "user", "content": "hi"}]).status_code == 401


def test_each_built_in_tool_gets_its_arguments():
    for name, arguments in (("databricks_query", '{"statement": "SELECT 1"}'), ("graph_read", '{"path": "/organization"}'),
                            ("mcp_notes_search", "{}")):
        answer = ask([{"role": "user", "content": f"try {name}"}], TOOLS).json()["choices"][0]
        assert answer["message"]["tool_calls"][0]["function"] == {"name": name, "arguments": arguments}
        assert answer["message"]["tool_calls"][0]["type"] == "function" and answer["finish_reason"] == "tool_calls"


def test_after_a_tool_message_it_says_which_tool_it_used():
    call = {"id": "call_mock_1", "type": "function", "function": {"name": "graph_read", "arguments": "{}"}}
    answer = ask([{"role": "user", "content": "try graph_read"}, {"role": "assistant", "content": None, "tool_calls": [call]},
                  {"role": "tool", "tool_call_id": "call_mock_1", "content": "{}"}], TOOLS).json()["choices"][0]
    assert answer["message"]["content"] == "I used graph_read: the tool answered." and answer["finish_reason"] == "stop"


def test_the_answer_has_the_openai_shape():
    doc = ask([{"role": "user", "content": "x" * 300}]).json()
    assert doc["object"] == "chat.completion" and doc["choices"][0]["message"] == {
        "role": "assistant", "content": "Hello from the mock model. You asked: " + "x" * 200}


def test_a_body_that_isn_t_a_chat_request_is_a_400():
    with TestClient(mock_gateway.app) as test:
        for content in (b"{not json", b"[]", b'{"messages": "x"}'):
            assert test.post("/v1/chat/completions", content=content, headers={"Authorization": "Bearer mock-key"}).status_code == 400


def test_the_runtime_never_imports_the_mock():
    package = Path(mock_gateway.__file__).parent
    for path in package.glob("*.py"):
        if path.name != "mock_gateway.py":
            assert not re.search(r"^\s*(from|import)\b.*\bmock_gateway\b", path.read_text(), re.M), path.name
