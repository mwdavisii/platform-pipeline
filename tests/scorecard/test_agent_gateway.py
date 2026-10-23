"""The gateway client, with a fake opener: no network."""

import email.message
import http.client
import io
import json
import traceback
import urllib.error

import pytest

from builder import VERSION
from golden_road.io import InputError
from scorecard.agent.gateway import MODEL_ENV, GatewayClient, GatewayError, access_headers, credential, gateway_model, parse_completion

TOKEN = "gw-token-" + "x" * 20
WIRE = "acme-sensitive-deploy-7"
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
    return GatewayClient(config, TOKEN, model=WIRE, opener=opener, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_request_shape_and_bearer_auth(config):
    opener = Opener(REPLY)
    done = client(config, opener).complete([{"role": "user", "content": "hi"}], [{"type": "function"}], max_tokens=500)
    request, timeout = opener.requests[0]
    body = json.loads(request.data)
    assert body == {"model": WIRE, "messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "function"}],
                    "tool_choice": "auto", "temperature": 0, "max_tokens": 500}
    assert request.full_url == "https://gateway.example.invalid/v1/chat/completions" and request.get_method() == "POST"
    assert request.get_header("User-agent") == f"golden-road-scorecard/{VERSION}"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}" and timeout == 120
    assert done.tokens == 100 and done.message["tool_calls"][0]["function"]["name"] == "read_file"


def test_temperature_is_configurable_and_null_omits_it(config):
    for configured in (0.7, 0):
        config["agent"]["model"]["temperature"] = configured
        opener = Opener(REPLY)
        client(config, opener).complete([], [], max_tokens=300)
        assert json.loads(opener.requests[0][0].data)["temperature"] == configured
    config["agent"]["model"]["temperature"] = None
    opener = Opener(REPLY)
    client(config, opener).complete([], [], max_tokens=300)
    assert "temperature" not in json.loads(opener.requests[0][0].data)


def test_a_non_finite_temperature_is_never_sent(config):
    config["agent"]["model"]["temperature"] = float("nan")
    with pytest.raises(ValueError):
        client(config, Opener(REPLY)).complete([], [], max_tokens=300)


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


@pytest.mark.parametrize("code, fatal", [(401, True), (403, True), (400, False), (302, True), (301, True), (307, True)])
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
    gateway = GatewayClient(config, TOKEN, model=WIRE, opener=lambda request, timeout: Drip(json.dumps(REPLY).encode(), clock), sleep=lambda s: None, clock=clock)
    gateway.set_time_limit(5)
    with pytest.raises(GatewayError, match="time budget") as caught:
        gateway.complete([], [], max_tokens=300)
    assert caught.value.fatal is True and clock.now <= 107


def test_no_call_starts_or_retries_past_the_deadline(config):
    clock = Clock()
    opener = Opener(http_error(503), REPLY)
    gateway = GatewayClient(config, TOKEN, model=WIRE, opener=opener, sleep=lambda s: None, clock=clock)
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
    gateway = GatewayClient(config, TOKEN, model=WIRE, opener=opener, sleep=lambda s: None, clock=clock)
    gateway.set_time_limit(30)
    gateway.complete([], [], max_tokens=300)
    assert opener.requests[0][1] == 30  # config timeout_seconds is 120


def test_without_a_run_deadline_each_call_still_gets_the_socket_timeout(config):
    clock = Clock()
    opener = Opener(REPLY, http_error(503), REPLY)
    gateway = GatewayClient(config, TOKEN, model=WIRE, opener=opener, sleep=lambda s: None, clock=clock)
    gateway.complete([], [], max_tokens=300)
    clock.now += 10**9
    gateway.complete([], [], max_tokens=300)  # a retry too, however late the clock
    assert [r[1] for r in opener.requests] == [120, 120, 120]


def test_duplicate_tool_call_ids_are_a_gateway_error():
    call = {"id": "c1", "type": "function", "function": {"name": "grep", "arguments": "{}"}}
    body = json.dumps({"choices": [{"message": {"content": None, "tool_calls": [call, call]}}]}).encode()
    with pytest.raises(GatewayError, match="repeats a tool call id"):
        parse_completion(body)


ACCESS_ID = "sentinelid0123456789abcdef.access"
ACCESS_SECRET = "sentinelsecret" + "ab" * 24
ACCESS_ENV = {"SCORECARD_GATEWAY_ACCESS_ID": ACCESS_ID, "SCORECARD_GATEWAY_ACCESS_SECRET": ACCESS_SECRET}


def test_access_headers_are_sent_on_every_request_when_both_are_set(config):
    opener = Opener(http_error(503), REPLY)
    GatewayClient(config, TOKEN, model=WIRE, access=access_headers(ACCESS_ENV), opener=opener, sleep=lambda s: None).complete([], [], max_tokens=300)
    assert len(opener.requests) == 2
    for request, _ in opener.requests:
        assert request.get_header("Cf-access-client-id") == ACCESS_ID
        assert request.get_header("Cf-access-client-secret") == ACCESS_SECRET
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"


def test_no_access_headers_when_neither_is_set(config):
    assert access_headers({}) is None and access_headers({"SCORECARD_GATEWAY_ACCESS_ID": " ", "SCORECARD_GATEWAY_ACCESS_SECRET": ""}) is None
    opener = Opener(REPLY)
    client(config, opener).complete([], [], max_tokens=300)
    request = opener.requests[0][0]
    assert request.get_header("Cf-access-client-id") is None and request.get_header("Cf-access-client-secret") is None


@pytest.mark.parametrize("env", [{"SCORECARD_GATEWAY_ACCESS_ID": ACCESS_ID}, {"SCORECARD_GATEWAY_ACCESS_SECRET": ACCESS_SECRET}])
def test_one_access_value_alone_is_a_configuration_error(env):
    with pytest.raises(InputError, match="both") as caught:
        access_headers(env)
    assert ACCESS_ID not in str(caught.value) and ACCESS_SECRET not in str(caught.value)


def test_access_values_shorter_than_16_characters_are_refused_without_echoing_them():
    short = "a" * 15
    for env in ({**ACCESS_ENV, "SCORECARD_GATEWAY_ACCESS_ID": short}, {**ACCESS_ENV, "SCORECARD_GATEWAY_ACCESS_SECRET": short}):
        with pytest.raises(InputError) as caught:
            access_headers(env)
        assert short not in str(caught.value)
    assert access_headers({**ACCESS_ENV, "SCORECARD_GATEWAY_ACCESS_ID": "a" * 16}) is not None


@pytest.mark.parametrize("bad", ["a\rb", "a\nb", "a b", "a=b", "t\u00f6k", "a:b"])
def test_access_values_with_unexpected_characters_are_refused_without_echoing_them(bad):
    for env in ({**ACCESS_ENV, "SCORECARD_GATEWAY_ACCESS_ID": bad}, {**ACCESS_ENV, "SCORECARD_GATEWAY_ACCESS_SECRET": bad}):
        with pytest.raises(InputError) as caught:
            access_headers(env)
        assert bad not in str(caught.value) and repr(bad) not in str(caught.value)


def test_access_values_never_appear_in_errors_reprs_or_exceptions(config):
    access = access_headers(ACCESS_ENV)
    gateway = GatewayClient(config, TOKEN, model=WIRE, access=access, opener=Opener(http_error(403)))
    for text in (repr(access), str(access), repr(gateway), str(gateway), repr(vars(gateway))):
        assert ACCESS_ID not in text and ACCESS_SECRET not in text
    assert dict(access)["CF-Access-Client-Secret"] == ACCESS_SECRET, "the headers are still real; only their repr is redacted"
    for outcome in (http_error(401), http_error(503), http_error(302), OSError(ACCESS_SECRET), b"not json", {"choices": []}):
        gateway = GatewayClient(config, TOKEN, model=WIRE, access=access, opener=Opener(outcome, outcome), sleep=lambda s: None)
        with pytest.raises(GatewayError) as caught:
            gateway.complete([], [], max_tokens=300)
        shown = "".join(traceback.format_exception(caught.value))  # what a log would show (the suppressed context is not shown)
        for text in (str(caught.value), repr(caught.value), shown):
            assert ACCESS_ID not in text and ACCESS_SECRET not in text


def test_a_redirect_is_never_followed_and_is_fatal(config):
    import http.server
    import threading

    from golden_road.checks import _open

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        config["agent"]["gateway"]["url"] = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        gateway = GatewayClient(config, TOKEN, model=WIRE, access=access_headers(ACCESS_ENV), opener=_open)
        with pytest.raises(GatewayError) as caught:
            gateway.complete([], [], max_tokens=300)
    finally:
        server.shutdown()
    assert caught.value.fatal and seen == ["/v1/chat/completions"]
    assert ACCESS_SECRET not in str(caught.value)


def test_the_body_model_is_the_secret_not_the_config(config):
    config["agent"]["model"]["name"] = "config-name-unused"
    opener = Opener(REPLY)
    client(config, opener).complete([], [], max_tokens=300)
    assert json.loads(opener.requests[0][0].data)["model"] == WIRE


def test_gateway_model_is_required_and_trimmed():
    assert gateway_model({MODEL_ENV: f" {WIRE}\n"}) == WIRE
    for env in ({}, {MODEL_ENV: ""}, {MODEL_ENV: "  "}):
        with pytest.raises(InputError, match="SCORECARD_GATEWAY_MODEL is not set"):
            gateway_model(env)


@pytest.mark.parametrize("bad", [".", "..", "a b", "a/b", "a%2fb", "{model}", "a?b", "a#b", "a\nb", "a\rb", "tök", "a:b", "a" * 129])
def test_a_gateway_model_outside_the_charset_is_refused_without_echoing_it(bad, config):
    with pytest.raises(InputError, match="not a valid model name") as caught:
        gateway_model({MODEL_ENV: bad})
    assert repr(bad) not in str(caught.value) and (len(bad.strip()) < 3 or bad.strip() not in str(caught.value))
    with pytest.raises(InputError, match="not a valid model name"):
        GatewayClient(config, TOKEN, model=bad)


def test_the_longest_gateway_model_is_accepted():
    assert gateway_model({MODEL_ENV: "a" * 128}) == "a" * 128


def test_the_gateway_model_never_shows_in_a_repr(config):
    model = gateway_model({MODEL_ENV: WIRE})
    gateway = GatewayClient(config, TOKEN, model=model)
    assert model == WIRE and json.dumps({"m": model}) == f'{{"m": "{WIRE}"}}', "the value itself is intact"
    for text in (repr(model), repr(gateway), str(gateway), repr(vars(gateway))):
        assert WIRE not in text and TOKEN not in text
    assert gateway.token == TOKEN, "the token itself is intact"


def test_a_wire_model_that_is_literally_in_the_url_template_or_the_version_is_refused_without_echoing_it(config):
    config["agent"]["gateway"]["url"] = f"https://gateway.example.invalid/openai/deployments/{WIRE}/chat/completions"
    with pytest.raises(InputError, match="must not contain the model name") as caught:
        GatewayClient(config, TOKEN, model=WIRE)
    assert WIRE not in str(caught.value)


def test_a_wire_model_that_is_in_the_model_version_is_refused_without_echoing_it(config):
    config["agent"]["model"]["version"] = f"v-{WIRE}"
    with pytest.raises(InputError, match="must not contain the model name") as caught:
        GatewayClient(config, TOKEN, model=WIRE)
    assert WIRE not in str(caught.value)


APIM_URL = "https://gateway.example.invalid/openai/deployments/{model}/chat/completions?api-version=2024-10-21"


def test_the_model_placeholder_is_filled_from_the_secret(config):
    config["agent"]["gateway"]["url"] = APIM_URL
    opener = Opener(http_error(503), REPLY)
    client(config, opener).complete([], [], max_tokens=300)
    for request, _ in opener.requests:  # the retry too
        assert request.full_url == f"https://gateway.example.invalid/openai/deployments/{WIRE}/chat/completions?api-version=2024-10-21"
        assert json.loads(request.data)["model"] == WIRE
    assert config["agent"]["gateway"]["url"] == APIM_URL, "the config is not changed"


def test_a_url_without_the_placeholder_is_used_as_is(config):
    opener = Opener(REPLY)
    client(config, opener).complete([], [], max_tokens=300)
    assert opener.requests[0][0].full_url == "https://gateway.example.invalid/v1/chat/completions"


def test_the_filled_url_never_shows_in_a_repr(config):
    config["agent"]["gateway"]["url"] = APIM_URL
    gateway = client(config, Opener())
    assert WIRE in gateway.url and WIRE not in repr(gateway.url) and WIRE not in repr(vars(gateway))


ECHO = json.dumps({"error": {"code": "DeploymentNotFound", "message": f"The API deployment {WIRE} does not exist"}})


def echoing_error(code):
    """An HTTP error the way APIM sends one: the URL, the reason and the body all name the deployment."""
    return urllib.error.HTTPError(f"https://gateway.example.invalid/openai/deployments/{WIRE}/chat/completions", code, f"deployment {WIRE}", {},
                                  io.BytesIO(ECHO.encode()))


def test_the_gateway_model_never_appears_in_an_error_or_a_traceback(config):
    config["agent"]["gateway"]["url"] = APIM_URL
    outcomes = [echoing_error(c) for c in (302, 400, 401, 403, 404, 429, 503)]
    outcomes += [urllib.error.URLError(WIRE), OSError(WIRE), TimeoutError(WIRE), http.client.InvalidURL(WIRE), ValueError(WIRE), ECHO.encode(),
                 json.dumps({"model": WIRE, "choices": []}).encode(), json.dumps({"model": WIRE, "choices": [{"message": {"content": 7}}]}).encode()]
    for outcome in outcomes:
        gateway = GatewayClient(config, TOKEN, model=WIRE, opener=Opener(outcome, outcome), sleep=lambda s: None)
        with pytest.raises(GatewayError) as caught:
            gateway.complete([], [], max_tokens=300)
        shown = "".join(traceback.format_exception(caught.value))
        for text in (str(caught.value), repr(caught.value), shown):
            assert WIRE not in text, (outcome, text)


def test_a_reply_that_echoes_the_model_is_reduced_to_the_message(config):
    done = client(config, Opener({**REPLY, "model": WIRE})).complete([], [], max_tokens=300)
    assert WIRE not in repr(done)


def test_the_user_agent_on_the_wire_is_the_scorecard_s_not_urllib_s(config):
    """Cloudflare's Browser Integrity Check refuses Python-urllib/3.x (error 1010)."""
    import http.server
    import threading

    from golden_road.checks import _open

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.headers.get_all("User-Agent"))
            body = json.dumps(REPLY).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        config["agent"]["gateway"]["url"] = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        GatewayClient(config, TOKEN, model=WIRE, access=access_headers(ACCESS_ENV), opener=_open).complete([], [], max_tokens=300)
    finally:
        server.shutdown()
    assert seen == [[f"golden-road-scorecard/{VERSION}"]]


def layer_error(code, headers=None, body=b"", length=True):
    msg = email.message.Message()
    if length and body:
        msg["Content-Length"] = str(len(body))
    for name, value in (headers or {}).items():
        msg[name] = value
    return urllib.error.HTTPError("https://gateway.example.invalid/v1/chat/completions", code, "no", msg, io.BytesIO(body))


@pytest.mark.parametrize("code,headers,body,layer", [
    (403, {"cf-access-domain": "acme.cloudflareaccess.com"}, b"", "Cloudflare Access"),
    (401, {"CF-Access-Aud": "a" * 64, "server": "cloudflare"}, b"error code: 1010", "Cloudflare Access"),
    (302, {"cf-access-domain": "acme.cloudflareaccess.com", "location": "https://acme.cloudflareaccess.com/login"}, b"", "Cloudflare Access"),
    (403, {"server": "cloudflare"}, b"error code: 1010", "Cloudflare edge, error 1010"),
    (403, {"Server": "Cloudflare"}, b"error code: 1020\n", "Cloudflare edge, error 1020"),
    (403, {"server": "cloudflare"}, b"<html>Attention required</html>", "origin"),
    (403, {"server": "cloudflare"}, b"error code: 10100", "origin"),
    (403, {"server": "cloudflare"}, b"error code: 1x10", "origin"),
    (403, {"server": "cloudflare"}, b"error code: 1010 and more text", "origin"),
    (403, {"server": "nginx"}, b"error code: 1010", "origin"),
    (403, {}, b"error code: 1010", "origin"),
    (503, {}, b"", "origin"),
])
def test_an_http_error_names_the_refusing_layer(config, code, headers, body, layer):
    with pytest.raises(GatewayError) as caught:
        client(config, Opener(layer_error(code, headers, body), layer_error(code, headers, body))).complete([], [], max_tokens=300)
    assert str(caught.value) == f"the gateway returned HTTP {code} ({layer})"


def test_the_layer_never_echoes_a_header_value_or_the_body(config):
    headers = {"cf-access-domain": f"{WIRE}.cloudflareaccess.com", "cf-ray": WIRE, "server": f"cloudflare {WIRE}"}
    for hdrs in (headers, {"server": "cloudflare", "cf-ray": WIRE}, {"server": f"cloudflare-{WIRE}"}):
        for body in (f"error code: {WIRE}".encode(), ECHO.encode(), TOKEN.encode()):
            with pytest.raises(GatewayError) as caught:
                client(config, Opener(layer_error(403, hdrs, body))).complete([], [], max_tokens=300)
            shown = "".join(traceback.format_exception(caught.value))
            assert WIRE not in shown and TOKEN not in shown and "cloudflareaccess" not in shown


def test_an_unreadable_error_body_is_the_origin(config):
    class Broken(io.BytesIO):
        def read(self, *args):
            raise OSError(WIRE)

    error = urllib.error.HTTPError("https://gateway.example.invalid/v1/chat/completions", 403, "no", layer_error(403, {"server": "cloudflare"}).headers, Broken())
    with pytest.raises(GatewayError, match=r"^the gateway returned HTTP 403 \(origin\)$"):
        client(config, Opener(error)).complete([], [], max_tokens=300)


def test_the_retry_and_fatal_rules_are_unchanged_by_the_layer(config):
    sleeps = []
    assert client(config, Opener(layer_error(503, {"server": "cloudflare"}, b"error code: 1015"), REPLY), sleeps).complete([], [], max_tokens=300).tokens == 100
    assert sleeps == [5]
    with pytest.raises(GatewayError) as caught:
        client(config, Opener(layer_error(403, {"server": "cloudflare"}, b"error code: 1010"))).complete([], [], max_tokens=300)
    assert caught.value.fatal and not caught.value.transient


class CountingBody(io.BytesIO):
    reads = 0

    def read(self, *args):
        type(self).reads += 1
        return super().read(*args)

    read1 = read


def test_an_error_body_without_a_content_length_is_not_read(config):
    CountingBody.reads = 0
    msg = layer_error(403, {"server": "cloudflare"}).headers
    error = urllib.error.HTTPError("https://gateway.example.invalid/v1/chat/completions", 403, "no", msg, CountingBody(b"error code: 1010"))
    with pytest.raises(GatewayError, match=r"^the gateway returned HTTP 403 \(origin\)$"):
        client(config, Opener(error)).complete([], [], max_tokens=300)
    assert CountingBody.reads == 0


@pytest.mark.parametrize("length", ["33", "x", "-1"])
def test_an_error_body_with_a_large_or_odd_content_length_is_not_read(config, length):
    CountingBody.reads = 0
    msg = layer_error(403, {"server": "cloudflare", "Content-Length": length}).headers
    error = urllib.error.HTTPError("https://gateway.example.invalid/v1/chat/completions", 403, "no", msg, CountingBody(b"error code: 1010"))
    with pytest.raises(GatewayError, match=r"^the gateway returned HTTP 403 \(origin\)$"):
        client(config, Opener(error)).complete([], [], max_tokens=300)
    assert CountingBody.reads == 0


def test_a_dripping_error_body_is_cut_off_by_the_run_deadline(config):
    """A small Content-Length does not make a server send promptly: the body read is held to the run's deadline."""
    clock = Clock()
    msg = layer_error(403, {"server": "cloudflare", "Content-Length": "16"}).headers
    error = urllib.error.HTTPError("https://gateway.example.invalid/v1/chat/completions", 403, "no", msg, Drip(b"error code: 1010", clock))
    gateway = GatewayClient(config, TOKEN, model=WIRE, opener=Opener(error), sleep=lambda s: None, clock=clock)
    gateway.set_time_limit(5)
    with pytest.raises(GatewayError, match=r"^the gateway returned HTTP 403 \(origin\)$"):
        gateway.complete([], [], max_tokens=300)
    assert clock.now <= 107, "stopped at the deadline, not after the whole body"


def test_a_small_dripping_error_body_inside_the_deadline_is_still_classified(config):
    clock = Clock()
    msg = layer_error(403, {"server": "cloudflare", "Content-Length": "16"}).headers
    error = urllib.error.HTTPError("https://gateway.example.invalid/v1/chat/completions", 403, "no", msg, Drip(b"error code: 1010", clock))
    gateway = GatewayClient(config, TOKEN, model=WIRE, opener=Opener(error), sleep=lambda s: None, clock=clock)
    gateway.set_time_limit(60)
    with pytest.raises(GatewayError, match=r"^the gateway returned HTTP 403 \(Cloudflare edge, error 1010\)$"):
        gateway.complete([], [], max_tokens=300)


def _serve_error(preamble: bytes, drip: bytes, config):
    """A real 127.0.0.1 server: a 403 with the given header block, then `drip` one byte every 0.3 s. Returns (message, seconds)."""
    import http.server
    import socketserver
    import threading
    import time

    from golden_road.checks import _open

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.wfile.write(preamble)
            self.wfile.flush()
            try:
                for byte in drip:
                    time.sleep(0.3)
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
            except OSError:
                pass

        def log_message(self, *args):
            pass

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        config["agent"]["gateway"]["url"] = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        config["agent"]["gateway"]["timeout_seconds"] = 1
        gateway = GatewayClient(config, TOKEN, model=WIRE, opener=_open, sleep=lambda s: None)
        gateway.set_time_limit(1)
        started = time.monotonic()
        with pytest.raises(GatewayError) as caught:
            gateway.complete([], [], max_tokens=300)
        return str(caught.value), time.monotonic() - started
    finally:
        server.shutdown()


def test_a_dripping_error_body_on_a_real_socket_is_cut_off_by_the_deadline(config):
    message, seconds = _serve_error(b"HTTP/1.1 403 Forbidden\r\nServer: cloudflare\r\nContent-Length: 16\r\n\r\n", b"error code: 1010", config)
    assert message == "the gateway returned HTTP 403 (origin)" and seconds < 2.5


def test_a_chunked_error_body_is_never_read_even_with_a_content_length(config):
    """http.client ignores Content-Length for chunked, and its chunk-size line read has no deadline check."""
    message, seconds = _serve_error(b"HTTP/1.1 403 Forbidden\r\nServer: cloudflare\r\nContent-Length: 16\r\nTransfer-Encoding: chunked\r\n\r\n",
                                    b"0" * 20 + b"10\r\nerror code: 1010\r\n0\r\n\r\n", config)
    assert message == "the gateway returned HTTP 403 (origin)" and seconds < 2.5


def test_a_chunked_error_body_is_not_read_in_the_fake_opener_either(config):
    CountingBody.reads = 0
    msg = layer_error(403, {"server": "cloudflare", "Content-Length": "16", "Transfer-Encoding": "chunked"}).headers
    error = urllib.error.HTTPError("https://gateway.example.invalid/v1/chat/completions", 403, "no", msg, CountingBody(b"error code: 1010"))
    with pytest.raises(GatewayError, match=r"^the gateway returned HTTP 403 \(origin\)$"):
        client(config, Opener(error)).complete([], [], max_tokens=300)
    assert CountingBody.reads == 0


def _refused(config, model=WIRE):
    with pytest.raises(InputError, match="must not contain the model name") as caught:
        GatewayClient(config, TOKEN, model=model)
    assert model not in str(caught.value)


def test_a_model_name_inside_a_longer_version_is_refused_on_word_boundaries(config):
    config["agent"]["model"]["version"] = "gpt-5-2025-08-07"
    _refused(config, "gpt-5")
    config["agent"]["model"]["version"] = "gpt-50"
    GatewayClient(config, TOKEN, model="gpt-5")  # not on a boundary: "0" follows
    config["agent"]["model"]["version"] = "xgpt-5"
    GatewayClient(config, TOKEN, model="gpt-5")


def test_a_two_character_model_name_is_accepted(config):
    GatewayClient(config, TOKEN, model="ab")
    config["agent"]["model"]["version"] = "ab-1"
    GatewayClient(config, TOKEN, model="ab")


def test_the_apim_template_is_accepted_and_a_path_word_name_is_refused(config):
    config["agent"]["gateway"]["url"] = APIM_URL
    GatewayClient(config, TOKEN, model="acme-deploy-test-0001")
    _refused(config, "openai")
    _refused(config, "chat")


def test_the_literal_check_is_case_sensitive(config):
    config["agent"]["model"]["version"] = "GPT-5-2025"
    GatewayClient(config, TOKEN, model="gpt-5")


def test_tool_choice_is_forced_only_when_asked(config):
    opener = Opener(REPLY, REPLY)
    gateway = client(config, opener)
    gateway.complete([], [], max_tokens=300)
    gateway.complete([], [], max_tokens=300, tool_choice="submit_assessment")
    bodies = [json.loads(request.data) for request, _ in opener.requests]
    assert bodies[0]["tool_choice"] == "auto"
    assert bodies[1]["tool_choice"] == {"type": "function", "function": {"name": "submit_assessment"}}
