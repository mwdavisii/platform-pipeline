"""databricks_query (spec 2026-11-17 A6, A7; decided while planning: item 17): one read statement on the bound SQL warehouse, through
the SQL Statement Execution API, as the app's identity. Anything but one SELECT (or WITH ... SELECT) is refused before it is sent.
The statement is polled until it ends and every chunk is read: no cap (A10). A repeated chunk link is an error (it would loop).

The guard is a security control, so it fails closed: it reads the statement into tokens as Databricks' SQL lexer does, and where
that reading could go two ways (nested comments, hints, a backslash before a quote, which a raw string r'...' or a session setting
reads differently, a backslash that carries a -- comment onto the next line) it refuses rather than guess. Outside quotes and
comments the text is ASCII, so a lookalike keyword or a fullwidth semicolon is refused, not guessed at. `${` is refused anywhere,
inside quotes too: variable substitution runs before the SQL is read.

What it refuses besides one statement starting SELECT or WITH: a write, DDL or session keyword (FORBIDDEN, as a bare word; a column
of that name is quoted with backticks); a call of a function that reaches into the JVM, a secret, a model, a stream or another
system (FUNCTIONS and any AI_ function, by name followed by "(", bare, backticked or double-quoted, the last part of a dotted name
included); $ outside quotes;
IDENTIFIER, which can build any name; and a script (TRANSFORM or MAP ... USING '<command>', RECORDWRITER, RECORDREADER).

The guard is defence in depth. The real control is the bound identity's Unity Catalog grants (read-only on what the agent should see)
and the warehouse's settings; the guard fails closed and refuses anything it cannot read the same way Spark does.

Decided (review I1): read_files and path tables (delta.`/path`) are allowed: they are reads, and Unity Catalog's external-location
grants govern them. The identity's grants stay the control that matters; this keeps an agent from even asking for a write."""

import re
import time

import httpx

from .tools import Tool, ToolError

API = "/api/2.0/sql/statements"
SCOPE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default"  # the Azure Databricks resource's application id, the same in every tenant
RUNNING = frozenset({"PENDING", "RUNNING"})
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"})
FORBIDDEN = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "DROP", "ALTER", "TRUNCATE", "GRANT", "REVOKE", "COPY", "OPTIMIZE",
                       "VACUUM", "CALL", "SET", "RESET", "USE", "REFRESH", "CACHE", "UNCACHE", "MSCK", "REPAIR", "LOAD", "EXECUTE", "PUT",
                       "REMOVE", "RESTORE", "CLONE", "INTO", "IDENTIFIER", "RECORDWRITER", "RECORDREADER"})
# functions that reach into the JVM, a secret, a model, a stream or another system: not reads of the warehouse's tables
FUNCTIONS = frozenset({"REFLECT", "TRY_REFLECT", "JAVA_METHOD", "HTTP_REQUEST", "SECRET", "TRY_SECRET", "READ_KAFKA", "READ_KINESIS",
                       "READ_PUBSUB", "READ_PULSAR", "REMOTE_QUERY", "VECTOR_SEARCH", "IDENTIFIER", "PYTHON_EXEC", "LIST_SECRETS"})
FUNCTION_PREFIXES = ("AI_",)
WORD = re.compile(r"[A-Za-z0-9_]+")  # Spark's identifiers may start with a digit: 1r is one word, not a raw prefix
CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
STATEMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
PARAMETERS = {"type": "object", "additionalProperties": False, "required": ["statement"],
              "properties": {"statement": {"type": "string", "description": "One SQL SELECT (or WITH ... SELECT) statement."}}}


def _quoted_end(text: str, start: int, backslash: bool) -> int:
    """The index of the quote that closes the one at `start`, or -1. A doubled quote stays inside (Databricks reads '' as two
    adjacent literals, which covers the same text); a backslash escapes the next character when `backslash` (never in backticks)."""
    quote, i = text[start], start + 1
    while i < len(text):
        if backslash and quote != "`" and text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            if i + 1 < len(text) and text[i + 1] == quote:
                i += 2
                continue
            return i
        i += 1
    return -1


def _tokens(statement: str, backslash: bool):
    """(the statement's tokens, None) or (None, the problem). A token is ("word", WORD), ("name", a backticked identifier, `` made
    `), ("string", its text with an r/R prefix folded in), or ("punct", one character); comments and blanks make none."""
    out, i, n = [], 0, len(statement)
    while i < n:
        c = statement[i]
        if statement.startswith("--", i):
            end = next((j for j in range(i, n) if statement[j] in "\r\n"), n)  # Databricks ends one at \r or \n
            if "\\" in statement[i:end]:
                return None, "a backslash in a -- comment can carry it onto the next line: leave it out"
            i = end
        elif statement.startswith("/*", i):
            if statement.startswith("/*+", i):
                return None, "a hint (/*+ ... */) is parsed, not skipped: leave it out"
            end = statement.find("*/", i + 2)
            if end < 0:
                return None, "a comment is not closed"
            if "/*" in statement[i + 2:end + 1]:  # /*/*/ opens a second comment with the * of its first "*/"
                return None, "a comment holds /* (Databricks nests comments, so where it ends reads two ways): leave it out"
            i = end + 2
        elif c in "'\"`":
            end = _quoted_end(statement, i, backslash)
            if end < 0:
                return None, "a quoted string is not closed"
            text = statement[i + 1:end]
            out.append(("name", text.replace("``", "`")) if c == "`" else ("string", text))
            i = end + 1
        elif (found := WORD.match(statement, i)):
            word = found.group()
            i = found.end()
            if word in ("r", "R") and i < n and statement[i] in "'\"":  # a raw string's prefix (WORD took the whole word)
                end = statement.find(statement[i], i + 1)  # Spark's raw string is R'(~')*': it ends at its first quote
                if end < 0:
                    return None, "a quoted string is not closed"
                if end + 1 < n and statement[end + 1] == statement[i]:
                    return None, "a raw string followed at once by a quote reads two ways: write it another way"
                out.append(("string", statement[i + 1:end]))
                i = end + 1
            else:
                out.append(("word", word))
        elif c == "$":
            return None, "the statement holds $ outside quotes (dollar quoting reads text two ways): leave it out"
        elif c.isspace():
            if not c.isascii():
                return None, "the statement holds a character outside quotes that is not ASCII (quote a name with backticks)"
            i += 1
        else:
            if not c.isascii():
                return None, "the statement holds a character outside quotes that is not ASCII (quote a name with backticks)"
            out.append(("punct", c))
            i += 1
    return out, None


def _function(name: str) -> bool:
    last = name.rsplit(".", 1)[-1].upper()  # `system.builtin.reflect` is one backticked name; its last part is the function
    return last in FUNCTIONS or last.startswith(FUNCTION_PREFIXES)


def _problem(tokens: list) -> str | None:
    body = list(tokens)
    if body and body[-1] == ("punct", ";"):
        body.pop()
    if ("punct", ";") in body:
        return "only one statement may run"
    words = [text.upper() for kind, text in body if kind == "word"]
    if not words or words[0] not in ("SELECT", "WITH") or body[0][0] not in ("word", "punct"):
        return "only a SELECT (or WITH ... SELECT) statement may run"
    bad = sorted(set(words) & FORBIDDEN)
    if bad:
        return f"the statement names {bad[0]}, which can change data or reach outside the warehouse: only reads may run " \
               "(quote a column of that name with backticks)"
    for (kind, text), following in zip(body, body[1:]):
        if following == ("punct", "(") and _function(text):  # a string before ( is a double-quoted name: a literal never is
            return f"the statement names {text.rsplit('.', 1)[-1].upper()}, a function that reaches outside the warehouse's tables: " \
                   "only reads may run"
        if kind == "word" and text.upper() == "USING" and following[0] == "string":
            return "the statement runs a script (TRANSFORM or MAP ... USING '...'): only reads may run"
    if "SELECT" not in words:
        return "the statement names no SELECT"
    return None


def _reading(statement: str, backslash: bool):
    tokens, problem = _tokens(statement, backslash)
    return (tokens, problem) if problem else (tokens, _problem(tokens))


def read_only_problem(statement) -> str | None:
    """Why `statement` may not run, or None for one read. It is read twice, with backslash escapes and without (a raw string, or a
    warehouse that sets escapedStringLiterals): both readings must be the same one read."""
    if not isinstance(statement, str) or not statement.strip():
        return "the statement is empty (or not text)"
    if CONTROL.search(statement):
        return "the statement holds a control character"
    if "${" in statement:
        return "the statement holds ${ (variable substitution runs before the SQL is read, inside quotes too): leave it out"
    escaped, raw = _reading(statement, True), _reading(statement, False)
    if escaped == raw:
        return escaped[1]
    if escaped[1] and escaped[1] == raw[1]:
        return escaped[1]
    return "a backslash before a quote reads two ways (a raw string r'...' or a setting ends the string there): write it another way"


def _answer(response) -> dict:
    if not 200 <= response.status_code < 300:
        raise ToolError(f"Databricks answered {response.status_code}")
    try:
        doc = response.json()
    except ValueError:
        raise ToolError("Databricks' answer is not JSON") from None
    if not isinstance(doc, dict):
        raise ToolError("Databricks' answer is not an object")
    return doc


def databricks_tool(databricks, tokens, http, sleep=time.sleep) -> Tool:
    base = f"https://{databricks.host}"

    def send(method: str, path: str, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {tokens.token(SCOPE)}"}  # each time: a read may outlive a token (no time cap)
        try:
            return _answer(http.request(method, base + path, headers=headers, timeout=None, **kwargs))
        except httpx.HTTPError as exc:
            raise ToolError(f"Databricks could not be reached ({type(exc).__name__})") from None

    def call(arguments):
        statement = arguments.get("statement") if isinstance(arguments, dict) else None
        problem = read_only_problem(statement)
        if problem:
            raise ToolError(f"databricks_query refused the statement before sending it: {problem}")
        request = {"statement": statement, "warehouse_id": databricks.warehouse_id}
        request.update({key: value for key, value in (("catalog", databricks.catalog), ("schema", databricks.schema)) if value})
        request.update(wait_timeout="30s", on_wait_timeout="CONTINUE", disposition="INLINE", format="JSON_ARRAY")
        answer = send("POST", API, json=request)
        statement_id = answer.get("statement_id")
        if not isinstance(statement_id, str) or not STATEMENT_ID.fullmatch(statement_id):
            raise ToolError("Databricks' answer has no statement id")

        def state(doc):
            found = (doc.get("status") or {}).get("state") if isinstance(doc.get("status"), dict) else None
            if found not in RUNNING | TERMINAL:
                raise ToolError("Databricks answered an unknown state for the statement")
            return found

        while state(answer) in RUNNING:  # no cap: the warehouse decides how long a read takes (A10)
            sleep(1)
            answer = send("GET", f"{API}/{statement_id}")
        status = answer["status"]
        if status["state"] != "SUCCEEDED":
            error = status.get("error") if isinstance(status.get("error"), dict) else {}
            # A short error summary, not data: the cut is not a token or time budget (no budgets, A10).
            raise ToolError(f"the statement ended {status['state']}: {str(error.get('message', ''))[:300]}")
        schema = (answer.get("manifest") or {}).get("schema") or {}
        columns = [c.get("name") for c in schema.get("columns") or [] if isinstance(c, dict)]
        chunk = re.compile(re.escape(f"{API}/{statement_id}/result/chunks/") + r"[0-9]+(\?[A-Za-z0-9=&._-]*)?")
        result = answer.get("result") or {}
        rows = list(result.get("data_array") or [])
        seen = set()
        while result.get("next_chunk_internal_link"):  # every chunk, however many (A10)
            link = result["next_chunk_internal_link"]
            if not isinstance(link, str) or not chunk.fullmatch(link):
                raise ToolError("Databricks' next chunk link leaves the statement's results: not followed")
            if link in seen:  # protocol correctness, not a cap: a link seen before would loop forever
                raise ToolError("Databricks named a chunk link again: the result can't be read to its end")
            seen.add(link)
            result = send("GET", link)
            rows += result.get("data_array") or []
        return {"columns": columns, "rows": rows}

    return Tool("databricks_query", "Run one read-only SQL statement (SELECT, or WITH ... SELECT) on this app's Databricks warehouse. "
                "Quote a column named like a SQL keyword with backticks.", PARAMETERS, call)
