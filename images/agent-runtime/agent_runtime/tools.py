"""A tool the model may call. A tool exists only for what the environment binds (spec 2026-11-17 A6), and every v1 tool reads (A7)."""

import dataclasses as dc


class ToolError(Exception):
    """A tool's failure, in words the model may read: never a token, a key or a credential."""


@dc.dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    call: object = dc.field(compare=False, repr=False)  # (arguments: dict) -> a JSON-able result; raises ToolError


def function_spec(tool: Tool) -> dict:
    """The tool as an OpenAI-compatible function tool."""
    return {"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters}}
