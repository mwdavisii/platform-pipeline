"""scorecard.md: rendered by code from the validated scorecard. Agent text is data.

Every agent- or repo-supplied string is redacted, has links removed, and is markdown-escaped
(`@` mentions, HTML, images, and marker comments can't survive `_md`). The output is cut to the
size limit with a pointer to the job summary and the artifact.
"""

import re
import urllib.parse

from golden_road.summary import MAX_COMMENT, _md

from .asvs import in_any
from .config import temperature_phrase
from .level import MAX_REFS
from .redact import MAX_EXCERPT_LINES, excerpt, redact

MARKER = "<!-- golden-road:scorecard -->"
SUMMARY_LIMIT = 1_000_000
HEADLINE = {"standard": "STANDARD", "elevated": "ELEVATED", "security_review": "SECURITY REVIEW"}
TRUNCATED = "\n_Truncated: the full report is in the job summary and the `scorecard.json` artifact._\n"
_URL = re.compile(r"(?i)(?:\b[a-z][a-z0-9+.-]*://|\bwww\.|\bmailto:|\bjavascript:|\bdata:)\S*")
# Removed before escaping, so none of them can survive as markup: marker-style comments (also unterminated ones),
# images, and HTML tags.
_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")


def safe(text, limit: int = 300) -> str:
    """Agent- or repo-supplied text made safe to render: redacted, comments, images, HTML, and links removed, escaped."""
    # Strip first, then redact: removing markup can join the halves of a secret that was split by it.
    text = _COMMENT.sub("", str(text))
    text = _IMAGE.sub("[image removed]", text)
    text = _TAG.sub("", text)
    return _md(redact(_URL.sub("[link removed]", text)), limit)


def _pct(value) -> str:
    return "n/a" if value is None else f"{value:g}%"


def _link(card: dict, server_url: str, file: str, line: int | None) -> str:
    label = safe(file, 200) + (f":{line}" if line else "")
    if file == "<unknown>" or redact(file) != file:
        return label  # no link for a path that looks like a secret: the URL would carry it
    url = f"{server_url.rstrip('/')}/{card['app']['repo']}/blob/{card['app']['commit']}/{urllib.parse.quote(file)}"
    return f"[{label}]({url}{f'#L{line}' if line else ''})"


def _ref(card: dict, server_url: str, ref: dict) -> str:
    if "file" in ref:
        return _link(card, server_url, ref["file"], ref.get("line"))
    if "asvs_id" in ref:
        return f"`{ref['asvs_id']}`"
    return safe(ref.get("source", "finding"), 120)


def _human_items(card: dict) -> list[dict]:
    """The needs-human results a person must look at: tool-covered ones belong to their tool team and are not listed."""
    return [r for r in card["asvs"]["results"] if r["status"] == "needs_human" and r["status_source"] != "scanner"]


def _headline(card: dict) -> str:
    needs = len(_human_items(card))
    n = len(card["reasons"])
    return (f"## Scorecard: {HEADLINE[card['level']]} — {n} reason{'s' if n != 1 else ''} · "
            f"ASVS L{card['asvs']['level']} coverage {_pct(card['asvs']['coverage'])} · {needs} needs-human")


def _needs_human(card: dict, texts: dict[str, str], names: dict[str, str]) -> list[str]:
    items = _human_items(card)
    if not items:
        return []
    lines = ["", "### Needs human", ""]
    chapter = None
    for r in items:
        if r["chapter"] != chapter:
            chapter = r["chapter"]
            lines += [f"#### {chapter} {safe(names.get(chapter, ''))}", ""]
        lines.append(f"- `{r['asvs_id']}` {safe(texts.get(r['asvs_id'], ''), 400)}")
        lines.append(f"  - Why: {safe(r.get('downgrade_reason') or r['justification'] or 'not decided by code')}")
        if r.get("agent_status") and r["justification"]:
            lines.append(f"  - The agent said {r['agent_status']}: {safe(r['justification'])}")
    return lines


def _spot_checks(card: dict, server_url: str, blocking: list[str]) -> list[str]:
    passes = [r for r in card["asvs"]["results"] if r["status"] == "pass" and r["status_source"] == "agent" and in_any(r["asvs_id"], blocking)]
    if not passes:
        return []
    lines = ["", "### Agent passes to spot-check", "", "verify_evidence proves the cited lines exist, not that they support the pass.", ""]
    for r in passes:
        where = ", ".join(_link(card, server_url, a["file"], a["start_line"]) for a in r["evidence"][:5])
        lines.append(f"- `{r['asvs_id']}` at {where}: {safe(r['justification'])}")
        for a in r["evidence"][:3]:
            lines.append(f"  - Evidence, {safe(a['file'], 200)}:{a['start_line']}: " + " ⏎ ".join(safe(s, 200) for s in redact(a["snippet"]).splitlines()[:MAX_EXCERPT_LINES]))
    return lines


def _findings(card: dict, server_url: str) -> list[str]:
    findings = card["signals"]["findings"] + card["assessment"]["findings"]
    if not findings:
        return []
    suppressed = {s["finding_id"] for s in card["suppressions_applied"]}
    lines = ["", "### Findings", ""]
    for f in sorted(findings, key=lambda f: (f["rule_id"], f["location"]["file"], f["id"])):
        loc = f["location"]
        mark = " (suppressed in the factory)" if f["id"] in suppressed else "" if f["evidence_verified"] else " (evidence not verified)"
        lines.append(f"- **{f['severity']}** `{safe(f['rule_id'], 80)}` at {_link(card, server_url, loc['file'], loc.get('start_line'))}: "
                     f"{safe(f['title'])}{mark}")
        shown = [safe(line, 200) for line in excerpt(f, f["evidence"][0])] if f["evidence"] else []
        if shown:
            lines.append("  - Excerpt: " + " ⏎ ".join(shown))
        lines.append(f"  - Fix: {safe(f['fix']['summary'])}")
    return lines


def _expired(expired: list[str]) -> list[str]:
    if not expired:
        return []
    return ["", "### Expired suppressions (ignored)", "", "These no longer suppress anything. Remove them from the factory or renew them.", "",
            *[f"- `{safe(i, 80)}`" for i in expired]]


def render(card: dict, texts: dict[str, str], names: dict[str, str], blocking: list[str], *,
           server_url: str = "https://github.com", limit: int = SUMMARY_LIMIT, expired: list[str] = ()) -> str:
    """Markdown for the job summary (default limit) or the PR comment (limit=MAX_COMMENT)."""
    app, asvs = card["app"], card["asvs"]
    lines = [MARKER, _headline(card), "",
             f"Assessed tree `{app['tree_sha']}` at commit `{app['commit']}`. The authoritative level is the "
             f"`level: {card['level']}` check from the scorecard run. This comment is a view: removing the label changes nothing."]
    lines += ["", "### Reasons", ""]
    if not card["reasons"]:
        lines.append(f"- No triggers fired. Level comes from the intake floor: {card['floor']}.")
    for reason in card["reasons"]:
        refs = ", ".join(_ref(card, server_url, r) for r in reason["refs"][:MAX_REFS])
        lines.append(f"- **{reason['trigger']}** · {reason['level']} · {safe(reason['summary'])}{': ' + refs if refs else ''}")
    lines += _needs_human(card, texts, names)
    lines += _spot_checks(card, server_url, blocking)
    lines += _findings(card, server_url)
    lines += _expired(list(expired))
    lines += ["", "### Gate 1 (context, not re-scored)", "", "| Check | State |", "|---|---|"]
    lines += [f"| {safe(c['name'], 120)} | {c['state']} |" for c in card["observations"]["checks"]]
    lines += ["", f"### ASVS 5.0.0 L{asvs['level']}", "", "| Chapter | Score | Pass | Fail | N/A | Needs human |", "|---|---|---|---|---|---|"]
    for c in asvs["chapters"]:
        n = c["counts"]
        lines.append(f"| {c['chapter']} {safe(c['name'], 80)} | {_pct(c['score'])} | {n['pass']} | {n['fail']} | {n['na']} | {n['needs_human']} |")
    lines += ["", f"Agent-assessed: {asvs['agent_requirements']} · Platform-provided: {asvs['platform_provided']} · "
              f"Covered by owned tools: {asvs['tool_covered']} · Confirmed N/A: {asvs['confirmed_na']} · Coverage threshold: {asvs['coverage_threshold']:g}%"]
    covered = [r for r in asvs["results"] if r["status_source"] == "scanner"]
    if covered:
        lines += ["", "### Covered by owned tools", ""]
        lines += [f"- `{r['asvs_id']}` covered by {safe(r['covered_by'], 80)}" for r in covered]
    a = card["assessment"]
    lines += ["", "### Assessment", ""]
    if a["present"]:
        lines.append(f"Model version {safe(a['model']['version'], 40)} at {temperature_phrase(a['model']['temperature'])}, "
                     f"prompt {safe(a['prompt_version'], 64)}. {'Complete.' if a['complete'] else 'Incomplete.'}")
    else:
        lines.append("No agent assessment. Every requirement for the agent is needs-human (T-14).")
    lines.append("The deterministic sections are reproducible. The agent's results are not guaranteed to be.")
    return _fit(lines, limit)


def _fit(lines: list[str], limit: int) -> str:
    out, size = [], 0
    for line in lines:
        if size + len(line) + 1 + len(TRUNCATED) > limit:
            return "\n".join(out) + "\n" + TRUNCATED
        out.append(line)
        size += len(line) + 1
    return "\n".join(out) + "\n"


__all__ = ["MARKER", "MAX_COMMENT", "render", "safe"]


def render_routing(card: dict, *, server_url: str = "https://github.com", limit: int = SUMMARY_LIMIT, expired=()) -> str:
    return _fit([MARKER, f"## Scorecard: {HEADLINE[card['level']]}"], limit)
