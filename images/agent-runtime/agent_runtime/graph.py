"""graph_read (spec 2026-11-17 A6, A7; decided while planning: item 18): a Microsoft Graph GET as the app's identity, under the scopes
its service principal was granted. Only GET, only under https://graph.microsoft.com/v1.0, and a path that can't leave it: it starts
with one /, holds no .. or . segment, no //, no ; before the query, no #, no @, no backslash, no space, control or non-ASCII character, no percent-encoded
dot, slash, backslash, percent or control character, and is at most 2000 characters. Every @odata.nextLink page is read (no cap,
A10) while the link stays under https://graph.microsoft.com/v1.0/; a link that leaves it is never sent the token."""

import re

import httpx

from .tools import Tool, ToolError

GRAPH = "https://graph.microsoft.com/v1.0"
HOST = "graph.microsoft.com"
SCOPE = "https://graph.microsoft.com/.default"
PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:%/?-]{0,1999}")
# %XX escapes that could re-form a dot segment, a slash, a backslash, another escape, or hide a control character
BAD_ESCAPE = re.compile(r"%(2[eEfF]|5[cC]|25|[01][0-9a-fA-F]|7[fF])")
ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
LINK = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:%/?@-]*")
PARAMETERS = {"type": "object", "additionalProperties": False, "required": ["path"],
              "properties": {"path": {"type": "string", "description": "A Microsoft Graph v1.0 path and query, for example "
                                      "/users?$top=5&$select=displayName (percent-encode an @ as %40)"}}}


def _route_problem(route: str) -> str | None:
    segments = route.split("/")[1:]
    if "//" in route or any(s in (".", "..") for s in segments):
        return "the path may not hold //, a . or a .. segment"
    if ";" in route:  # a path parameter (/..;/) can read as a dot segment on the far side
        return "the path may not hold ; before its query"
    return None


def path_problem(path) -> str | None:
    """Why `path` may not be read, or None."""
    if not isinstance(path, str) or not PATH.fullmatch(path):
        return "the path must start with / and hold no space, #, @, backslash, control or non-ASCII character (at most 2000 characters)"
    if ESCAPE.search(path) or BAD_ESCAPE.search(path):
        return "the path may not percent-encode a dot, a slash, a backslash, a percent sign or a control character"
    return _route_problem(path.split("?", 1)[0])


def _next_path(link) -> str | None:
    """The path under /v1.0 an @odata.nextLink names, or None when it leaves https://graph.microsoft.com/v1.0/."""
    if not isinstance(link, str) or not link.startswith(GRAPH + "/") or not LINK.fullmatch(link):
        return None
    try:
        url = httpx.URL(link)
    except httpx.InvalidURL:
        return None
    if (url.scheme, url.host, url.port, url.userinfo) != ("https", HOST, None, b""):
        return None
    path = link[len(GRAPH):]
    route = path.split("?", 1)[0]
    if "@" in route or ESCAPE.search(path) or BAD_ESCAPE.search(route) or _route_problem(route):
        return None
    return path


def graph_tool(tokens, http) -> Tool:
    def get(path: str) -> dict:
        try:
            response = http.get(GRAPH + path, headers={"Authorization": f"Bearer {tokens.token(SCOPE)}"}, timeout=None)
        except httpx.HTTPError as exc:
            raise ToolError(f"Microsoft Graph could not be reached ({type(exc).__name__})") from None
        if not 200 <= response.status_code < 300:  # a redirect is not followed
            raise ToolError(f"Microsoft Graph answered {response.status_code}")
        try:
            doc = response.json()
        except ValueError:
            raise ToolError("Microsoft Graph's answer is not JSON") from None
        if not isinstance(doc, dict):
            raise ToolError("Microsoft Graph's answer is not an object")
        return doc

    def call(arguments):
        path = arguments.get("path") if isinstance(arguments, dict) else None
        problem = path_problem(path)
        if problem:
            raise ToolError(f"graph_read refused the path before sending it: {problem}")
        doc = get(path)
        if not isinstance(doc.get("value"), list):
            return doc
        values = list(doc["value"])
        page, seen = doc, set()
        while "@odata.nextLink" in page:  # every page, however many (A10)
            following = _next_path(page["@odata.nextLink"])
            if following is None:
                raise ToolError("Microsoft Graph's next page link leaves https://graph.microsoft.com/v1.0: not followed")
            if following in seen:  # protocol correctness, not a cap: a link seen before would loop forever
                raise ToolError("Microsoft Graph named a next page again: the collection can't be read to its end")
            seen.add(following)
            page = get(following)
            values += page.get("value") if isinstance(page.get("value"), list) else []
        answer = {k: v for k, v in doc.items() if k != "@odata.nextLink"}
        answer["value"] = values
        return answer

    return Tool("graph_read", "Read from Microsoft Graph v1.0 with a GET, as this app's service principal. Every page of a collection "
                "is read.", PARAMETERS, call)
