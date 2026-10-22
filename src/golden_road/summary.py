"""Markdown for the job summary and PR comment. Every scanner-supplied string is escaped and flattened."""

import re
import unicodedata

ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]
MAX_SHOWN = 100
MAX_FIELD = 300
MAX_COMMENT = 60_000  # GitHub's limit is 65,536; leave headroom
_MORE_RESERVE = 200  # room for the trailing "N more findings" line
# Every field is rendered mid-line after a fixed prefix, so `-`, `>` and `+` can't start a list or
# quote, and `<` is HTML-escaped so a bare `>` can't close anything. Quotes are safe too, since no
# field is ever placed in an attribute. They stay readable.
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#!|~])")
_UNIT = re.compile(r"\\.|&amp;|&lt;|@\u200d|.", re.DOTALL)


def _md(text, limit: int = MAX_FIELD) -> str:
    """Escape one field, then cut it to `limit` characters on an escape boundary."""
    clean = str(text).encode("utf-8", "replace").decode("utf-8")
    # Format and control characters (bidi overrides, zero-width, ...) could spoof what a reader sees; the only joiner
    # in the output is the one inserted below to neutralize `@`.
    clean = "".join(c for c in clean if c.isspace() or unicodedata.category(c) not in ("Cf", "Cc"))
    flat = " ".join(clean.split())
    escaped = flat.replace("&", "&amp;").replace("<", "&lt;").replace("@", "@\u200d")
    escaped = _MD_SPECIAL.sub(r"\\\1", escaped)
    out, size = [], 0
    for unit in _UNIT.findall(escaped):
        if size + len(unit) > limit:
            break
        out.append(unit)
        size += len(unit)
    return "".join(out)


def _where(finding: dict) -> str:
    loc = finding["location"]
    line = f":{loc['start_line']}" if loc.get("start_line") else ""
    return f"{_md(loc['file'])}{line}"


def _block(finding: dict, mark: str) -> str:
    lines = [f"- {mark}**{finding['severity']}** `{_md(finding['rule_id'])}` at {_where(finding)}: {_md(finding['title'])}",
             f"  - Fix: {_md(finding['fix']['summary'])}"]
    for step in finding["fix"]["steps"][:5]:
        lines.append(f"    - {_md(step)}")
    return "\n".join(lines) + "\n"


def render(gate: dict, findings: list[dict]) -> str:
    status = "PASS" if gate["status"] == "pass" else "FAIL"
    blocking = set(gate["blocking"])
    overridden = set(gate["overrides_applied"])
    head = [f"## Golden road gate: {status}", "", "| Severity | Count |", "|---|---|"]
    by_severity = gate["counts"]["by_severity"]
    for severity in SEVERITY_ORDER:
        head.append(f"| {severity} | {_md(by_severity.get(severity, 0))} |")
    if overridden:
        head += ["", f"Overrides applied: {len(overridden)}"]
    ordered = sorted(findings, key=lambda f: (f["id"] not in blocking, ORDER[f["severity"]], f["rule_id"], f["id"]))
    if ordered:
        head += ["", "### Findings", ""]
    out = "\n".join(head) + "\n"
    shown = 0
    for finding in ordered[:MAX_SHOWN]:
        mark = "BLOCKING " if finding["id"] in blocking else "OVERRIDDEN " if finding["id"] in overridden else ""
        block = _block(finding, mark)
        if len(out) + len(block) + _MORE_RESERVE > MAX_COMMENT:
            break
        out += block
        shown += 1
    if shown < len(ordered):
        out += f"- {len(ordered) - shown} more findings not shown; see the workflow artifact.\n"
    return out


_LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "notice", "info": "notice"}
MAX_ANNOTATIONS = 50


def _cmd_data(text: str) -> str:
    return str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _cmd_prop(text: str) -> str:
    return _cmd_data(text).replace(":", "%3A").replace(",", "%2C")


def annotations(findings: list[dict]) -> list[str]:
    """GitHub workflow commands that put findings on the PR's Files tab."""
    lines = []
    for finding in sorted(findings, key=lambda f: (ORDER[f["severity"]], f["rule_id"], f["id"]))[:MAX_ANNOTATIONS]:
        location = finding["location"]
        props = []
        if location["file"] != "<unknown>":
            line = location.get("start_line")
            if not line and finding["evidence"] and finding["evidence"][0]["file"] == location["file"]:
                line = finding["evidence"][0]["start_line"]
            props.append(f"file={_cmd_prop(location['file'])}")
            if line:
                props.append(f"line={line}")
        props.append(f"title={_cmd_prop(finding['rule_id'])}")
        message = _cmd_data(f"{finding['title']}: {finding['fix']['summary']}"[:1000])
        lines.append(f"::{_LEVEL[finding['severity']]} {','.join(props)}::{message}")
    return lines
