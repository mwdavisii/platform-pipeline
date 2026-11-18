"""The platform's model gateway (spec 2026-11-17 A5; decided while planning: item 15): OpenAI-compatible chat completions with function
tools, the app's key as a bearer token. No max_tokens and no timeout: a limit, when wanted, is the gateway's (A10)."""

import httpx

from .tools import function_spec


class GatewayError(Exception):
    """The gateway didn't answer with a message. The words never hold the key or the gateway's own text."""


class Gateway:
    def __init__(self, url: str, key: str, http):
        self.url, self.key, self.http = url.rstrip("/"), key, http

    def complete(self, model: str, messages: list, tools: list) -> dict:
        """The assistant message of the first choice: content (text or null) and, when it calls tools, tool_calls (a list)."""
        request = {"model": model, "messages": messages}
        if tools:
            request["tools"] = [function_spec(t) for t in tools]
        try:
            response = self.http.post(f"{self.url}/chat/completions", json=request, headers={"Authorization": f"Bearer {self.key}"},
                                      timeout=None, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise GatewayError(f"the gateway could not be reached ({type(exc).__name__})") from None
        if not 200 <= response.status_code < 300:  # a redirect is not followed: the key would go with it (M6)
            raise GatewayError(f"the gateway answered {response.status_code}")
        try:
            message = response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise GatewayError("the gateway's answer has no message") from None
        if not isinstance(message, dict):
            raise GatewayError("the gateway's answer has no message")
        if message.get("content") is not None and not isinstance(message["content"], str):
            raise GatewayError("the gateway's message content is not text")
        if message.get("tool_calls") is not None and not isinstance(message["tool_calls"], list):
            raise GatewayError("the gateway's tool calls are not a list")
        return message
