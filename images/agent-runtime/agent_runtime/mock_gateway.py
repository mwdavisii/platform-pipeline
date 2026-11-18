"""A mock of the platform's model gateway, for tests, the docker end-to-end test and the personal PoC's local demo (spec 2026-11-17
"Mock only"; decided while planning: item 21). It answers OpenAI chat completions deterministically. The runtime never imports it
(a test holds that): run it as `python -m uvicorn agent_runtime.mock_gateway:app --host 0.0.0.0 --port 9090` in the runtime's own image.

- After a tool message: `I used <tool>: the tool answered.`
- When the last user message names one of the request's tools: a call to the first such tool, in the request's order
  (`databricks_query` with `{"statement": "SELECT 1"}`, `graph_read` with `{"path": "/organization"}`, any other with `{}`).
- Otherwise: `Hello from the mock model. You asked: <the message, first 200 characters>`.
The bearer key must be MOCK_GATEWAY_KEY (default `mock-key`).

It ships in the runtime's image on purpose (decided in the round 3b review, M3): the docker test and the local demo run it from that
image, and it is inert there, since an agent app can't change the image's CMD (its Dockerfile is FROM the runtime plus agent.yaml, held
by verify) and nothing in the runtime imports it."""

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ARGUMENTS = {"databricks_query": '{"statement": "SELECT 1"}', "graph_read": '{"path": "/organization"}'}
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _answer(message: dict, finish: str) -> dict:
    return {"id": "chatcmpl-mock", "object": "chat.completion", "created": 0, "model": "mock",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "invalid_request_error"}}, status_code=status)


def _tool_names(doc: dict) -> list:
    tools = doc.get("tools") if isinstance(doc.get("tools"), list) else []
    return [t["function"]["name"] for t in tools
            if isinstance(t, dict) and isinstance(t.get("function"), dict) and isinstance(t["function"].get("name"), str)]


@app.post("/v1/chat/completions")
async def completions(request: Request):
    if request.headers.get("authorization") != f"Bearer {os.environ.get('MOCK_GATEWAY_KEY', 'mock-key')}":
        return _error("Incorrect API key provided", 401)
    try:
        doc = await request.json()
    except ValueError:
        return _error("The body is not JSON", 400)
    if not isinstance(doc, dict) or not isinstance(doc.get("messages"), list):
        return _error("messages must be a list", 400)
    messages = [m for m in doc["messages"] if isinstance(m, dict)]
    if messages and messages[-1].get("role") == "tool":
        called = next((c["function"].get("name") for m in reversed(messages) if m.get("role") == "assistant"
                       for c in m.get("tool_calls") or [] if isinstance(c, dict) and isinstance(c.get("function"), dict)), None)
        return _answer({"role": "assistant", "content": f"I used {called or 'a tool'}: the tool answered."}, "stop")
    text = next((m.get("content") for m in reversed(messages) if m.get("role") == "user"), "")
    text = text if isinstance(text, str) else ""
    named = next((n for n in _tool_names(doc) if n in text), None)
    if named:
        call = {"id": "call_mock_1", "type": "function", "function": {"name": named, "arguments": ARGUMENTS.get(named, "{}")}}
        return _answer({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")
    return _answer({"role": "assistant", "content": f"Hello from the mock model. You asked: {text[:200]}"}, "stop")
