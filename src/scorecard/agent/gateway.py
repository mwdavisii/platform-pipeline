"""The model gateway client: OpenAI-compatible chat completions with tool calling, over stdlib urllib.

The URL is the full chat-completions endpoint from the factory config, so an OpenAI-compatible proxy
(`https://<proxy>/v1/chat/completions`) and an API-management front end (`.../chat/completions?api-version=...`) both work.
The credential is an OIDC-derived bearer token or a scoped key (R3), read only from the environment variable the config
names. No redirect is followed, and no error message carries the response body or the credential.

Time: urllib's `timeout_seconds` applies to each socket operation, so a server that sends one byte at a time would never trip it.
The response body is therefore read in chunks (one socket read each) against the run's deadline (`set_time_limit`), and a call
is not started or retried once the deadline has passed. Two things are not covered: the connect and the response headers (each
bounded only by `timeout_seconds` per socket operation), and one chunk read already waiting, which ends within `timeout_seconds`.
The workflow's job timeout is the backstop for those.
"""

import dataclasses
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from typing import Protocol

from golden_road.checks import _open
from golden_road.io import InputError

from ..config import GITHUB_TOKEN_PREFIXES


CHUNK_BYTES = 65536


@dataclasses.dataclass(frozen=True)
class Completion:
    message: dict  # the assistant message: {"role": "assistant", "content": str | None[, "tool_calls": [...]]}
    tokens: int | None  # total tokens the gateway reported for the call, or None when it reported none


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


class GatewayClient:
    def __init__(self, config: dict, token: str, *, opener=_open, sleep=time.sleep, clock=time.monotonic):
        agent = config["agent"]
        self.gateway, self.model = agent["gateway"], agent["model"]["name"]
        self.token, self.opener, self.sleep, self.clock = token, opener, sleep, clock
        self.deadline: float | None = None

    def set_time_limit(self, seconds: float) -> None:
        """Calls and body reads stop `seconds` from now (the run's remaining wall-clock budget)."""
        self.deadline = self.clock() + seconds

    def _remaining(self) -> float | None:
        return None if self.deadline is None else self.deadline - self.clock()

    def _out_of_time(self) -> GatewayError:
        return GatewayError("the run's time budget ran out during a model call", fatal=True)

    def _headers(self) -> dict:
        auth = self.gateway["auth"]
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth["scheme"] == "bearer":
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            headers[auth["header"]] = self.token
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
        request = urllib.request.Request(self.gateway["url"], data=body, method="POST", headers=self._headers())
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
            raise GatewayError(f"the gateway returned HTTP {exc.code}", fatal=exc.code in (401, 403),
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
