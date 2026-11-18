"""graph_read (spec 2026-11-17 A6, A7; decided while planning: item 18): GETs only, only under https://graph.microsoft.com/v1.0, a path
that can't leave it, and every @odata.nextLink page that stays under it."""

import httpx
import pytest
from runtime_fakes import CLIENT, TENANT, client, entra

from agent_runtime.azure import Tokens
from agent_runtime.graph import GRAPH, graph_tool, path_problem
from agent_runtime.settings import Azure
from agent_runtime.tools import ToolError


def graph(request):
    """Microsoft Graph v1.0: a collection answers `value`; /users answers two pages, the first with @odata.nextLink."""
    assert request.method == "GET" and request.url.host == "graph.microsoft.com"
    if request.url.path == "/v1.0/users":
        if request.url.params.get("$skiptoken") == "X'page2":
            return httpx.Response(200, json={"@odata.context": f"{GRAPH}/$metadata#users", "value": [{"displayName": "Bea"}]})
        return httpx.Response(200, json={"@odata.context": f"{GRAPH}/$metadata#users", "value": [{"displayName": "Ann"}],
                                         "@odata.nextLink": f"{GRAPH}/users?$top=1&$skiptoken=X%27page2"})
    if request.url.path == "/v1.0/me":
        return httpx.Response(200, json={"@odata.context": f"{GRAPH}/$metadata#users/$entity", "displayName": "Ann"})
    return httpx.Response(200, json={"@odata.context": f"{GRAPH}/$metadata#organization", "value": [{"displayName": "Example"}]})


def tool(handler=graph):
    http, recorder = client(lambda r: entra(r) if "login" in r.url.host else handler(r))
    return graph_tool(Tokens(Azure(CLIENT, TENANT, "/dev/null", "https://login.microsoftonline.com/"), http), http), recorder


def test_a_path_is_read_with_a_get_and_the_graph_token():
    found, recorder = tool()
    assert found.call({"path": "/organization?$select=displayName"})["value"] == [{"displayName": "Example"}]
    sent = recorder.requests[-1]
    assert (sent.method, sent.url.host, sent.url.path, sent.url.params["$select"]) == ("GET", "graph.microsoft.com", "/v1.0/organization", "displayName")
    assert sent.headers["authorization"] == "Bearer tok-https:"


def test_an_entity_is_answered_as_it_is():
    found, _ = tool()
    assert found.call({"path": "/me"})["displayName"] == "Ann"


def test_every_next_page_is_read():
    found, recorder = tool()
    answer = found.call({"path": "/users?$top=1"})
    assert answer["value"] == [{"displayName": "Ann"}, {"displayName": "Bea"}] and "@odata.nextLink" not in answer
    assert [r.url.path for r in recorder.requests if r.url.host == "graph.microsoft.com"] == ["/v1.0/users", "/v1.0/users"]


@pytest.mark.parametrize("link", ["https://evil.example/v1.0/users?$skiptoken=x", "http://graph.microsoft.com/v1.0/users?$skiptoken=x",
                                  "https://graph.microsoft.com/beta/users?$skiptoken=x", "https://graph.microsoft.com:8443/v1.0/users",
                                  "https://user@graph.microsoft.com/v1.0/users", "https://graph.microsoft.com/v1.0/../beta/users",
                                  "https://graph.microsoft.com.evil.example/v1.0/users", "/v1.0/users?$skiptoken=x",
                                  "https://graph.microsoft.com/v1.0/users#x", "https://graph.microsoft.com/v1.0/us\\ers", 42])
def test_a_next_page_that_leaves_v1_0_is_never_followed(link):
    def leaving(request):
        assert request.url.path == "/v1.0/users" and "$skiptoken" not in request.url.params, f"followed {request.url}"
        return httpx.Response(200, json={"value": [{"displayName": "Ann"}], "@odata.nextLink": link})
    found, recorder = tool(leaving)
    with pytest.raises(ToolError, match="next page"):
        found.call({"path": "/users"})
    assert len([r for r in recorder.requests if "login" not in r.url.host]) == 1


@pytest.mark.parametrize("path", [
    "organization", "//evil.example/x", "/../beta/users", "/users/..", "/users/./me", "/users#x", "/users me", "/users\tme",
    "https://graph.microsoft.com/v1.0/me", "/" + "a" * 2000, None, 3, "", "/users\n", "/users\x00",
    "/users/someone@example.com", "@evil.example/x", "/users\\..\\beta", "/%2e%2e/beta/users", "/%2E%2E/beta", "/.%2e/beta",
    "/users%2f..%2fbeta", "/users%5c..", "/users%252e%252e", "/users%", "/users%zz", "/users?$filter=x%00", "/users ",
    "/users／..", "/users?x=1#frag", "/me?redirect=@evil.example", "/users/..;/x", "/users;x=1/me",
])
def test_a_path_that_could_leave_v1_0_or_hide_text_is_refused(path):
    assert path_problem(path) is not None


@pytest.mark.parametrize("path", ["/me", "/users?$top=5&$select=displayName,mail", "/users/someone%40example.com",
                                  "/groups?$filter=startswith(displayName,'Team')", "/users('0000')/memberOf", "/", "/" + "a" * 1999,
                                  "/sites/root:/docs:/children", "/users?$search=%22displayName:Ann%22",
                                  "/users?$filter=a%20eq%201;b"])
def test_an_ordinary_path_passes(path):
    assert path_problem(path) is None


def test_a_refused_path_is_never_sent():
    found, recorder = tool()
    with pytest.raises(ToolError, match="graph_read refused the path"):
        found.call({"path": "/../beta/users"})
    assert not [r for r in recorder.requests if r.url.host == "graph.microsoft.com"]


def test_only_gets_ever_leave():
    found, recorder = tool()
    found.call({"path": "/organization"})
    found.call({"path": "/users"})
    assert {r.method for r in recorder.requests if r.url.host == "graph.microsoft.com"} == {"GET"}


@pytest.mark.parametrize("answer, words", [(httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied"}}), "answered 403"),
                                           (httpx.Response(302, headers={"location": "https://evil.example/"}), "answered 302"),
                                           (httpx.Response(200, text="<html>"), "not JSON")])
def test_an_answer_that_is_not_a_read_is_a_tool_error(answer, words):
    found, recorder = tool(lambda r: answer)
    with pytest.raises(ToolError, match=words):
        found.call({"path": "/me"})
    assert not [r for r in recorder.requests if "evil" in r.url.host]


def test_graph_that_can_t_be_reached_is_a_tool_error():
    def down(request):
        raise httpx.ConnectError("refused", request=request)
    found, _ = tool(down)
    with pytest.raises(ToolError, match="could not be reached"):
        found.call({"path": "/me"})


def test_a_next_page_seen_before_is_an_error_not_a_loop():
    link = f"{GRAPH}/users?$skiptoken=same"

    def looping(request):
        return httpx.Response(200, json={"value": [{"displayName": "Ann"}], "@odata.nextLink": link})
    found, _ = tool(looping)
    with pytest.raises(ToolError, match="again"):
        found.call({"path": "/users"})
