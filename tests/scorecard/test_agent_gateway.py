"""The gateway client, with a fake opener: no network."""

import http.client
import io
import json
import urllib.error

import pytest

from golden_road.io import InputError
from scorecard.agent.gateway import GatewayClient, GatewayError, credential, parse_completion

TOKEN = "gw-token-" + "x" * 20
REPLY = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
    {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "app.py"}'}}]}}],
    "usage": {"prompt_tokens": 90, "completion_tokens": 10, "total_tokens": 100}, "model": "whatever"}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    """Returns (or raises) one scripted outcome per call and records each request."""

    def __init__(self, *outcomes):
        self.outcomes, self.requests = list(outcomes), []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return Response(outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode())


def http_error(code):
    return urllib.error.HTTPError("https://gateway.example.invalid/v1/chat/completions", code, "no", {}, io.BytesIO(TOKEN.encode()))


def client(config, opener, sleeps=None):
    return GatewayClient(config, TOKEN, opener=opener, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_request_shape_and_bearer_auth(config):
    opener = Opener(REPLY)
    done = client(config, opener).complete([{"role": "user", "content": "hi"}], [{"type": "function"}], max_tokens=500)
    request, timeout = opener.requests[0]
    body = json.loads(request.data)
    assert body == {"model": "scorecard-reviewer", "messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "function"}],
                    "tool_choice": "auto", "temperature": 0, "max_tokens": 500}
    assert request.full_url == "https://gateway.example.invalid/v1/chat/completions" and request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}" and timeout == 120
    assert done.tokens == 100 and done.message["tool_calls"][0]["function"]["name"] == "read_file"


def test_api_key_header(config):
    config["agent"]["gateway"]["auth"] = {"scheme": "header", "header": "api-key"}
    opener = Opener(REPLY)
    client(config, opener).complete([], [], max_tokens=300)
    request = opener.requests[0][0]
    assert request.get_header("Api-key") == TOKEN and request.get_header("Authorization") is None


def test_one_retry_on_transient_errors(config):
    sleeps = []
    opener = Opener(http_error(503), REPLY)
    assert client(config, opener, sleeps).complete([], [], max_tokens=300).tokens == 100
    assert sleeps == [5] and len(opener.requests) == 2
    with pytest.raises(GatewayError, match="HTTP 429"):
        client(config, Opener(http_error(429), http_error(429))).complete([], [], max_tokens=300)
    with pytest.raises(GatewayError, match="could not be reached"):
        client(config, Opener(TimeoutError(), urllib.error.URLError("down"))).complete([], [], max_tokens=300)


@pytest.mark.parametrize("code, fatal", [(401, True), (403, True), (400, False), (302, False)])
def test_no_retry_on_other_errors_and_auth_is_fatal(config, code, fatal):
    opener = Opener(http_error(code))
    with pytest.raises(GatewayError) as caught:
        client(config, opener).complete([], [], max_tokens=300)
    assert caught.value.fatal is fatal and len(opener.requests) == 1
    assert TOKEN not in str(caught.value)


def test_oversized_response_is_refused(config):
    config["agent"]["gateway"]["max_response_bytes"] = 10_000
    with pytest.raises(GatewayError, match="larger than 10000 bytes"):
        client(config, Opener(b"{" + b" " * 20_000 + b"}")).complete([], [], max_tokens=300)


@pytest.mark.parametrize("body", [
    b"not json", b"[]", b'{"choices": []}', b'{"choices": [{"message": "hi"}]}',
    b'{"choices": [{"message": {"content": 5}}]}',
    b'{"choices": [{"message": {"content": null, "tool_calls": {"id": 1}}}]}',
    b'{"choices": [{"message": {"content": null, "tool_calls": [{"id": "c1", "function": {"name": 5}}]}}]}',
    b'{"choices": [{"message": {"content": null, "tool_calls": [{"function": {"name": "grep", "arguments": "{}"}}]}}]}',
])
def test_malformed_responses_are_gateway_errors(body):
    with pytest.raises(GatewayError):
        parse_completion(body)


def test_usage_is_optional():
    assert parse_completion(b'{"choices": [{"message": {"content": "done"}}]}').tokens is None
    assert parse_completion(b'{"choices": [{"message": {"content": "x"}}], "usage": {"total_tokens": -3}}').tokens is None


def test_credential_comes_from_the_named_variable(config):
    assert credential(config, {"SCORECARD_GATEWAY_TOKEN": f" {TOKEN}\n"}) == TOKEN
    for env in ({}, {"SCORECARD_GATEWAY_TOKEN": "  "}, {"SCORECARD_GATEWAY_TOKEN": "a\rb"}, {"SCORECARD_GATEWAY_TOKEN": "t\u00f6k"}):
        with pytest.raises(InputError):
            credential(config, env)


class Truncated(Response):
    def read(self, size=-1):
        raise http.client.IncompleteRead(TOKEN.encode(), 50)

    read1 = read


class TruncatingOpener(Opener):
    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.parametrize("failure", [
    http.client.IncompleteRead(TOKEN.encode(), 50), http.client.BadStatusLine(TOKEN), http.client.LineTooLong("header line"),
    http.client.RemoteDisconnected(TOKEN),
])
def test_malformed_http_replies_are_transient_gateway_errors(config, failure):
    sleeps = []
    opener = Opener(failure, failure)
    with pytest.raises(GatewayError, match="could not be reached") as caught:
        client(config, opener, sleeps).complete([], [], max_tokens=300)
    assert sleeps == [5] and len(opener.requests) == 2 and TOKEN not in str(caught.value)
    assert client(config, Opener(failure, REPLY)).complete([], [], max_tokens=300).tokens == 100


def test_a_reply_cut_off_while_reading_is_a_gateway_error(config):
    opener = TruncatingOpener(Truncated(), Truncated())
    with pytest.raises(GatewayError, match="IncompleteRead") as caught:
        client(config, opener).complete([], [], max_tokens=300)
    assert TOKEN not in str(caught.value) and len(opener.requests) == 2


def test_the_agent_loop_round_trips_through_the_http_client(config, make_app):
    """The OpenAI wire format end to end: a tool call, its tool reply on the next request, then the submission."""
    from scorecard.agent.plan import Budget
    from scorecard.agent.runner import run_batch
    from scorecard.agent.tools import RepoTools

    root = make_app({"app.py": "def get(note, user):\n    if note.owner != user:\n        raise Forbidden()\n"})
    anchor = {"file": "app.py", "start_line": 2, "snippet": "if note.owner != user:"}
    submission = {"results": [{"asvs_id": "v5.0.0-8.2.2", "status": "pass", "confidence": "high", "justification": "owner check",
                               "evidence": [anchor], "finding_refs": []}], "findings": []}
    second = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c2", "type": "function", "function": {"name": "submit_assessment", "arguments": json.dumps(submission)}}]}}],
        "usage": {"total_tokens": 50}}
    opener = Opener(REPLY, second)
    results, _ = run_batch(client(config, opener), RepoTools(root, config), Budget(config["agent"]["budget"]), system="SYS",
                           user="USER", batch_ids=["v5.0.0-8.2.2"], max_turns=4, now="2026-10-21T12:00:00Z")
    assert results[0]["evidence"] == [anchor]
    sent = json.loads(opener.requests[1][0].data)["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool"]
    assert sent[2]["tool_calls"][0]["id"] == "c1" and sent[3]["tool_call_id"] == "c1" and sent[3]["content"].startswith("app.py lines 1-3 of 3")
    assert [t["function"]["name"] for t in json.loads(opener.requests[0][0].data)["tools"]] == ["list_files", "read_file", "grep", "submit_assessment"]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Drip(Response):
    """Delivers one byte per read1 and moves the clock a second each time, like a server that never stalls long enough to trip
    the per-socket-operation timeout."""

    def __init__(self, data, clock):
        super().__init__(data)
        self.clock = clock

    def read1(self, size=-1):
        self.clock.now += 1
        return super().read1(1)

    read = read1


def test_a_slow_drip_is_cut_off_by_the_run_deadline(config):
    """M6: urllib's timeout is per socket operation; the body is read in chunks against the run's deadline."""
    clock = Clock()
    gateway = GatewayClient(config, TOKEN, opener=lambda request, timeout: Drip(json.dumps(REPLY).encode(), clock), sleep=lambda s: None, clock=clock)
    gateway.set_time_limit(5)
    with pytest.raises(GatewayError, match="time budget") as caught:
        gateway.complete([], [], max_tokens=300)
    assert caught.value.fatal is True and clock.now <= 107


def test_no_call_starts_or_retries_past_the_deadline(config):
    clock = Clock()
    opener = Opener(http_error(503), REPLY)
    gateway = GatewayClient(config, TOKEN, opener=opener, sleep=lambda s: None, clock=clock)
    gateway.set_time_limit(3)  # less than retry_seconds (5)
    with pytest.raises(GatewayError, match="time budget"):
        gateway.complete([], [], max_tokens=300)
    assert len(opener.requests) == 1
    gateway.set_time_limit(0)
    with pytest.raises(GatewayError, match="time budget"):
        gateway.complete([], [], max_tokens=300)
    assert len(opener.requests) == 1


def test_the_socket_timeout_never_exceeds_the_time_left(config):
    clock = Clock()
    opener = Opener(REPLY)
    gateway = GatewayClient(config, TOKEN, opener=opener, sleep=lambda s: None, clock=clock)
    gateway.set_time_limit(30)
    gateway.complete([], [], max_tokens=300)
    assert opener.requests[0][1] == 30  # config timeout_seconds is 120


def test_duplicate_tool_call_ids_are_a_gateway_error():
    call = {"id": "c1", "type": "function", "function": {"name": "grep", "arguments": "{}"}}
    body = json.dumps({"choices": [{"message": {"content": None, "tool_calls": [call, call]}}]}).encode()
    with pytest.raises(GatewayError, match="repeats a tool call id"):
        parse_completion(body)
