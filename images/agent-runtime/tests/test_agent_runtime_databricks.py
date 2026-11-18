"""databricks_query (spec 2026-11-17 A6, A7; decided while planning: item 17): one read statement, refused before it is sent when it
is anything else, polled until it ends and every chunk read (A10). The guard fails closed: text Databricks could read two ways is
refused."""

import functools

import httpx
import pytest
from runtime_fakes import CLIENT, DBX_HOST, TENANT, body, client, databricks, entra

from agent_runtime.azure import Tokens
from agent_runtime.databricks import SCOPE, databricks_tool, read_only_problem
from agent_runtime.settings import Azure, Databricks
from agent_runtime.tools import ToolError

AZURE = Azure(CLIENT, TENANT, "/dev/null", "https://login.microsoftonline.com/")
R = 'reflect("java.lang.Thread","sleep",1L)'


@pytest.mark.parametrize("statement", [
    "SELECT 1", "select * from events where day = 'Thursday';", "WITH t AS (SELECT 1 AS n) SELECT n FROM t",
    "SELECT `update`, `delete` FROM audit", "SELECT 'drop table x; insert' AS s", "-- a comment\nSELECT 1", "/* note */ SELECT 1",
    "SELECT 'it''s' FROM t", "SELECT replace(name, 'a', 'b') FROM t", "SELECT 1;  -- done", "SELECT 1 -- done",
    "(SELECT 1) UNION (SELECT 2)", "SELECT `we``ird` FROM t", "SELECT 'a\\\\' FROM t", "SELECT 'Zoë' AS name", "SELECT `naïve` FROM t",
    "SELECT offset, settings, user_set, reset_at FROM t", "SELECT 1 /* a ; b */", "SELECT\t1\r\nFROM t",
    "SELECT * FROM t JOIN u USING (id)", "SELECT transform(xs, x -> x + 1) FROM t", "SELECT a:b.c FROM t",
    # A column named like a function is a read; read_files and path tables are reads governed by external-location grants.
    "SELECT reflect, `secret`, ai_score, t.http_request FROM t", "SELECT * FROM read_files('/Volumes/c/s/v/x.csv')",
    "SELECT * FROM delta.`/mnt/data/events`", "SELECT bar'x' FROM t", "SELECT '$', '{x}', '$ {' FROM t",
    "SELECT r'a\\b', R'x' FROM t", "SELECT 1e3, 2L, 0.5 FROM t", "SELECT 'reflect', \"secret\" FROM t", "SELECT 1 /* a * / b */"])
def test_reads_pass(statement):
    assert read_only_problem(statement) is None


@pytest.mark.parametrize("statement, words", [
    ("", "empty"), ("   ", "empty"), (None, "empty"), (7, "empty"),
    ("DELETE FROM events", "only a SELECT"), ("SHOW TABLES", "only a SELECT"), ("EXPLAIN SELECT 1", "only a SELECT"),
    ("SET spark.sql.ansi.enabled = false", "only a SELECT"), ("USE CATALOG other", "only a SELECT"),
    ("CALL system.do_it()", "only a SELECT"), ("COPY INTO t FROM '/x'", "only a SELECT"), ("CACHE TABLE t", "only a SELECT"),
    ("OPTIMIZE t", "only a SELECT"), ("REFRESH TABLE t", "only a SELECT"), ("VALUES (1)", "only a SELECT"), ("TABLE t", "only a SELECT"),
    ("FROM t INSERT INTO u SELECT *", "only a SELECT"), ("DESCRIBE t", "only a SELECT"),
    ("SELECT 1; SELECT 2", "only one statement"), ("SELECT 1; DROP TABLE t", "only one statement"), ("SELECT 1;;", "only one statement"),
    ("SELECT 1 ;\n; ", "only one statement"),
    ("WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x", "names DELETE"),
    ("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x", "names INSERT"),
    ("WITH x AS (SELECT 1) MERGE INTO t USING x ON true WHEN MATCHED THEN DELETE", "names DELETE"),
    ("SELECT * INTO backup FROM t", "names INTO"), ("SELECT 1 /* unclosed", "a comment is not closed"),
    ("SELECT 'unclosed", "a quoted string is not closed"), ("SELECT `unclosed", "a quoted string is not closed"),
    ("WITH t AS (VALUES (1))", "names no SELECT"), ("SELECT update FROM audit", "names UPDATE"),
    ("SELECT * FROM t WHERE 1 = 1 /* x */ ; /* y */ DROP TABLE t", "only one statement"),
    # Nested comments read two ways (Databricks nests them; a first "*/" would end it elsewhere): refused.
    ("SELECT 1 /* /* */ ' */ ; DROP TABLE t -- '", "a comment holds /*"),
    # A hint is parsed, not skipped, so a quote inside one can hide "*/": refused.
    ("SELECT /*+ X(' */ ' ) */ ; DROP TABLE t; --'", "a hint"),
    # A raw string r'...' is read raw, as Databricks reads it; a backslash before a quote in any other string can end it or not
    # (escapedStringLiterals): refused.
    ("SELECT r'\\' ; DROP TABLE t --'", "only one statement"), ("SELECT 'x\\' ; DROP TABLE t; --'", "backslash"),
    ("SELECT \"x\\\"y\" FROM t", "backslash"),
    # A "--" comment ends at a carriage return, and a backslash before its line break carries it on: refused or seen.
    ("SELECT 1 -- note\r; DROP TABLE t", "only one statement"), ("SELECT 1 -- x \\\n'\n; DROP TABLE t --'", "backslash"),
    # Text outside quotes is ASCII: a fullwidth semicolon or a lookalike keyword is refused, not guessed at.
    ("SELECT 1； DROP TABLE t", "outside quotes"), ("ＳELECT 1", "outside quotes"), ("SELECT 1 FROM t", "outside quotes"),
    ("SELECT 1\x00", "control character"), ("SELECT '\x1b[2J'", "control character"), ("SELECT 1\x85", "control character"),
    # Functions that reach outside the warehouse or into the JVM are not reads.
    ("SELECT reflect('java.lang.Runtime', 'getRuntime')", "names REFLECT"), ("SELECT java_method('a', 'b')", "names JAVA_METHOD"),
    ("SELECT http_request(conn => 'c', method => 'POST', path => '/x')", "names HTTP_REQUEST"),
    ("SELECT TRANSFORM (a) USING 'rm -rf /' FROM t", "USING"),
    # A backticked or dotted name is still the function (review C1); IDENTIFIER() can build any name, so it is refused outright.
    ("SELECT `reflect`('java.lang.System','getenv','DATABRICKS_TOKEN')", "names REFLECT"),
    ("SELECT `java_method`('java.lang.Runtime','getRuntime')", "names JAVA_METHOD"),
    ("SELECT `system`.`builtin`.`http_request`(conn => 'c', method => 'GET', path => '/')", "names HTTP_REQUEST"),
    ("SELECT system.builtin.http_request(conn => 'c')", "names HTTP_REQUEST"), ("SELECT `ReFlEcT` /* x */ ('a')", "names REFLECT"),
    ("SELECT `system.builtin.reflect`('a')", "names REFLECT"),
    ("SELECT IDENTIFIER('reflect')('java.lang.System','getenv','X')", "names IDENTIFIER"),
    ("SELECT IDENTIFIER('http' || '_request')(conn => 'c')", "names IDENTIFIER"), ("SELECT * FROM identifier('t')", "names IDENTIFIER"),
    # A raw-string prefix is part of the string, so the script is still seen (review C2); MAP ... USING and the record classes too.
    ("SELECT TRANSFORM(a) USING r'cat /etc/passwd' FROM t", "USING"), ("SELECT TRANSFORM(a) USING R\"sh -c id\" AS (b) FROM t", "USING"),
    ("SELECT TRANSFORM(a) USING /* x */ r'cat' FROM t", "USING"), ("SELECT * FROM (MAP a USING r'cat' AS b FROM t)", "USING"),
    ("SELECT TRANSFORM(a) ROW FORMAT DELIMITED RECORDWRITER 'w' USING 'cat' FROM t", "RECORDWRITER"),
    ("SELECT TRANSFORM(a) RECORDREADER 'r' FROM t", "RECORDREADER"),
    # Secrets, AI functions, streams and remote reads leave the warehouse's tables (review I1).
    ("SELECT secret('scope', 'key')", "names SECRET"), ("SELECT `try_secret`('scope', 'key')", "names TRY_SECRET"),
    ("SELECT ai_query('endpoint', 'say hi')", "names AI_QUERY"), ("SELECT `AI_summarize`(text) FROM t", "names AI_SUMMARIZE"),
    ("SELECT * FROM read_kafka(bootstrapServers => 'k:9092')", "names READ_KAFKA"), ("SELECT * FROM read_kinesis(streamName => 's')", "names READ_KINESIS"),
    ("SELECT * FROM read_pubsub(subscriptionId => 's')", "names READ_PUBSUB"), ("SELECT * FROM read_pulsar(topic => 't')", "names READ_PULSAR"),
    ("SELECT * FROM remote_query('conn', query => 'DELETE FROM t')", "names REMOTE_QUERY"),
    ("SELECT * FROM vector_search(index => 'i', query => 'q')", "names VECTOR_SEARCH"),
    # Variable substitution runs before the SQL is read, inside quotes too (review I2).
    ("SELECT '${env:DATABRICKS_TOKEN}'", "${"), ("SELECT ${spark:x}", "${"), ("SELECT `${x}` FROM t", "${"),
    # The second re-review (R is a reflect call). A raw string ends at its first quote, so a doubled one reads two ways.
    ("SELECT r'a''\\'', " + R + " --'", "raw string"),
    # 1r is one identifier (Spark's may start with a digit), so the string after it is an ordinary one, whose backslash reads two ways.
    ("SELECT 1r'\\'' , " + R + " --'", "backslash"),
    # /*/*/ opens a nested comment: the check sees the * that closes the first /*.
    ("SELECT 1 /*/*/ ' */ */ , " + R + " --'", "a comment holds /*"),
    # $ outside quotes (dollar quoting) is refused.
    ("SELECT $$'$$, " + R + " --'", "$"), ("SELECT $1", "$"),
    # A double-quoted name (ansi.doubleQuotedIdentifiers) followed by ( is a call.
    ("SELECT \"reflect\"('java.lang.Thread','sleep',1L)", "names REFLECT"),
    ("SELECT \"system\".\"builtin\".\"http_request\"(conn => 'c')", "names HTTP_REQUEST"),
    ("SELECT 'ai_query' ('e', 'q')", "names AI_QUERY"),
    ("SELECT system.ai.python_exec('import os')", "names PYTHON_EXEC"), ("SELECT `python_exec`('1')", "names PYTHON_EXEC"),
    ("SELECT * FROM list_secrets()", "names LIST_SECRETS"),
])
def test_anything_but_one_read_is_refused(statement, words):
    problem = read_only_problem(statement)
    assert problem is not None and words in problem, problem


def tool(handler, clock=None):
    databricks.polls = 0
    http, recorder = client(lambda r: entra(r) if "login" in r.url.host else handler(r))
    tokens = Tokens(AZURE, http) if clock is None else Tokens(AZURE, http, clock=clock)
    return databricks_tool(Databricks(DBX_HOST, "0000notes0000", "notes", "public"), tokens, http, sleep=lambda s: None), recorder


def test_one_select_runs_on_the_bound_warehouse_and_every_chunk_is_read():
    found, recorder = tool(databricks)
    assert found.call({"statement": "SELECT n, day FROM free_days"}) == {"columns": ["n", "day"], "rows": [["1", "Thursday"], ["2", "Friday"]]}
    post = next(r for r in recorder.requests if r.method == "POST" and r.url.host == DBX_HOST)
    assert body(post) == {"statement": "SELECT n, day FROM free_days", "warehouse_id": "0000notes0000", "catalog": "notes", "schema": "public",
                          "wait_timeout": "30s", "on_wait_timeout": "CONTINUE", "disposition": "INLINE", "format": "JSON_ARRAY"}
    assert post.headers["authorization"] == "Bearer tok-" + SCOPE.split("/")[0]
    assert {r.url.host for r in recorder.requests} == {DBX_HOST, "login.microsoftonline.com"}


def test_without_a_catalog_or_schema_neither_is_sent():
    databricks.polls = 0
    http, recorder = client(lambda r: entra(r) if "login" in r.url.host else databricks(r))
    found = databricks_tool(Databricks(DBX_HOST, "0000notes0000", None, None), Tokens(AZURE, http), http, sleep=lambda s: None)
    found.call({"statement": "SELECT 1"})
    post = next(r for r in recorder.requests if r.method == "POST" and r.url.host == DBX_HOST)
    assert "catalog" not in body(post) and "schema" not in body(post)


def test_a_refused_statement_is_never_sent():
    found, recorder = tool(databricks)
    with pytest.raises(ToolError, match="refused the statement before sending it: only a SELECT"):
        found.call({"statement": "DROP TABLE free_days"})
    assert not [r for r in recorder.requests if r.url.host == DBX_HOST]


def test_a_failed_statement_is_a_tool_error():
    def failing(request):
        return httpx.Response(200, json={"statement_id": "01ef-0001", "status": {"state": "FAILED", "error": {
            "error_code": "BAD_REQUEST", "message": "Table not found"}}})
    found, _ = tool(failing)
    with pytest.raises(ToolError, match="ended FAILED: Table not found"):
        found.call({"statement": "SELECT 1"})


@pytest.mark.parametrize("answer, words", [
    (httpx.Response(403, json={"error_code": "PERMISSION_DENIED", "message": "no"}), "answered 403"),
    (httpx.Response(200, text="<html>"), "not JSON"),
    (httpx.Response(200, json={"status": {"state": "PENDING"}}), "no statement id"),
    (httpx.Response(200, json={"statement_id": "../../x", "status": {"state": "PENDING"}}), "no statement id"),
    (httpx.Response(200, json={"statement_id": "01ef-0001", "status": {"state": "LOST"}}), "unknown state"),
    (httpx.Response(200, json={"statement_id": "01ef-0001"}), "unknown state"),
])
def test_an_answer_not_shaped_like_databricks_is_a_tool_error(answer, words):
    found, _ = tool(lambda r: answer)
    with pytest.raises(ToolError, match=words):
        found.call({"statement": "SELECT 1"})


@pytest.mark.parametrize("link", ["@evil.example/x", "//evil.example/api/2.0/sql/statements/01ef-0001/result/chunks/1",
                                  "https://evil.example/api/2.0/sql/statements/01ef-0001/result/chunks/1",
                                  "/api/2.0/sql/statements/01ef-0001/result/chunks/1/../../../../../2.0/jobs",
                                  "/api/2.0/sql/statements/other/result/chunks/1", "/api/2.0/sql/statements/01ef-0001/result/chunks/1\\x"])
def test_a_chunk_link_that_leaves_the_statement_is_never_followed(link):
    def answer(request):
        if request.method == "POST":
            return httpx.Response(200, json={"statement_id": "01ef-0001", "status": {"state": "SUCCEEDED"},
                                             "manifest": {"schema": {"columns": [{"name": "n"}]}},
                                             "result": {"data_array": [["1"]], "next_chunk_internal_link": link}})
        raise AssertionError(f"followed {request.url}")
    found, recorder = tool(answer)
    with pytest.raises(ToolError, match="chunk link"):
        found.call({"statement": "SELECT 1"})
    assert len([r for r in recorder.requests if r.url.host == DBX_HOST]) == 1


def test_a_long_read_is_polled_until_it_ends():
    """No poll count stops the loop (A10): 25 RUNNING answers, then the result."""
    found, _ = tool(functools.partial(databricks, pending=25))
    assert found.call({"statement": "SELECT 1"})["rows"] == [["1", "Thursday"], ["2", "Friday"]]
    assert databricks.polls == 26


def test_a_warehouse_that_can_t_be_reached_is_a_tool_error():
    def down(request):
        raise httpx.ConnectError("refused", request=request)
    found, _ = tool(down)
    with pytest.raises(ToolError, match="could not be reached"):
        found.call({"statement": "SELECT 1"})


def test_a_token_is_asked_once_per_scope_while_it_lives():
    now = [1000.0]
    http, recorder = client(entra)
    tokens = Tokens(AZURE, http, clock=lambda: now[0])
    assert tokens.token(SCOPE) == tokens.token(SCOPE) == "tok-" + SCOPE.split("/")[0]
    assert len(recorder.requests) == 1
    sent = recorder.requests[0]
    assert str(sent.url) == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    tokens.token("https://graph.microsoft.com/.default")
    assert len(recorder.requests) == 2  # another scope, another token
    now[0] += 3599 - 60 - 1
    tokens.token(SCOPE)
    assert len(recorder.requests) == 2
    now[0] += 2  # within a minute of its expiry: asked again
    tokens.token(SCOPE)
    assert len(recorder.requests) == 3


def test_the_federated_token_is_the_client_assertion(tmp_path):
    from urllib.parse import parse_qs
    (tmp_path / "token").write_text("eyJ.federated.SENTINEL\n")
    http, recorder = client(entra)
    Tokens(Azure(CLIENT, TENANT, str(tmp_path / "token"), "https://login.example.test/"), http).token(SCOPE)
    form = {k: v[0] for k, v in parse_qs(recorder.requests[0].content.decode()).items()}
    assert form["client_assertion"] == "eyJ.federated.SENTINEL" and form["client_id"] == CLIENT and form["scope"] == SCOPE
    assert recorder.requests[0].url.host == "login.example.test"


@pytest.mark.parametrize("answer, words", [(httpx.Response(400, json={"error": "invalid_client", "error_description": "SENTINEL"}), "refused"),
                                           (httpx.Response(200, json={"token_type": "Bearer"}), "no token")])
def test_a_refused_or_empty_token_answer_is_a_tool_error(answer, words):
    http, _ = client(lambda r: answer)
    with pytest.raises(ToolError, match=words) as caught:
        Tokens(AZURE, http).token(SCOPE)
    assert "SENTINEL" not in str(caught.value)


def test_an_unreadable_token_file_is_a_tool_error(tmp_path):
    http, _ = client(entra)
    with pytest.raises(ToolError, match="token file"):
        Tokens(Azure(CLIENT, TENANT, str(tmp_path / "missing"), "https://login.microsoftonline.com/"), http).token(SCOPE)


def test_a_chunk_link_seen_before_is_an_error_not_a_loop():
    link = "/api/2.0/sql/statements/01ef-0001/result/chunks/1"

    def answer(request):
        if request.method == "POST":
            return httpx.Response(200, json={"statement_id": "01ef-0001", "status": {"state": "SUCCEEDED"},
                                             "manifest": {"schema": {"columns": [{"name": "n"}]}},
                                             "result": {"data_array": [["1"]], "next_chunk_internal_link": link}})
        return httpx.Response(200, json={"chunk_index": 1, "data_array": [["2"]], "next_chunk_internal_link": link})
    found, _ = tool(answer)
    with pytest.raises(ToolError, match="again"):
        found.call({"statement": "SELECT 1"})


@pytest.mark.parametrize("expires_in, kept", [(30, 0), (59, 0), (61, 1), (86400, 3600), ("x", 0), (-5, 0)])
def test_a_token_is_kept_until_a_minute_before_it_expires_and_never_past_an_hour(expires_in, kept):
    now = [1000.0]

    def answer(request):
        return httpx.Response(200, json={"token_type": "Bearer", "expires_in": expires_in, "access_token": "tok"})
    http, recorder = client(answer)
    tokens = Tokens(AZURE, http, clock=lambda: now[0])
    tokens.token(SCOPE)
    if kept:
        now[0] += kept - 0.5
        tokens.token(SCOPE)
        assert len(recorder.requests) == 1
    now[0] += 1
    tokens.token(SCOPE)
    assert len(recorder.requests) == 2
