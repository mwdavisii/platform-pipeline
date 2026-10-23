"""Parsers for the platform's five PR comments (drift, preview, semgrep, trivy, scorecard).

They are pure: text in, a small dict out, never an exception (a line that does not parse is skipped). Every regex is
anchored and bounded, so no input makes them slow. The comments are evidence only (see golden_road.remediate), and each
parser is tested by round-tripping the real renderer, so a renderer change that breaks one fails a test.
"""

import re

MARKERS = {"drift": "<!-- golden-road:drift -->", "preview": "<!-- golden-road:preview -->",
           "semgrep": "<!-- security-scans:semgrep -->", "trivy": "<!-- security-scans:trivy -->",
           "scorecard": "<!-- golden-road:scorecard -->"}

MAX_TEXT = 70_000
MAX_LINES = 2_000
_UNESCAPE = re.compile(r"\\(.)|&lt;|&amp;|@\u200d", re.DOTALL)
_TICKS = re.compile(r"`+")


def _text(value) -> str:
    return value[:MAX_TEXT] if isinstance(value, str) else ""


def _lines(value) -> list[str]:
    return _text(value).splitlines()[:MAX_LINES]


def unescape_md(text: str) -> str:
    """Undo golden_road.summary._md: a backslash escape, &lt;, &amp; and the joiner after @."""
    def one(match):
        if match.group(1) is not None:
            return match.group(1)
        return {"&lt;": "<", "&amp;": "&", "@\u200d": "@"}[match.group(0)]
    return _UNESCAPE.sub(one, text)


def parse_code_span(line: str, at: int) -> tuple[str, int] | None:
    """The CommonMark code span that starts at `at`: a run of n backticks, text, then a run of exactly n backticks.
    One space is stripped from each side when both sides have one. Returns (text, index after the span)."""
    if not isinstance(line, str) or not 0 <= at < len(line) or line[at] != "`":
        return None
    opener = _TICKS.match(line, at)
    n = len(opener.group(0))
    for run in _TICKS.finditer(line, opener.end()):
        if len(run.group(0)) == n:
            text = line[opener.end():run.start()]
            if len(text) >= 2 and text[0] == " " and text[-1] == " " and text.strip(" "):
                text = text[1:-1]
            return text, run.end()
    return None


_SCAN_HEAD = re.compile(r"### security-scans / ([a-z][a-z0-9-]{0,30}): (FAILED|passed)")
_SCAN_MORE = re.compile(r"\.\.\.and [0-9]{1,9} more not listed here\.")
_SCAN_ITEM = re.compile(r"- \*\*(error|warning|note)\*\* (?=`)")


def _where(text: str) -> tuple[str, int | None]:
    head, sep, tail = text.rpartition(":")
    if sep and head and tail.isdigit() and len(tail) <= 9 and int(tail) >= 1:
        return head, int(tail)
    return text, None


def _scan_item(line: str) -> dict | None:
    match = _SCAN_ITEM.match(line)
    if not match:
        return None
    rule = parse_code_span(line, match.end())
    if not rule or not line.startswith(" in ", rule[1]):
        return None
    at = rule[1] + 4
    if line.startswith("(no location)", at):
        file, number, at = "", None, at + len("(no location)")
    else:
        where = parse_code_span(line, at)
        if not where:
            return None
        (file, number), at = _where(where[0]), where[1]
    if not line.startswith(": ", at):
        return None
    message = parse_code_span(line, at + 2)
    if not message:
        return None
    return {"level": match.group(1), "rule_id": rule[0], "file": file, "line": number, "message": message[0]}


def parse_scan(text) -> dict:
    out = {"tool": None, "failed": False, "unfinished": False, "truncated": False, "findings": []}
    for line in _lines(text):
        head = _SCAN_HEAD.fullmatch(line) if out["tool"] is None else None
        if head:
            out["tool"], out["failed"] = head.group(1), head.group(2) == "FAILED"
        elif line.startswith("- "):
            item = _scan_item(line)
            if item:
                out["findings"].append(item)
        elif line.startswith(("Note: the scan didn't finish", "No usable results were produced")):
            out["unfinished"] = True
        elif _SCAN_MORE.match(line) or line == "(truncated)":
            out["truncated"] = True
    if out["unfinished"]:
        out["failed"] = True
    return out


_DRIFT_HEAD = re.compile(r"## Golden road gate: (PASS|FAIL)")
_DRIFT_ITEM = re.compile(r"- (BLOCKING |OVERRIDDEN )?\*\*([a-z]{1,20})\*\* `((?:\\.|[^`\\]){1,200})` at (.{1,400}?)(?::([0-9]{1,9}))?: ")


def parse_drift(text) -> dict:
    out = {"failed": False, "findings": []}
    for line in _lines(text):
        head = _DRIFT_HEAD.fullmatch(line)
        if head:
            out["failed"] = head.group(1) == "FAIL"
            continue
        match = _DRIFT_ITEM.match(line)
        if match:
            blocking, _, rule, file, number = match.groups()
            out["findings"].append({"rule_id": unescape_md(rule), "file": unescape_md(file),
                                    "line": int(number) if number else None, "blocking": blocking == "BLOCKING "})
    return out


_PREVIEW_HEAD = re.compile(r"## Golden road PR preview: (PASS|FAIL|DID NOT RUN|CANCELLED)")
_PREVIEW_STATE = {"PASS": "pass", "FAIL": "fail", "DID NOT RUN": "did-not-run", "CANCELLED": "cancelled"}
# A sentence under a PASS or FAIL heading that names a more specific state.
_PREVIEW_SENTENCE = (("The image built. This app declares no `authz`", "not-declared"),
                     ("There is no probe result:", "no-result"),
                     ("The probe result was a pass, but the preview job failed", "job-failed"))
_PREVIEW_MORE = re.compile(r"- [0-9]{1,9} more not shown; see the workflow artifact\.")


def parse_preview(text) -> dict:
    out = {"state": "unknown", "failures": []}
    in_failures = False
    for line in _lines(text):
        head = _PREVIEW_HEAD.fullmatch(line) if out["state"] == "unknown" else None
        if head:
            out["state"] = _PREVIEW_STATE[head.group(1)]
        elif line.startswith("### "):
            in_failures = line == "### Requests the guard should have denied"
        elif in_failures and line.startswith("- "):
            if not _PREVIEW_MORE.fullmatch(line):
                out["failures"].append(unescape_md(line[2:])[:400])
        elif out["state"] in ("pass", "fail"):
            out["state"] = next((state for prefix, state in _PREVIEW_SENTENCE if line.startswith(prefix)), out["state"])
    if out["state"] != "fail":
        out["failures"] = []
    return out


_SC_HEAD = re.compile(r"## Scorecard: (STANDARD|ELEVATED|SECURITY REVIEW)\b")
_SC_REASON = re.compile(r"- \*\*(T-[0-9]{1,3})\*\* · (standard|elevated|security_review) · (.{0,1500})")
_SC_REF_START = re.compile(r": (?=\[|`)")


def parse_scorecard(text) -> dict:
    out = {"level": None, "reasons": []}
    in_reasons = False
    for line in _lines(text):
        head = _SC_HEAD.match(line) if out["level"] is None else None
        if head:
            out["level"] = head.group(1).lower().replace(" ", "_")
        elif line.startswith("### "):
            in_reasons = line == "### Reasons"
        elif in_reasons:
            match = _SC_REASON.fullmatch(line)
            if match:
                rest = _SC_REF_START.split(match.group(3), maxsplit=1)[0]  # an unescaped "[" or backtick can only start a reference
                out["reasons"].append({"trigger": match.group(1), "level": match.group(2), "summary": unescape_md(rest)[:300]})
    return out
