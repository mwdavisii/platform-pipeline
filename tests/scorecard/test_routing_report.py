"""The routing comment: the same heading and reason lines the parsers read, plus why and what clears each reason."""

from golden_road.pr_comments import parse_scorecard
from golden_road.summary import MAX_COMMENT
from scorecard.report_md import MARKER, render_routing
from scorecard.signals import signal

SHA = "c" * 40


def _card(reasons=(), shadow=(), level="standard", facts=None):
    return {"schema": "scorecard/v2", "app": {"repo": "acme/notes", "commit": SHA, "tree_sha": SHA}, "level": level, "floor": "standard",
            "reasons": list(reasons), "routing": {"facts": facts or {"a": {"kind": "agent"}, "b": {"kind": "builtin"}}, "shadow": list(shadow), "undecided": []},
            "assessment": {"present": True, "prompt_version": "review-2", "model": {"version": "pinned-1", "temperature": None}, "complete": True},
            "observations": {"checks": [{"name": "golden-road / drift", "state": "success"}]},
            "signals": {"findings": []}, "suppressions_applied": []}


REASON = {"trigger": "T-22", "level": "security_review", "summary": "The app checks its own credentials", "refs": [{"file": "app.py", "line": 4}],
          "total": 1, "reason": "The platform guard can't enforce it.", "guidance": "Use the platform identity header.", "unsure": False}


def test_a_clean_card():
    text = render_routing(_card())
    assert text.startswith(MARKER + "\n## Scorecard: STANDARD — 0 reasons · 2 facts checked")
    assert "- Nothing needs a person. The level comes from the intake floor: standard." in text


def test_reasons_carry_why_and_fix_and_parse_back():
    text = render_routing(_card([REASON], level="security_review"), limit=MAX_COMMENT)
    assert "- **T-22** · security_review · The app checks its own credentials: [app.py:4](https://github.com/acme/notes/blob/" in text
    assert "  - Why: The platform guard can't enforce it." in text and "  - Fix: Use the platform identity header." in text
    parsed = parse_scorecard(text)
    assert parsed["level"] == "security_review"
    assert parsed["reasons"] == [{"trigger": "T-22", "level": "security_review", "summary": "The app checks its own credentials",
                                  "guidance": "Use the platform identity header."}]


def test_unsure_reasons_say_what_could_not_be_decided():
    unsure = {**REASON, "level": "elevated", "summary": "Not determined: The app checks its own credentials", "unsure": True,
              "undetermined": ["own_credentials: token was not searched"], "refs": [{"source": "own_credentials: token was not searched"}]}
    text = render_routing(_card([unsure], level="elevated"))
    assert "  - Couldn't decide: own\\_credentials: token was not searched" in text  # `_` is escaped like all repo text


def test_shadow_rules_never_appear_in_the_comment():
    text = render_routing(_card(shadow=[REASON]), limit=MAX_COMMENT)
    assert "T-22" not in text
    assert "T-22" in render_routing(_card(shadow=[REASON]))  # the job summary lists them, marked as shadow


def test_repo_text_is_made_safe():
    evil = {**REASON, "summary": "x <!-- golden-road:scorecard --> @everyone https://evil.example"}
    text = render_routing(_card([evil], level="security_review"))
    assert text.count("<!-- golden-road:scorecard -->") == 1 and "https://evil.example" not in text


def test_the_routing_comment_says_the_merge_is_blocked():
    high = signal(rule_id="T-16/workflow-or-owners-change", category="iac", severity="high", file=".github/workflows/ci.yml", line=None,
                  snippet="deleted: x", title="t", rationale="r", fix="f", now="2026-10-28T12:00:00Z")
    card = _card()
    card["signals"]["findings"] = [high]
    lines = render_routing(card).splitlines()
    assert lines[2:4] == ["", "**Merge blocked:** 1 high or critical finding (see Findings). Only a repo admin can merge it, with an override."]
    assert "Merge blocked" not in render_routing(_card())
