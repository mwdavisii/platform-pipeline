"""The gateway client and the tool loop (spec 2026-11-17 A9, A10; decided while planning: items 15, 19): the loop runs until the model
stops calling tools, each tool's result goes back as data, and a tool's failure (or a call the model got wrong) is the tool's result."""

import json

import httpx
import pytest
from runtime_fakes import body, client
from starlette.testclient import TestClient

from agent_runtime import mock_gateway
from agent_runtime.loop import DATA_NOTE, run
from agent_runtime.model import Gateway, GatewayError
from agent_runtime.tools import Tool, ToolError


def mock():
    """The mock gateway's app, served through httpx: the loop runs against the same answers the docker test sees."""
    test = TestClient(mock_gateway.app, base_url="https://llm.example.org")

    def handle(request):
        answer = test.post(request.url.path, content=request.content, headers=dict(request.headers))
        return httpx.Response(answer.status_code, json=answer.json())

    http, recorder = client(handle)
    return Gateway("https://llm.example.org/v1", "mock-key", http), recorder


def calendar_tool(result=None, error=None):
    calls = []

    def call(arguments):
        calls.append(arguments)
        if error:
            raise error if isinstance(error, Exception) else ToolError(error)
        return result or {"free": ["Thursday 10:00"]}

    return Tool("mcp_calendar", "Read calendars.", {"type": "object"}, call), calls


def scripted(*answers):
    """A gateway that answers each turn with the next message (the OpenAI shape), and records what it was sent."""
    turns = iter(answers)
    http, recorder = client(lambda r: httpx.Response(200, json={"choices": [{"index": 0, "message": next(turns), "finish_reason": "stop"}]}))
    return Gateway("https://llm.example.org/v1", "k", http), recorder


def calling(*calls):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": a}} for i, (n, a) in enumerate(calls)]}


def test_a_plain_question_is_answered_with_no_tool():
    gateway, _ = mock()
    reply, used = run(gateway, "chat-default", "Be brief.", [], [{"role": "user", "content": "Hello there"}])
    assert reply == "Hello from the mock model. You asked: Hello there" and used == []


def test_a_named_tool_is_called_its_result_given_as_data_and_the_model_answers():
    gateway, recorder = mock()
    tool, calls = calendar_tool()
    reply, used = run(gateway, "chat-default", "Be brief.", [tool], [{"role": "user", "content": "Use mcp_calendar for Thursday"}])
    assert reply == "I used mcp_calendar: the tool answered." and used == [{"tool": "mcp_calendar", "arguments": {}, "ok": True}]
    assert calls == [{}]
    second = body(recorder.requests[1])["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]
    assert json.loads(second[-1]["content"]) == {"data": {"free": ["Thursday 10:00"]}, "note": DATA_NOTE}
    assert second[-1]["tool_call_id"] == second[2]["tool_calls"][0]["id"]
    assert body(recorder.requests[0])["tools"][0] == {"type": "function", "function": {"name": "mcp_calendar", "description": "Read calendars.",
                                                                                         "parameters": {"type": "object"}}}


def test_the_arguments_reach_the_tool_and_the_answer():
    gateway, _ = scripted(calling(("mcp_calendar", '{"day": "Thursday"}')), {"role": "assistant", "content": "Free at 10."})
    tool, calls = calendar_tool()
    assert run(gateway, "m", "s", [tool], [{"role": "user", "content": "x"}]) == (
        "Free at 10.", [{"tool": "mcp_calendar", "arguments": {"day": "Thursday"}, "ok": True}])
    assert calls == [{"day": "Thursday"}]


def test_a_tool_error_is_the_tool_s_result_not_a_failure():
    gateway, recorder = mock()
    tool, _ = calendar_tool(error="the calendar is down")
    reply, used = run(gateway, "chat-default", "s", [tool], [{"role": "user", "content": "mcp_calendar please"}])
    assert used == [{"tool": "mcp_calendar", "arguments": {}, "ok": False}] and reply.startswith("I used mcp_calendar")
    assert json.loads(body(recorder.requests[1])["messages"][-1]["content"]) == {"error": "the calendar is down", "note": DATA_NOTE}


def test_a_tool_that_breaks_is_a_tool_error_naming_only_the_kind_of_failure():
    gateway, recorder = scripted(calling(("mcp_calendar", "{}")), {"role": "assistant", "content": "Sorry."})
    tool, _ = calendar_tool(error=RuntimeError("secret-SENTINEL in a message"))
    assert run(gateway, "m", "s", [tool], [{"role": "user", "content": "x"}])[1] == [{"tool": "mcp_calendar", "arguments": {}, "ok": False}]
    sent = body(recorder.requests[1])["messages"][-1]["content"]
    assert json.loads(sent) == {"error": "the tool failed (RuntimeError)", "note": DATA_NOTE} and "SENTINEL" not in sent


def test_an_unknown_tool_or_bad_arguments_are_tool_errors_too():
    gateway, recorder = scripted(calling(("drop_tables", "{}"), ("mcp_calendar", "{not json"), ("mcp_calendar", "[1]")),
                                 {"role": "assistant", "content": "Done."})
    tool, calls = calendar_tool()
    reply, used = run(gateway, "m", "s", [tool], [{"role": "user", "content": "x"}])
    assert reply == "Done." and calls == []
    assert used == [{"tool": "drop_tables", "arguments": {}, "ok": False}, {"tool": "mcp_calendar", "arguments": {}, "ok": False},
                    {"tool": "mcp_calendar", "arguments": {}, "ok": False}]
    sent = body(recorder.requests[1])["messages"]
    errors = [json.loads(m["content"])["error"] for m in sent if m["role"] == "tool"]
    assert errors == ["there is no tool named drop_tables", "the arguments are not a JSON object", "the arguments are not a JSON object"]
    assert [m["tool_call_id"] for m in sent if m["role"] == "tool"] == ["c0", "c1", "c2"]


def test_a_call_with_no_function_is_a_tool_error():
    gateway, _ = scripted({"role": "assistant", "content": None, "tool_calls": [{"id": "c0", "type": "function"}, "junk"]},
                          {"role": "assistant", "content": "Done."})
    assert run(gateway, "m", "s", [], [{"role": "user", "content": "x"}]) == (
        "Done.", [{"tool": "", "arguments": {}, "ok": False}, {"tool": "", "arguments": {}, "ok": False}])


def test_the_loop_has_no_turn_cap():
    """A model that calls a tool 50 times is answered 50 times (A10): only the model stops the loop."""
    gateway, _ = scripted(*[calling(("mcp_calendar", "{}")) for _ in range(50)], {"role": "assistant", "content": "Finally."})
    tool, calls = calendar_tool()
    assert run(gateway, "m", "s", [tool], [{"role": "user", "content": "x"}])[0] == "Finally."
    assert len(calls) == 50


def test_the_gateway_is_called_with_the_key_and_no_limits():
    gateway, recorder = mock()
    run(gateway, "chat-default", "s", [], [{"role": "user", "content": "hi"}])
    sent = recorder.requests[0]
    assert str(sent.url) == "https://llm.example.org/v1/chat/completions" and sent.headers["authorization"] == "Bearer mock-key"
    assert set(body(sent)) == {"model", "messages"}  # no tools when none are bound; never max_tokens


def test_the_caller_s_conversation_is_not_changed():
    gateway, _ = scripted(calling(("mcp_calendar", "{}")), {"role": "assistant", "content": "Done."})
    tool, _ = calendar_tool()
    conversation = [{"role": "user", "content": "x"}]
    run(gateway, "m", "s", [tool], conversation)
    assert conversation == [{"role": "user", "content": "x"}]


def refused(request):
    raise httpx.ConnectError("refused")


@pytest.mark.parametrize("handler", [lambda r: httpx.Response(500, json={}), lambda r: httpx.Response(200, json={"choices": []}),
                                     lambda r: httpx.Response(200, text="x"), lambda r: httpx.Response(200, json={"choices": [{"message": "x"}]}),
                                     lambda r: httpx.Response(200, json={"choices": [{"message": {"content": 1}}]}),
                                     lambda r: httpx.Response(200, json={"choices": [{"message": {"tool_calls": {}}}]}), refused])
def test_a_gateway_that_won_t_answer_is_a_gateway_error(handler):
    http, _ = client(handler)
    with pytest.raises(GatewayError):
        run(Gateway("https://llm.example.org/v1", "k", http), "m", "s", [], [{"role": "user", "content": "x"}])


def test_a_gateway_error_never_holds_the_key():
    http, _ = client(lambda r: httpx.Response(401, json={"error": {"message": "bad key sk-SENTINEL"}}))
    with pytest.raises(GatewayError) as caught:
        Gateway("https://llm.example.org/v1", "sk-SENTINEL", http).complete("m", [], [])
    assert "SENTINEL" not in str(caught.value) and "401" in str(caught.value)


def test_the_gateway_never_follows_a_redirect():
    """Even on a client that would: the key would go with it (M6)."""
    recorder = []

    def handle(request):
        recorder.append(request)
        return httpx.Response(307, headers={"location": "https://elsewhere.example.test/v1/chat/completions"})

    http = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True)
    with pytest.raises(GatewayError) as caught:
        Gateway("https://llm.example.org/v1", "k", http).complete("m", [], [])
    assert len(recorder) == 1 and "307" in str(caught.value)


def test_tool_content_is_ascii_json_and_the_call_id_is_text_or_none():
    """M1, M4: a tool's text (a lone surrogate included) is sent escaped; a call id that isn't text is sent as null."""
    gateway, recorder = scripted({"role": "assistant", "content": None, "tool_calls": [
        {"id": 7, "type": "function", "function": {"name": "mcp_calendar", "arguments": "{}"}},
        {"type": "function", "function": {"name": "mcp_calendar", "arguments": "{}"}},
        {"id": "c2", "type": "function", "function": {"name": "mcp_calendar", "arguments": "{}"}}]}, {"role": "assistant", "content": "Done."})
    tool, _ = calendar_tool(result={"text": "café \ud800"})
    run(gateway, "m", "s", [tool], [{"role": "user", "content": "x"}])
    sent = [m for m in body(recorder.requests[1])["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in sent] == [None, None, "c2"]
    assert all(m["content"].isascii() for m in sent) and json.loads(sent[0]["content"])["data"] == {"text": "café \ud800"}
