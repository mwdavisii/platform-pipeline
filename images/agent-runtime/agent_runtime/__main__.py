"""`python -m agent_runtime`: read the settings and /app/agent.yaml, build the tools, serve on 8080 (spec 2026-11-17 "Start"; decided
while planning: items 16, 25). A missing or bad setting or file exits 2 naming it, never its value; MCP servers that can't be listed exit
3, and Kubernetes starts the pod again (no cap). Every HTTP call has no timeout (A10)."""

import logging
import os
import sys
from pathlib import Path

import httpx

from .agent_file import AgentFileError, load
from .app import create_app
from .instructions import system_prompt
from .mcp import McpError
from .model import Gateway
from .registry import build_tools, visible
from .settings import SettingError, from_env

AGENT_FILE = "/app/agent.yaml"  # where the agent app's Dockerfile copies it
PORT = 8080  # builder.spec.AGENT_PORT


def _say(message: str) -> None:
    print(f"agent-runtime: {message}", file=sys.stderr, flush=True)


def _say_raw(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class _Stderr(logging.StreamHandler):
    """Writes to whatever sys.stderr is when a line is written."""

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _):
        pass


def _log_requests() -> None:
    """Only the runtime's own logger writes (its one line per request), never the root logger: httpx and httpcore log every
    request's URL at INFO (a Graph query's included), which would break "one line, no arguments" (C1)."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    runtime = logging.getLogger("agent_runtime")
    runtime.setLevel(logging.INFO)
    if not any(isinstance(h, _Stderr) for h in runtime.handlers):
        handler = _Stderr()
        handler.setFormatter(logging.Formatter("%(message)s"))
        runtime.addHandler(handler)


def _one_line(text: str) -> str:
    """Words from outside (an MCP server's error message) on one line, stripped of hidden characters (M2)."""
    return " ".join(visible(text).split())


def _serve(app) -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning", access_log=False, server_header=False)


def http_client() -> httpx.Client:
    """The one HTTP client the runtime builds, shared by the gateway, MCP, Graph, Databricks and Entra: no timeout (A10), and no
    redirect followed (httpx drops Authorization on a cross-origin redirect but not CF-Access-Client-Secret)."""
    return httpx.Client(timeout=None, follow_redirects=False)


def main(env=os.environ, *, agent_path=AGENT_FILE, http=None, serve=_serve) -> int:
    _log_requests()
    try:
        settings = from_env(env)
    except SettingError as exc:
        _say(str(exc))
        return 2
    try:
        raw = Path(agent_path).read_bytes()
    except OSError:
        _say(f"{AGENT_FILE} can't be read")  # the runtime's fixed path, whatever path a test passes
        return 2
    try:
        agent = load(raw)  # the builder checked the model against the platform's list; the runtime has no list of its own
    except AgentFileError as exc:
        _say(str(exc))
        return 2
    http = http or http_client()
    try:
        tools = build_tools(settings, http, _say_raw)
    except (McpError, httpx.HTTPError) as exc:
        _say(f"the MCP servers can't be listed ({_one_line(str(exc)) if isinstance(exc, McpError) else type(exc).__name__}); "
             "the pod is started again and tries again")
        return 3
    _say(f"ready with {len(tools)} tools: {', '.join(t.name for t in tools) or 'none'}")
    serve(create_app(agent, system_prompt(agent, settings.skills_dir), tools, Gateway(settings.gateway_url, settings.gateway_key, http)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
