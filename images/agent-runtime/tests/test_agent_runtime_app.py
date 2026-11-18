"""The agent's routes (spec 2026-11-17 A3; decided while planning: items 22 to 24): the chat page under the portal's CSP, POST /chat and
/healthz, and one log line per request that never holds the text, a tool's result, its arguments or the key."""

import importlib.util
import logging
import re
from pathlib import Path

from runtime_fakes import AGENT
from starlette.testclient import TestClient

from agent_runtime.app import CSP, HEADERS, create_app
from agent_runtime.model import GatewayError
from agent_runtime.tools import Tool

ROOT = Path(__file__).resolve().parents[3]
HTML_SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML")  # the script writes text with textContent only (decision 23)


class Gateway:
    """Answers one tool call when asked about Thursday, else a reply; or fails."""

    def __init__(self, fail=None, call="mcp_calendar"):
        self.fail, self.call, self.seen = fail, call, []

    def complete(self, model, messages, tools):
        self.seen.append(messages)
        if self.fail:
            raise self.fail
        if messages[-1]["role"] == "user" and "Thursday" in messages[-1]["content"]:
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                                                                          "function": {"name": self.call, "arguments": '{"q": "SENTINEL-ARG"}'}}]}
        return {"role": "assistant", "content": "Thursday at 10 is free."}


def app(gateway=None):
    tool = Tool("mcp_calendar", "Read calendars.", {"type": "object"}, lambda a: {"free": ["Thursday 10:00 SENTINEL-RESULT"]})
    return TestClient(create_app(AGENT, "SYSTEM", [tool], gateway or Gateway(), new_id=lambda: "req-1"), raise_server_exceptions=False)


def test_chat_answers_with_the_reply_the_tools_and_the_request_id():
    answer = app().post("/chat", json={"messages": [{"role": "user", "content": "When is everyone free on Thursday?"}]})
    assert answer.status_code == 200
    assert answer.json() == {"reply": "Thursday at 10 is free.", "tool_calls": [{"tool": "mcp_calendar", "arguments": {"q": "SENTINEL-ARG"},
                                                                                  "ok": True}], "request_id": "req-1"}


def test_the_conversation_is_the_caller_s_and_the_system_prompt_comes_first():
    gateway = Gateway()
    app(gateway).post("/chat", json={"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                                                  {"role": "user", "content": "again"}]})
    assert [m["role"] for m in gateway.seen[0]] == ["system", "user", "assistant", "user"] and gateway.seen[0][0]["content"] == "SYSTEM"


def test_a_malformed_body_is_a_400():
    client = app()
    for bad in ({}, {"messages": []}, {"messages": [{"role": "system", "content": "x"}]}, {"messages": [{"role": "user", "content": 1}]},
                {"messages": [{"role": "user", "content": "x", "name": "n"}]}, {"messages": [{"role": "assistant", "content": "x"}]}, [],
                {"messages": [{"role": "user", "content": "x"}], "model": "other"}, {"messages": ["x"]}, {"messages": "x"},
                {"messages": [{"role": ["user"], "content": "x"}]}):
        answer = client.post("/chat", json=bad)
        assert answer.status_code == 400 and answer.json()["request_id"] == "req-1" and answer.json()["error"], bad
    for raw in (b"{not json", b"\xff\xfe"):
        assert client.post("/chat", content=raw, headers={"content-type": "application/json"}).status_code == 400


def test_a_gateway_failure_is_a_502_not_a_crash():
    answer = app(Gateway(fail=GatewayError("the gateway answered 503"))).post("/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert answer.status_code == 502 and answer.json() == {"error": "The model gateway did not answer. Try again.", "request_id": "req-1"}


def test_anything_else_is_a_500_with_the_request_id_and_its_log_line(caplog):
    caplog.set_level(logging.INFO, logger="agent_runtime")
    answer = app(Gateway(fail=RuntimeError("SENTINEL"))).post("/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert answer.status_code == 500 and answer.json() == {"error": "The agent failed. Try again.", "request_id": "req-1"}
    assert [r.getMessage() for r in caplog.records if r.name == "agent_runtime"] == [
        "request=req-1 person=- model=chat-default tools=- outcome=error"]
    assert "SENTINEL" not in caplog.text


def test_one_log_line_per_request_without_text_results_arguments_or_key(caplog):
    caplog.set_level(logging.INFO, logger="agent_runtime")
    app().post("/chat", headers={"Cf-Access-Authenticated-User-Email": "pat@example.com"},
               json={"messages": [{"role": "user", "content": "SENTINEL-QUESTION Thursday"}]})
    lines = [r.getMessage() for r in caplog.records if r.name == "agent_runtime"]
    assert lines == ["request=req-1 person=pat@example.com model=chat-default tools=mcp_calendar outcome=ok"]
    assert "SENTINEL" not in caplog.text


def test_a_refused_body_is_logged_once_too(caplog):
    caplog.set_level(logging.INFO, logger="agent_runtime")
    app().post("/chat", json={"messages": [{"role": "system", "content": "SENTINEL"}]})
    assert [r.getMessage() for r in caplog.records if r.name == "agent_runtime"] == [
        "request=req-1 person=- model=chat-default tools=- outcome=refused"]


def test_a_tool_name_the_model_made_up_is_not_logged(caplog):
    caplog.set_level(logging.INFO, logger="agent_runtime")
    answer = app(Gateway(call="SENTINEL tool\nrequest=forged")).post("/chat", json={"messages": [{"role": "user", "content": "Thursday"}]})
    assert answer.json()["tool_calls"][0]["ok"] is False
    assert caplog.records[-1].getMessage() == "request=req-1 person=- model=chat-default tools=unknown outcome=ok"


def test_a_person_header_that_isn_t_an_email_is_not_logged(caplog):
    caplog.set_level(logging.INFO, logger="agent_runtime")
    app().post("/chat", headers={"Cf-Access-Authenticated-User-Email": "not-an-email person=admin"}, json={"messages": [{"role": "user", "content": "hi"}]})
    assert "person=- " in caplog.records[-1].getMessage()


STYLE_ATTR = re.compile(r"<[a-zA-Z][^>]*\sstyle\s*=", re.S)
HANDLER = re.compile(r"<[a-zA-Z][^>]*\son[a-z]+\s*=", re.S)


def test_the_chat_page_names_the_agent_and_its_starters_under_the_csp():
    client = app()
    page = client.get("/")
    assert page.status_code == 200 and page.headers["content-security-policy"] == CSP
    html = page.text
    assert "<h1>Calendar helper</h1>" in html and "When is everyone free on Thursday?" in html
    assert not STYLE_ATTR.search(html) and not HANDLER.search(html) and "<style" not in html
    assert re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S) == [""]
    assert 'src="/static/chat.js"' in html
    for path, kind in (("/static/chat.js", "text/javascript"), ("/static/chat.css", "text/css")):
        answer = client.get(path)
        assert answer.status_code == 200 and answer.headers["content-type"].startswith(kind)
    script = client.get("/static/chat.js").text
    assert "textContent" in script and not [sink for sink in HTML_SINKS if sink in script]


def test_the_csp_is_the_portal_s():
    spec = importlib.util.spec_from_file_location("portal_headers", ROOT / "bootstrap/golden-road-portal/portal/web/headers.py")
    portal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(portal)
    assert CSP == portal.CSP
    assert {k.decode(): v.decode() for k, v in portal.HEADERS}.items() <= HEADERS.items()


def test_text_from_agent_yaml_is_escaped():
    page = TestClient(create_app({**AGENT, "display_name": "<b>x</b>", "starters": ["<script>alert(1)</script>"]}, "s", [], Gateway())).get("/")
    assert "<b>x</b>" not in page.text and "&lt;script&gt;" in page.text


def test_healthz_answers_and_every_answer_has_the_headers():
    client = app()
    for answer in (client.get("/healthz"), client.get("/nowhere"), client.post("/chat", json={})):
        assert answer.headers["x-content-type-options"] == "nosniff" and answer.headers["content-security-policy"] == CSP
        assert answer.headers["cache-control"] == "no-store" and answer.headers["x-frame-options"] == "DENY"
    assert client.get("/healthz").json() == {"ok": True}


def test_a_tool_s_error_words_go_to_the_model_and_never_to_the_log(caplog):
    """M5: an error's text (a Databricks message, say) may echo data values; the log line names tools and the outcome only."""
    from agent_runtime.tools import ToolError

    def broken(arguments):
        raise ToolError("the statement ended FAILED: value SENTINEL-DATA is not a date")

    caplog.set_level(logging.DEBUG)
    gateway = Gateway()
    tool = Tool("mcp_calendar", "Read calendars.", {"type": "object"}, broken)
    client = TestClient(create_app(AGENT, "SYSTEM", [tool], gateway, new_id=lambda: "req-1"))
    answer = client.post("/chat", json={"messages": [{"role": "user", "content": "Thursday"}]})
    assert answer.json()["tool_calls"] == [{"tool": "mcp_calendar", "arguments": {"q": "SENTINEL-ARG"}, "ok": False}]
    assert "SENTINEL-DATA" in gateway.seen[-1][-1]["content"]  # the model is told why
    assert [r.getMessage() for r in caplog.records if r.name == "agent_runtime"] == [
        "request=req-1 person=- model=chat-default tools=mcp_calendar outcome=ok"]
    assert "SENTINEL" not in caplog.text


def lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "agent_runtime"]


def test_deeply_nested_json_is_a_400_with_its_line_and_the_headers(caplog):
    """I1: the JSON parser's RecursionError is a bad body, not a bare 500."""
    caplog.set_level(logging.INFO, logger="agent_runtime")
    answer = app().post("/chat", content=b"[" * 200_000 + b"]" * 200_000, headers={"content-type": "application/json"})
    assert answer.status_code == 400 and answer.json()["request_id"] == "req-1" and answer.headers["content-security-policy"] == CSP
    assert lines(caplog) == ["request=req-1 person=- model=chat-default tools=- outcome=refused"]


def test_a_failure_anywhere_in_the_handler_still_logs_its_line_and_has_the_headers(caplog, monkeypatch):
    import agent_runtime.app as module

    def broken(body):
        raise RuntimeError("SENTINEL")

    monkeypatch.setattr(module, "conversation", broken)
    caplog.set_level(logging.INFO, logger="agent_runtime")
    answer = app().post("/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert answer.status_code == 500 and answer.json() == {"error": "The agent failed. Try again.", "request_id": "req-1"}
    assert answer.headers["content-security-policy"] == CSP and answer.headers["cache-control"] == "no-store"
    assert lines(caplog) == ["request=req-1 person=- model=chat-default tools=- outcome=error"] and "SENTINEL" not in caplog.text


def test_only_a_json_body_is_read():
    """I2: a form or text POST needs no preflight, so a cross-site page could send one; only application/json is read (415)."""
    client = app()
    body = b'{"messages": [{"role": "user", "content": "hi"}]}'
    for kind in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "", "application/jsonx"):
        answer = client.post("/chat", content=body, headers={"content-type": kind} if kind else {})
        assert answer.status_code == 415 and answer.json()["request_id"] == "req-1", kind
    for kind in ("application/json", "application/json; charset=utf-8", "Application/JSON"):
        assert client.post("/chat", content=body, headers={"content-type": kind}).status_code == 200, kind


def test_another_origin_is_refused(caplog):
    """I2: a browser names the page's origin; another site's (or an opaque "null") is a 403. The scheme isn't compared: the edge
    ends TLS, so the runtime sees http while the browser says https."""
    caplog.set_level(logging.INFO, logger="agent_runtime")
    client = app()
    chat = {"messages": [{"role": "user", "content": "hi"}]}
    for origin in ("https://evil.example.test", "null", "https://testserver.evil.example.test", "https://testserver:8443", "x"):
        answer = client.post("/chat", json=chat, headers={"origin": origin})
        assert answer.status_code == 403 and answer.json()["request_id"] == "req-1", origin
    assert lines(caplog)[-1] == "request=req-1 person=- model=chat-default tools=- outcome=refused"
    for origin in ("http://testserver", "https://testserver", "HTTPS://TestServer"):
        assert client.post("/chat", json=chat, headers={"origin": origin}).status_code == 200, origin
    assert client.post("/chat", json=chat).status_code == 200  # no Origin: not a browser's cross-site request


def test_a_lone_surrogate_is_a_400():
    """M1: a lone surrogate isn't Unicode text; it would fail later, encoding the request to the gateway."""
    for text in ('"\\ud800"', '"a\\udfffb"'):
        raw = ('{"messages": [{"role": "user", "content": %s}]}' % text).encode()
        assert app().post("/chat", content=raw, headers={"content-type": "application/json"}).status_code == 400, text
    raw = '{"messages": [{"role": "user", "content": "\\ud83d\\ude00 ok"}]}'.encode()  # a pair is one emoji, and fine
    assert app().post("/chat", content=raw, headers={"content-type": "application/json"}).status_code == 200
