import re

import pytest

from contracts.findings import make_finding_v2
from scorecard.redact import excerpt, redact
from scorecard.report_github import findings_check, github_actions
from scorecard.report_md import MARKER, MAX_COMMENT, render
from scorecard.signals import signal

AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
BLOCKING = ["v5.0.0-6", "v5.0.0-7", "v5.0.0-8", "v5.0.0-9", "v5.0.0-13.3"]
TEXTS = {"v5.0.0-8.2.2": "Verify that data-specific access is restricted.", "v5.0.0-8.2.1": "Verify function-level access."}
NAMES = {"v5.0.0-8": "Authorization"}


def _finding(snippet, category="authz", rationale="r", title="Debug route"):
    return make_finding_v2(
        first_seen="2026-10-21T12:00:00Z", layer="agentic", tool="scorecard-agent", rule_id="VC-4/debug-endpoint", category=category,
        severity="high", location={"kind": "line", "file": "app.py", "start_line": 3}, evidence=[{"file": "app.py", "start_line": 3, "snippet": snippet}],
        evidence_verified=True, title=title, rationale=rationale, fix={"summary": "Remove it.", "steps": []},
        standards=[{"framework": "golden-road-vc", "id": "VC-4"}],
    )


def _result(asvs_id, status, source, **extra):
    return {"schema": "requirement-result/v1", "asvs_id": asvs_id, "chapter": "v5.0.0-8", "section": "v5.0.0-8.2", "level": 1, "status": status,
            "confidence": "high", "justification": extra.pop("justification", ""), "evidence": extra.pop("evidence", []), "finding_ids": [],
            "evidence_verified": status in ("pass", "fail"), "status_source": source, **extra}


def _card(findings=(), results=(), reasons=None, level="security_review"):
    counts = {"pass": 0, "fail": 0, "na": 0, "needs_human": 0}
    for r in results:
        counts[r["status"]] += 1
    return {
        "app": {"repo": "acme/notes", "commit": "a" * 40, "tree_sha": "c" * 40}, "level": level, "floor": "standard",
        "reasons": [{"trigger": "T-6", "level": "security_review", "summary": "Debug or admin endpoint, or an auth bypass (VC-4)",
                     "refs": [{"file": "app.py", "line": 3}, {"asvs_id": "v5.0.0-8.2.2"}, {"source": "pr-body"}]}] if reasons is None else reasons,
        "asvs": {"level": 2, "coverage": 50.0, "coverage_threshold": 70, "counts": counts, "agent_requirements": 2, "platform_provided": 1,
                 "tool_covered": 0, "confirmed_na": 0, "results": list(results),
                 "chapters": [{"chapter": "v5.0.0-8", "name": "Authorization", "score": 100.0, "counts": counts}]},
        "signals": {"findings": [], "skipped_packages": [], "lookup_failures": [], "input_problems": [], "pr_text_hits": []},
        "assessment": {"present": True, "tree_sha": "c" * 40, "prompt_version": "fixture", "complete": True, "ignored_results": [],
                       "model": {"version": "1", "temperature": 0}, "findings": list(findings)},
        "observations": {"checks": [{"name": "golden-road / drift", "state": "success", "observed_at": "2026-10-21T12:00:00Z"}], "findings": []},
        "suppressions_applied": [],
    }


def _render(card, **kw):
    return render(card, TEXTS, NAMES, BLOCKING, **kw)


def test_headline_reasons_and_links():
    md = _render(_card(results=[_result("v5.0.0-8.2.2", "needs_human", "downgraded", downgrade_reason="not assessed by the agent")]))
    assert md.startswith(MARKER + "\n## Scorecard: SECURITY REVIEW — 1 reason · ASVS L2 coverage 50% · 1 needs-human")
    assert f"[app.py:3](https://github.com/acme/notes/blob/{'a' * 40}/app.py#L3)" in md
    assert "`level: security_review` check" in md
    assert "#### v5.0.0-8 Authorization" in md and "not assessed by the agent" in md


def test_secret_shaped_excerpt_is_redacted_whatever_the_category():
    """AC-16: the agent labelled it authz, and the excerpt is still redacted."""
    md = _render(_card(findings=[_finding(f"key = '{AWS}'")]))
    assert AWS not in md and "redacted: aws-access-key-id" in md


def test_data_handling_and_secrets_excerpts_are_replaced_entirely():
    finding = _finding("logger.info(patient.ssn)", category="data-handling")
    assert excerpt(finding, finding["evidence"][0]) == ["[redacted: VC-4/debug-endpoint at app.py:3]"]
    assert "patient.ssn" not in _render(_card(findings=[finding]))


def test_justifications_are_redacted_and_escaped():
    result = _result("v5.0.0-8.2.2", "needs_human", "downgraded", downgrade_reason="agent-proposed na",
                     justification=f"token={AWS}x see https://evil.example/x @security-team <!-- golden-road:scorecard --> ![img](x)", agent_status="na")
    md = _render(_card(results=[result]))
    assert AWS not in md
    assert "https://evil.example" not in md and "link removed" in md
    assert "@security-team" not in md and "@‍security-team" in md
    assert md.count("<!--") == 1  # only the code's own marker
    assert "![img]" not in md


def test_agent_passes_in_blocking_chapters_are_listed_for_spot_checks():
    evidence = [{"file": "app.py", "start_line": 3, "snippet": "if note.owner != user:"}]
    md = _render(_card(results=[_result("v5.0.0-8.2.2", "pass", "agent", evidence=evidence, justification="owner check")]))
    assert "### Agent passes to spot-check" in md and "`v5.0.0-8.2.2` at [app.py:3]" in md


def test_comment_is_truncated_with_a_pointer():
    findings = [_finding(f"line {i} " + "x" * 150, title="T" * 300) for i in range(600)]
    md = _render(_card(findings=findings), limit=MAX_COMMENT)
    assert len(md) <= MAX_COMMENT
    assert md.rstrip().endswith("_Truncated: the full report is in the job summary and the `scorecard.json` artifact._")
    assert len(_render(_card(findings=findings))) > MAX_COMMENT


def test_no_reasons_and_no_assessment():
    card = _card(reasons=[], level="standard")
    card["assessment"].update(present=False, model=None, prompt_version=None, complete=False)
    md = _render(card)
    assert "No triggers fired" in md and "No agent assessment" in md


def test_redact_patterns():
    assert redact("ghp_" + "a" * 36) == "[redacted: github-pat]"


def test_a_stateless_github_app_token_is_redacted_whole():
    """GitHub's installation tokens since 2026-10: ghs_<app id>_<JWT>, with dots. None of it, not even the App id, is left."""
    token = "ghs_1234_eyJhbGciOiJSUzI1NiJ9." + "eyJpc3MiOjEyMzR9" * 20 + "." + "s-_" * 110
    assert redact(f"Authorization: Bearer {token} ok") == "Authorization: Bearer [redacted: github-pat] ok"
    assert "hunter2hunter2" not in redact("password: hunter2hunter2")
    assert redact("plain text") == "plain text"


@pytest.mark.parametrize(
    "level, label, teams",
    [("standard", "scrutiny:standard", []), ("elevated", "scrutiny:elevated", ["acme/security-champions"]),
     ("security_review", "scrutiny:security_review", ["acme/security"])],
)
def test_github_actions(config, level, label, teams):
    actions = github_actions(level, config)
    assert actions["label"] == label and actions["review_teams"] == teams
    assert label not in actions["stale_labels"] and len(actions["stale_labels"]) == 2


def test_html_images_and_marker_lookalikes_are_removed_from_agent_text():
    finding = _finding("x", title="<b>bold</b> <!-- golden-road:scorecard --> ![i](https://evil.example/p.png) <img src=x onerror=y>",
                       rationale="<!-- unterminated")
    md = _render(_card(findings=[finding]))
    assert md.count("<!--") == 1 and "<b>" not in md and "<img" not in md and "evil.example" not in md
    assert "unterminated" not in md


def test_secrets_in_titles_fixes_and_spot_checks_are_redacted_too():
    finding = _finding("x", title=f"found {AWS}")
    finding["fix"]["summary"] = "rotate ghp_" + "a" * 36
    evidence = [{"file": "app.py", "start_line": 3, "snippet": f"key = '{AWS}'  # owner check"}]
    md = _render(_card(findings=[finding], results=[_result("v5.0.0-8.2.2", "pass", "agent", evidence=evidence, justification=f"uses {AWS}")]))
    assert AWS not in md and "ghp_" not in md


def test_spot_check_lists_the_evidence_lines():
    evidence = [{"file": "app.py", "start_line": 3, "snippet": "if note.owner != user:"}]
    md = _render(_card(results=[_result("v5.0.0-8.2.2", "pass", "agent", evidence=evidence, justification="owner check")]))
    assert "note.owner" in md.split("### Agent passes to spot-check")[1]


def test_spot_check_ignores_passes_outside_blocking_chapters_and_platform_passes():
    evidence = [{"file": "app.py", "start_line": 3, "snippet": "if note.owner != user:"}]
    other = _result("v5.0.0-16.2.5", "pass", "agent", evidence=evidence, justification="logging")
    platform = _result("v5.0.0-8.2.1", "pass", "platform")
    assert "spot-check" not in _render(_card(results=[other, platform]))


def test_private_key_blocks_are_redacted_to_the_end():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\ndef\n-----END RSA PRIVATE KEY-----\nafter"
    assert redact(text) == "[redacted: private-key]\nafter"


def test_redaction_survives_markup_inside_a_secret():
    md = _render(_card(findings=[_finding("x", title=f"AKIA<b>{AWS[4:]}</b>")]))
    assert AWS not in md and "ABCDEFGHIJKLMNOP" not in md


def test_paths_are_escaped_redacted_and_not_autolinked():
    token = "ghp_" + "a" * 36
    evidence = [{"file": f"src/{token}.py", "start_line": 3, "snippet": "if note.owner != user:"},
                {"file": "www.evil.com/login", "start_line": 4, "snippet": "if note.owner != user:"}]
    md = _render(_card(results=[_result("v5.0.0-8.2.2", "pass", "agent", evidence=evidence, justification="owner check")]))
    visible = re.sub(r"\]\(https://github\.com/[^)]*\)", "]", md)  # link targets are inert repo URLs; the displayed text is what matters
    assert token not in md and "www.evil.com" not in visible and "link removed" in visible


@pytest.mark.parametrize("text", ["a" * 290 + "AKIA" + "ABCDEFGHIJKLMNOP", "xAKIA" + "ABCDEFGHIJKLMNOP", "_ghp_" + "a" * 36])
def test_secrets_glued_to_other_text_are_redacted(text):
    assert "ABCDEFGHIJKLMNOP" not in redact(text) and "a" * 36 not in redact(text).replace("a" * 290, "")


def test_headline_needs_human_count_excludes_tool_covered():
    results = [_result("v5.0.0-8.2.2", "needs_human", "downgraded", downgrade_reason="not assessed by the agent"),
               _result("v5.0.0-8.2.3", "needs_human", "scanner", covered_by="scanner-x")]
    md = _render(_card(results=results))
    assert "· 1 needs-human" in md.splitlines()[1]
    assert md.count("### Needs human") == 1 and "`v5.0.0-8.2.3` " not in md.split("### Needs human")[1].split("###")[0]


def test_expired_suppressions_are_listed():
    md = _render(_card(), expired=["sup-old"])
    assert "### Expired suppressions (ignored)" in md and "`sup-old`" in md
    assert "Expired suppressions" not in _render(_card())


def test_refs_shown_match_the_cap_the_summary_states():
    from scorecard.level import MAX_REFS

    refs = [{"asvs_id": f"v5.0.0-8.2.{n}"} for n in range(1, MAX_REFS + 1)]
    reasons = [{"trigger": "T-12", "level": "elevated", "summary": f"Needs-human (showing {MAX_REFS} of {MAX_REFS + 5})", "refs": refs}]
    line = next(x for x in _render(_card(reasons=reasons)).splitlines() if x.startswith("- **T-12**"))
    assert line.count("`v5.0.0-8.2.") == MAX_REFS


def test_pgp_private_key_blocks_are_redacted():
    text = "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQdGBF2abc\n=ab12\n-----END PGP PRIVATE KEY BLOCK-----\nafter"
    assert redact(text) == "[redacted: private-key]\nafter"


def test_the_assessment_line_for_a_model_with_no_temperature():
    card = _card()
    card["assessment"]["model"]["temperature"] = None
    assert "Model version 1 at the provider's default temperature, prompt fixture. Complete." in _render(card)


def test_the_assessment_line_shows_the_model_version_only():
    assert "Model version 1 at temperature 0, prompt fixture. Complete." in _render(_card())


def _high(rule_id="T-16/workflow-or-owners-change", file=".github/workflows/ci.yml", line=None):
    return signal(rule_id=rule_id, category="iac", severity="high", file=file, line=line, snippet="deleted: x", title="t", rationale="r",
                  fix="f", now="2026-10-28T12:00:00Z")


def test_github_actions_without_a_card_has_no_block(config):
    actions = github_actions("standard", config)
    assert actions["blocking_findings"] == []
    assert actions["findings_check"] == {"conclusion": "success", "title": "No high or critical findings", "summary": "No high or critical findings."}


def test_github_actions_lists_the_blocking_findings(config):
    card = _card()
    card["signals"]["findings"] = [_high(), _high("T-15/inline-suppression", "app.py", 4)]
    actions = github_actions("security_review", config, card=card, who="a member of `acme/platform`")
    assert actions["blocking_findings"] == [{"rule_id": "T-15/inline-suppression", "file": "app.py", "line": 4},
                                            {"rule_id": "T-16/workflow-or-owners-change", "file": ".github/workflows/ci.yml", "line": None}]
    check = actions["findings_check"]
    assert check["conclusion"] == "failure" and check["title"] == "Merge blocked: 2 high or critical findings"
    assert "`T-15/inline-suppression` at app.py:4" in check["summary"]
    assert "a member of `acme/platform` can merge with an override" in check["summary"]


def test_the_check_lists_at_most_20():
    many = [{"rule_id": f"T-16/r{i:02d}", "location": {"file": "f.py", "start_line": i + 1}} for i in range(23)]
    summary = findings_check(many, "a repo admin")["summary"]
    assert sum(line.startswith("- `") for line in summary.splitlines()) == 20 and "- and 3 more" in summary
    assert findings_check(many[:1], "a repo admin")["title"] == "Merge blocked: 1 high or critical finding"


def test_the_comment_says_the_merge_is_blocked():
    card = _card()
    card["signals"]["findings"] = [_high()]
    lines = _render(card, override_by="a member of `acme/platform`").splitlines()
    assert lines[2:4] == ["", "**Merge blocked:** 1 high or critical finding (see Findings). Only a member of `acme/platform` can merge it, with an override."]
    assert "Merge blocked" not in _render(_card())
    assert "@acme" not in "\n".join(lines)


def test_the_check_neutralises_hostile_repo_text():
    hostile = "@everyone`<script>alert(1)</script>\u202e\nx" + "d/" * 500
    entry = {"rule_id": hostile, "location": {"file": hostile, "start_line": 3}}
    check = findings_check([entry], "a repo admin")
    text = check["title"] + "\n" + check["summary"]
    assert "@everyone" not in text and "<script" not in text and "\u202e" not in text
    line = next(x for x in check["summary"].splitlines() if x.startswith("- `"))
    assert len(line) < 600
    assert check["summary"].splitlines()[0] == line, "the newline in the text did not start a new line"
    assert check["summary"].splitlines()[1] == ""
