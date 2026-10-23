"""The model gateway client: OpenAI-compatible chat completions with tool calling, over stdlib urllib.

The URL is the full chat-completions endpoint from the factory config, so an OpenAI-compatible proxy
(`https://<proxy>/v1/chat/completions`) and an API-management front end (`.../chat/completions?api-version=...`) both work; an APIM deployment URL writes `{model}` where the deployment name goes, and the client fills it from SCORECARD_GATEWAY_MODEL.
The optional Cloudflare Access service token (`SCORECARD_GATEWAY_ACCESS_ID` and `_SECRET`, both or neither) adds two headers to
every request, and is never logged or put in an error. The model name sent in the body comes only from SCORECARD_GATEWAY_MODEL
(required; it can be organization-sensitive) and is never logged or put in an error; the factory config records only its version.
The credential is an OIDC-derived bearer token or a scoped key (R3), read only from the environment variable the config
names. No redirect is followed (a 3xx stops the run like a refused credential), and no error message carries the response body or the credential; an HTTP error names only the refusing layer (Cloudflare Access, the Cloudflare edge with its four-digit code, or the origin).

Time: urllib's `timeout_seconds` applies to each socket operation, so a server that sends one byte at a time would never trip it.
The response body is therefore read in chunks (one socket read each) against the run's deadline (`set_time_limit`), and a call
is not started or retried once the deadline has passed. Two things are not covered: the connect and the response headers (each
bounded only by `timeout_seconds` per socket operation), and one chunk read already waiting, which ends within `timeout_seconds`.
The workflow's job timeout is the backstop for those. Chunk-size lines of a chunked response are not deadline-covered either (the
success path reads them through http.client; an error response's body is never read when it is chunked).
"""

import dataclasses
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Protocol

from builder import VERSION as BUILDER_VERSION
from golden_road.checks import _open
from golden_road.io import InputError

from ..config import GITHUB_TOKEN_PREFIXES, MODEL_PLACEHOLDER


CHUNK_BYTES = 65536
USER_AGENT = f"golden-road-scorecard/{BUILDER_VERSION}"  # urllib's default, Python-urllib/3.x, is refused by Cloudflare's Browser Integrity Check (1010)


@dataclasses.dataclass(frozen=True)
class Completion:
    message: dict  # the assistant message: {"role": "assistant", "content": str | None[, "tool_calls": [...]]}
    tokens: int | None  # total tokens the gateway reported for the call, or None when it reported none

_EDGE_CODE = re.compile(rb"error code: ([0-9]{4})\s?")  # Cloudflare's short edge error body, such as "error code: 1010"
EDGE_BODY_BYTES = 32
MIN_LITERAL_CHECK = 3


class ModelClient(Protocol):
    def complete(self, messages: list[dict], tools: list[dict], *, max_tokens: int) -> Completion: ...


class GatewayError(Exception):
    """A model call failed. `fatal` means no later call can succeed either (the gateway refused the credential)."""

    def __init__(self, message: str, *, fatal: bool = False, transient: bool = False):
        super().__init__(message)
        self.fatal, self.transient = fatal, transient


def credential(config: dict, environ=None) -> str:
    name = config["agent"]["gateway"]["credential_env"]
    value = (os.environ if environ is None else environ).get(name, "").strip()
    if not value:
        raise InputError(f"the gateway credential {name} is not set")
    if not value.isascii() or not value.isprintable():
        raise InputError(f"{name} is not a valid credential")
    if value.startswith(GITHUB_TOKEN_PREFIXES):  # a runner or GitHub token must never be sent to the gateway (SEC-2)
        raise InputError(f"{name} holds a GitHub token, not a gateway credential")
    return value


ACCESS_ID_ENV, ACCESS_SECRET_ENV = "SCORECARD_GATEWAY_ACCESS_ID", "SCORECARD_GATEWAY_ACCESS_SECRET"
_ACCESS_VALUE = re.compile(r"[A-Za-z0-9._-]{16,512}")


class AccessHeaders(dict):
    """The Cloudflare Access service-token headers. Its repr never shows the values."""

    def __repr__(self) -> str:
        return "AccessHeaders(<redacted>)"

    __str__ = __repr__


def access_headers(environ=None) -> AccessHeaders | None:
    """The optional Cloudflare Access service token (the gateway's public route sits behind it). Both variables or neither:
    one alone, or a value with unexpected characters, is a configuration error. No message carries a value."""
    env = os.environ if environ is None else environ
    values = {name: env.get(name, "").strip() for name in (ACCESS_ID_ENV, ACCESS_SECRET_ENV)}
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise InputError(f"set both {ACCESS_ID_ENV} and {ACCESS_SECRET_ENV}, or neither")
    for name, value in values.items():
        if not _ACCESS_VALUE.fullmatch(value):
            raise InputError(f"{name} is not a valid Cloudflare Access value")
    return AccessHeaders({"CF-Access-Client-Id": values[ACCESS_ID_ENV], "CF-Access-Client-Secret": values[ACCESS_SECRET_ENV]})


MODEL_ENV = "SCORECARD_GATEWAY_MODEL"
_MODEL_VALUE = re.compile(r"[A-Za-z0-9._-]{1,128}")


class Secret(str):
    """An organization-sensitive string (the gateway's model name, and the URL that carries it). It is used as is; only its
    repr is redacted, so a traceback or a vars() dump can't show it."""

    def __repr__(self) -> str:
        return "<redacted>"


def wire_model(value: str) -> Secret:
    """The model name the gateway expects. Path-safe, because it can go into the URL's path ("." and ".." would move it)."""
    if not isinstance(value, str) or not _MODEL_VALUE.fullmatch(value) or value in (".", ".."):
        raise InputError(f"{MODEL_ENV} is not a valid model name (1 to 128 of A-Z a-z 0-9 . _ -)")
    return Secret(value)


def gateway_model(environ=None) -> Secret:
    """The required gateway model name. It is a secret, not config: an APIM deployment name can be organization-sensitive.
    The factory config's `agent.model.version` labels it. No message carries the value."""
    value = (os.environ if environ is None else environ).get(MODEL_ENV, "").strip()
    if not value:
        raise InputError(f"the gateway model {MODEL_ENV} is not set")
    return wire_model(value)


class GatewayClient:
    def __init__(self, config: dict, token: str, *, model: str, access: AccessHeaders | None = None, opener=_open, sleep=time.sleep,
                 clock=time.monotonic):
        self.gateway, self.model = config["agent"]["gateway"], wire_model(model)
        self.url = Secret(self.gateway["url"].replace(MODEL_PLACEHOLDER, self.model))  # the model name is path-safe (wire_model)
        self.token, self.opener, self.sleep, self.clock = Secret(token), opener, sleep, clock
        # The name must come only from the secret. The match is on word boundaries (not inside a longer alphanumeric run) and case-sensitive;
        # under MIN_LITERAL_CHECK characters it would be a coincidence, so shorter names are not checked. The URL is checked without its {model}.
        if len(self.model) >= MIN_LITERAL_CHECK:
            literal = re.compile(r"(?<![A-Za-z0-9])" + re.escape(self.model) + r"(?![A-Za-z0-9])")
            if literal.search(self.gateway["url"].replace(MODEL_PLACEHOLDER, "")) or literal.search(config["agent"]["model"]["version"]):
                raise InputError("agent.gateway.url and agent.model.version must not contain the model name (use {model} in the URL); "
                                 "if the name is also a word of the URL or the version, use a more specific deployment name")
        self.access = access
        self.deadline: float | None = None

    def set_time_limit(self, seconds: float) -> None:
        """Calls and body reads stop `seconds` from now (the run's remaining wall-clock budget)."""
        self.deadline = self.clock() + seconds

    def _remaining(self) -> float | None:
        return None if self.deadline is None else self.deadline - self.clock()

    def _out_of_time(self) -> GatewayError:
        return GatewayError("the run's time budget ran out during a model call", fatal=True)

    def _layer(self, exc: urllib.error.HTTPError) -> str:
        """Which layer refused the call, in a fixed vocabulary: Cloudflare Access by its headers, the Cloudflare edge by `server:
        cloudflare` and its short `error code: NNNN` body (only the four digits are kept), otherwise the origin. No header value and
        no other body text is used: an origin's error body can echo the model name. The body is read only when Content-Length
        promises at most EDGE_BODY_BYTES, and through the deadline-checked reader, so a drip-fed error can't hold the run."""
        names = {str(k).lower(): str(v) for k, v in (exc.headers or {}).items()}
        if "cf-access-domain" in names or "cf-access-aud" in names:
            return "Cloudflare Access"
        length = names.get("content-length", "").strip()
        # Any Transfer-Encoding: http.client ignores Content-Length for chunked, and its chunk-size line read has no deadline check.
        if "transfer-encoding" not in names and names.get("server", "").strip().lower() == "cloudflare" and length.isascii() and length.isdigit() and 0 < int(length) <= EDGE_BODY_BYTES:
            try:  # a tunnelled origin's answers carry the server value too, so the body decides
                head = self._read(exc, EDGE_BODY_BYTES)
            except (GatewayError, OSError, ValueError, http.client.HTTPException):
                head = b""
            if match := _EDGE_CODE.fullmatch(head):
                return f"Cloudflare edge, error {match.group(1).decode()}"
        return "origin"

    def _headers(self) -> dict:
        auth = self.gateway["auth"]
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
        if auth["scheme"] == "bearer":
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            headers[auth["header"]] = self.token
        if self.access:
            headers.update(self.access)
        return headers

    def complete(self, messages: list[dict], tools: list[dict], *, max_tokens: int) -> Completion:
        body = json.dumps({"model": self.model, "messages": messages, "tools": tools, "tool_choice": "auto",
                           "temperature": 0, "max_tokens": max_tokens}).encode("utf-8")
        try:
            return self._post(body)
        except GatewayError as exc:
            if not exc.transient:
                raise
        left = self._remaining()
        if left is not None and left <= self.gateway["retry_seconds"]:
            raise self._out_of_time()
        self.sleep(self.gateway["retry_seconds"])  # one retry, for a rate limit, a 5xx, or a network failure
        return self._post(body)

    def _post(self, body: bytes) -> Completion:
        request = urllib.request.Request(self.url, data=body, method="POST", headers=self._headers())
        limit = self.gateway["max_response_bytes"]
        timeout = self.gateway["timeout_seconds"]
        if (left := self._remaining()) is not None:
            if left <= 0:
                raise self._out_of_time()
            timeout = min(timeout, max(1, left))
        try:
            with self.opener(request, timeout) as response:
                data = self._read(response, limit)
        except urllib.error.HTTPError as exc:
            raise GatewayError(f"the gateway returned HTTP {exc.code} ({self._layer(exc)})",
                               fatal=exc.code in (401, 403) or 300 <= exc.code < 400,  # a redirect (Access sends a bad token to a login page) is never followed
                               transient=exc.code == 429 or exc.code >= 500) from None
        except (OSError, ValueError, http.client.HTTPException) as exc:  # URLError, timeouts, connection errors, and malformed replies
            raise GatewayError(f"the gateway could not be reached ({type(exc).__name__})", transient=True) from None
        if len(data) > limit:
            raise GatewayError(f"the gateway response is larger than {limit} bytes")
        return parse_completion(data)

    def _read(self, response, limit: int) -> bytes:
        """The body, at most limit + 1 bytes, read one socket read at a time so the deadline is checked between reads."""
        read = getattr(response, "read1", response.read)  # read1 returns after one underlying read, however few bytes
        chunks, total = [], 0
        while total <= limit:
            if (left := self._remaining()) is not None and left <= 0:
                raise self._out_of_time()
            chunk = read(min(CHUNK_BYTES, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks)


def _tool_call(item) -> dict:
    function = item.get("function") if isinstance(item, dict) else None
    if (not isinstance(function, dict) or not isinstance(item.get("id"), str) or not item["id"]
            or not isinstance(function.get("name"), str) or not isinstance(function.get("arguments", ""), str)):
        raise GatewayError("the gateway response has a malformed tool call")
    return {"id": item["id"][:200], "type": "function",
            "function": {"name": function["name"][:200], "arguments": function.get("arguments", "")}}


def parse_completion(data: bytes) -> Completion:
    """The first choice's message, reduced to the fields the loop uses. Anything malformed is a GatewayError."""
    try:
        doc = json.loads(data)
    except (ValueError, RecursionError):
        raise GatewayError("the gateway response is not JSON") from None
    choices = doc.get("choices") if isinstance(doc, dict) else None
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise GatewayError("the gateway response has no message")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise GatewayError("the gateway response has non-text content")
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list):
        raise GatewayError("the gateway response has a malformed tool call")
    out = {"role": "assistant", "content": content}
    if calls:
        out["tool_calls"] = [_tool_call(item) for item in calls]
        ids = [c["id"] for c in out["tool_calls"]]
        if len(ids) != len(set(ids)):  # replies are matched to calls by id, so a repeated id can't be answered unambiguously
            raise GatewayError("the gateway response repeats a tool call id")
    usage = doc.get("usage")
    total = usage.get("total_tokens") if isinstance(usage, dict) else None
    return Completion(message=out, tokens=total if type(total) is int and total >= 0 else None)
