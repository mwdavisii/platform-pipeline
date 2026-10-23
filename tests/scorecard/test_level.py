import pytest

from contracts.findings import make_finding_v2
from golden_road.io import InputError
from scorecard.level import SUMMARIES, decide, triggers_of

NOTES = {"tier": "moderate", "data_classification": "internal"}
FLAGS = {"incomplete": False, "unverified": [], "ignored": []}


def _decide(config, **kw):
    args = dict(config=config, intake=NOTES, spec_classification=None, results=[], findings=[], covered=10, total=10, flags=FLAGS,
                lookup_failures=[], pr_text_hits=[])
    args.update(kw)
    return decide(**args)


def _result(asvs_id, status, source="agent"):
    return {"asvs_id": asvs_id, "status": status, "status_source": source}


def _finding(standards, layer="deterministic", verified=True):
    return make_finding_v2(
        first_seen="2026-10-21T12:00:00Z", layer=layer, tool="t", rule_id="r", category="authz", severity="high",
        location={"kind": "line", "file": "app.py", "start_line": 4}, evidence=[{"file": "app.py", "start_line": 4, "snippet": "x = 1"}],
        evidence_verified=verified, title="t", rationale="r", fix={"summary": "s", "steps": []}, standards=standards,
    )


def _triggers(reasons):
    return [r["trigger"] for r in reasons]


def test_clean_moderate_change_is_standard(config):
    assert _decide(config) == ("standard", "standard", [])


def test_high_tier_is_always_security_review(config):
    """AC-8."""
    level, floor, reasons = _decide(config, intake={"tier": "high", "data_classification": "public"})
    assert (level, floor, reasons) == ("security_review", "security_review", [])


def test_missing_intake_is_t8(config):
    level, _, reasons = _decide(config, intake=None)
    assert level == "security_review" and _triggers(reasons) == ["T-8"]


def test_verified_fail_in_a_blocking_chapter_is_t1(config):
    level, _, reasons = _decide(config, results=[_result("v5.0.0-8.2.2", "fail"), _result("v5.0.0-16.2.5", "fail")])
    assert level == "security_review" and _triggers(reasons) == ["T-1"]
    assert reasons[0]["refs"] == [{"asvs_id": "v5.0.0-8.2.2"}]


def test_needs_human_in_a_blocking_chapter_is_t12_but_tool_covered_is_not(config):
    level, _, reasons = _decide(config, results=[_result("v5.0.0-6.2.1", "needs_human", "downgraded"), _result("v5.0.0-13.3.1", "needs_human", "scanner")])
    assert level == "elevated" and reasons[0]["refs"] == [{"asvs_id": "v5.0.0-6.2.1"}]


def test_unverified_citation_raises_to_elevated(config):
    """AC-13 (level half)."""
    flags = {**FLAGS, "unverified": [{"asvs_id": "v5.0.0-16.2.5"}]}
    level, _, reasons = _decide(config, flags=flags)
    assert level == "elevated" and _triggers(reasons) == ["T-12"]


@pytest.mark.parametrize(
    "standards, trigger, level",
    [
        ([{"framework": "golden-road-trigger", "id": "T-2"}], "T-2", "security_review"),
        ([{"framework": "golden-road-vc", "id": "VC-1"}], "T-3", "security_review"),
        ([{"framework": "golden-road-vc", "id": "VC-3"}, {"framework": "cwe", "id": "CWE-1357"}], "T-5", "security_review"),
        ([{"framework": "golden-road-vc", "id": "VC-INJ"}], "T-7", "security_review"),
        ([{"framework": "golden-road-trigger", "id": "T-9"}], "T-9", "elevated"),
        ([{"framework": "golden-road-trigger", "id": "T-15"}], "T-15", "security_review"),
        ([{"framework": "golden-road-trigger", "id": "T-16"}], "T-16", "security_review"),
        ([{"framework": "golden-road-trigger", "id": "T-17"}], "T-17", "elevated"),
        ([{"framework": "golden-road-trigger", "id": "T-18"}], "T-18", "elevated"),
    ],
)
def test_findings_fire_their_triggers(config, standards, trigger, level):
    finding = _finding(standards)
    assert triggers_of(finding) == {trigger}
    got_level, _, reasons = _decide(config, findings=[finding])
    assert got_level == level and _triggers(reasons) == [trigger]
    assert reasons[0]["refs"] == [{"finding_id": finding["id"], "file": "app.py", "line": 4}]


def test_vc6_is_security_review_for_sensitive_data(config):
    finding = _finding([{"framework": "golden-road-vc", "id": "VC-6"}], layer="agentic")
    assert _decide(config, findings=[finding])[0] == "elevated"
    assert _decide(config, findings=[finding], intake={"tier": "moderate", "data_classification": "phi"})[0] == "security_review"


def test_unverified_agent_finding_does_not_fire_its_own_trigger(config):
    finding = _finding([{"framework": "golden-road-vc", "id": "VC-4"}], layer="agentic", verified=False)
    assert _decide(config, findings=[finding]) == ("standard", "standard", [])


def test_coverage_incomplete_and_lookup_failures(config):
    level, _, reasons = _decide(config, covered=5, total=10, flags={**FLAGS, "incomplete": True},
                                lookup_failures=[{"source": "pypi:requests==1.0", "detail": "timeout"}])
    assert level == "elevated" and _triggers(reasons) == ["T-11", "T-14"]
    assert reasons[1]["summary"].endswith("(2 items)")
    _, _, reasons = _decide(config, covered=0, total=0)
    assert _triggers(reasons) == ["T-11"]


def test_pr_text_hits_fire_t7(config):
    level, _, reasons = _decide(config, pr_text_hits=[{"source": "pr-body", "rule": "VC-INJ/reviewer-directive"}])
    assert level == "security_review" and reasons[0]["refs"] == [{"source": "pr-body"}]


def test_triggers_only_raise(config):
    level, _, _ = _decide(config, intake={"tier": "high", "data_classification": "public"}, covered=1, total=10)
    assert level == "security_review"


@pytest.mark.parametrize("source", ["package-lock.json", "docs/big.bin", "tests/test_a.py", "pypi:x==1"])
def test_every_kind_of_problem_fires_t14(config, source):
    """tree_signals, change_signals, read_packages, and dependency_signals all report {source, detail}."""
    level, _, reasons = _decide(config, problems=[{"source": source, "detail": "can't read"}])
    assert level == "elevated" and _triggers(reasons) == ["T-14"] and reasons[0]["refs"] == [{"source": source}]


def test_agent_finding_cannot_name_a_trigger_directly(config):
    finding = _finding([{"framework": "golden-road-vc", "id": "VC-4"}, {"framework": "golden-road-trigger", "id": "T-16"}], layer="agentic")
    assert triggers_of(finding) == {"T-6"}
    only_trigger = _finding([{"framework": "golden-road-trigger", "id": "T-16"}], layer="agentic")
    assert triggers_of(only_trigger) == set()


def test_floor_is_max_and_reasons_are_capped_and_sorted(config):
    findings = [make_finding_v2(first_seen="2026-10-21T12:00:00Z", layer="deterministic", tool="t", rule_id=f"r{i}", category="authz",
                                severity="high", location={"kind": "line", "file": "app.py", "start_line": i + 1},
                                evidence=[{"file": "app.py", "start_line": i + 1, "snippet": f"x = {i}"}], evidence_verified=True,
                                title="t", rationale="r", fix={"summary": "s", "steps": []},
                                standards=[{"framework": "golden-road-trigger", "id": "T-9"}]) for i in range(25)]
    level, floor, reasons = _decide(config, findings=findings, intake=None, covered=0, total=10)
    assert (level, floor) == ("security_review", "standard")
    assert _triggers(reasons) == ["T-8", "T-9", "T-11"]
    assert len(reasons[1]["refs"]) == 20 and reasons[1]["summary"].endswith("(showing 20 of 25)")
    assert all(not r["trigger"] == "T-13" for r in reasons)


def test_unconfirmed_agent_na_outside_blocking_chapter_is_quiet_and_inside_is_t12(config):
    quiet = _decide(config, results=[_result("v5.0.0-16.2.5", "needs_human", "downgraded")])
    assert quiet == ("standard", "standard", [])
    loud = _decide(config, results=[_result("v5.0.0-8.2.2", "needs_human", "downgraded")])
    assert _triggers(loud[2]) == ["T-12"]


def test_failed_agent_citation_in_any_chapter_is_t12(config):
    flags = {**FLAGS, "unverified": [{"asvs_id": "v5.0.0-16.2.5"}, {"finding_id": "a" * 64}]}
    _, _, reasons = _decide(config, flags=flags)
    assert _triggers(reasons) == ["T-12"] and len(reasons[0]["refs"]) == 2



def test_t11_compares_raw_counts_not_the_rounded_percentage(config):
    """142 of 203 is 69.95%, which displays as 70.0 but is below 70."""
    _, _, reasons = _decide(config, covered=142, total=203)
    assert _triggers(reasons) == ["T-11"]
    assert _decide(config, covered=7, total=10)[2] == []
    assert _decide(config, covered=141, total=200)[2] == []


def test_every_vc_standard_fires_its_trigger(config):
    finding = _finding([{"framework": "golden-road-vc", "id": "VC-5"}, {"framework": "golden-road-vc", "id": "VC-4"}], layer="agentic")
    assert triggers_of(finding) == {"T-9", "T-6"}
    level, _, reasons = _decide(config, findings=[finding])
    assert _triggers(reasons) == ["T-6", "T-9"] and level == "security_review"


def test_unknown_vc_id_is_an_error_not_a_skip():
    finding = _finding([{"framework": "golden-road-vc", "id": "VC-3"}])
    finding["standards"] = [{"framework": "golden-road-vc", "id": "VC-9"}]
    with pytest.raises(InputError):
        triggers_of(finding)


def test_agent_finding_that_maps_to_no_trigger_fires_t12(config):
    finding = _finding([{"framework": "cwe", "id": "CWE-79"}], layer="agentic")
    level, _, reasons = _decide(config, findings=[finding])
    assert level == "elevated" and _triggers(reasons) == ["T-12"]
    assert reasons[0]["refs"] == [{"finding_id": finding["id"], "file": "app.py", "line": 4}]
    # A deterministic finding with no trigger is not suspicious.
    assert _decide(config, findings=[_finding([{"framework": "cwe", "id": "CWE-79"}])])[2] == []


def test_classification_is_the_more_sensitive_of_intake_and_spec(config):
    finding = _finding([{"framework": "golden-road-vc", "id": "VC-6"}], layer="agentic")
    phi = {"tier": "moderate", "data_classification": "phi"}
    assert _decide(config, findings=[finding], intake=phi, spec_classification="internal")[0] == "security_review"
    internal = {"tier": "moderate", "data_classification": "internal"}
    assert _decide(config, findings=[finding], intake=internal, spec_classification="restricted")[0] == "security_review"
    assert _decide(config, findings=[finding], intake=internal, spec_classification="internal")[0] == "elevated"
    assert _decide(config, findings=[finding], intake=None, spec_classification="phi")[0] == "security_review"


def test_t12_refs_are_deduped_ordered_and_capped(config):
    flags = {**FLAGS, "unverified": [{"asvs_id": "v5.0.0-8.2.2"}, {"asvs_id": "v5.0.0-8.2.2"}]}
    results = [_result("v5.0.0-8.2.2", "needs_human", "downgraded"), _result("v5.0.0-6.2.1", "needs_human", "downgraded")]
    _, _, reasons = _decide(config, flags=flags, results=results)
    assert reasons[0]["refs"] == [{"asvs_id": "v5.0.0-8.2.2"}, {"asvs_id": "v5.0.0-6.2.1"}]
    many = {**FLAGS, "unverified": [{"finding_id": f"{i:064x}"} for i in range(30)]}
    _, _, reasons = _decide(config, flags=many, results=results)
    assert reasons[0]["refs"][0] == {"finding_id": f"{0:064x}"} and reasons[0]["summary"].endswith("(showing 20 of 32)")
    assert len(reasons[0]["refs"]) == 20 and reasons[0]["total"] == 32


def test_every_reason_carries_the_deduplicated_ref_count_before_the_cap(config):
    flags = {**FLAGS, "unverified": [{"asvs_id": "v5.0.0-8.2.2"}, {"asvs_id": "v5.0.0-8.2.2"}]}
    _, _, reasons = _decide(config, flags=flags, results=[_result("v5.0.0-8.2.2", "needs_human", "downgraded")])
    assert [(r["refs"], r["total"]) for r in reasons] == [([{"asvs_id": "v5.0.0-8.2.2"}], 1)]


def test_answers_outside_the_agents_set_are_suspicious(config):
    level, _, reasons = _decide(config, flags={**FLAGS, "ignored": ["v5.0.0-1.1.1"]})
    assert level == "elevated" and _triggers(reasons) == ["T-12"]
    assert reasons[0]["refs"] == [{"asvs_id": "v5.0.0-1.1.1"}]
    assert "not asked about" in reasons[0]["summary"]


def test_unknown_tier_is_an_input_error(config):
    with pytest.raises(InputError):
        _decide(config, intake={"tier": "critical", "data_classification": "internal"})


@pytest.mark.parametrize("size, fires", [
    ({"lines": 1000, "files": 40}, False),
    ({"lines": 1001, "files": 1}, True),
    ({"lines": 0, "files": 41}, True),
    ({"lines": 999, "files": 39}, False),
])
def test_t19_fires_only_above_the_large_change_limits(config, size, fires):
    level, _, reasons = _decide(config, change_size=size)
    assert _triggers(reasons) == (["T-19"] if fires else [])
    if fires:
        assert level == "elevated"
        lines, files = size["lines"], size["files"]
        assert reasons[0]["summary"] == f"Large change: {lines} line{'s' * (lines != 1)} in {files} file{'s' * (files != 1)}"
        assert reasons[0]["refs"] == [{"source": "the base and head diff"}]


def test_no_change_size_never_fires_t19(config):
    assert _decide(config, change_size=None) == ("standard", "standard", [])


def test_the_re_review_triggers_take_their_level_from_the_config(config):
    config["triggers"].update({"T-17": "security_review", "T-19": "security_review"})
    finding = _finding([{"framework": "golden-road-trigger", "id": "T-17"}])
    level, _, reasons = _decide(config, findings=[finding], change_size={"lines": 5000, "files": 1})
    assert level == "security_review"
    assert [(r["trigger"], r["level"]) for r in reasons] == [("T-17", "security_review"), ("T-19", "security_review")]


def test_every_configurable_trigger_has_a_summary(config):
    assert set(SUMMARIES) == set(config["triggers"])


@pytest.mark.parametrize("size, text", [
    ({"lines": 1, "files": 41}, "Large change: 1 line in 41 files"),
    ({"lines": 1001, "files": 1}, "Large change: 1001 lines in 1 file"),
    ({"lines": 5000, "files": 50}, "Large change: 5000 lines in 50 files"),
])
def test_t19_summary_pluralizes(config, size, text):
    _, _, reasons = _decide(config, change_size=size)
    assert reasons[0]["summary"] == text


REGISTRATION = {"tier": "moderate", "data_classification": "internal", "rules": ["write_roles", "other_egress"]}


@pytest.mark.parametrize("intake, fires", [
    ({"tier": "low", "data_classification": "internal"}, True),
    ({"tier": "moderate", "data_classification": "public"}, True),
    ({"tier": "moderate", "data_classification": "internal"}, False),
    ({"tier": "high", "data_classification": "phi"}, False),
])
def test_t21_fires_when_the_registration_is_below_the_floor(config, intake, fires):
    _, _, reasons = _decide(config, intake=intake, registration=REGISTRATION)
    assert [t for t in _triggers(reasons) if t == "T-21"] == (["T-21"] if fires else [])


def test_t21_names_the_registered_and_needed_values_and_the_rules_that_fired(config):
    level, _, reasons = _decide(config, intake={"tier": "low", "data_classification": "internal"}, registration=REGISTRATION)
    [reason] = [r for r in reasons if r["trigger"] == "T-21"]
    assert reason["summary"] == "Registered as low/internal; this change needs at least moderate/internal (signals: write_roles, other_egress)"
    assert reason["refs"] == [{"source": "write_roles"}, {"source": "other_egress"}] and reason["total"] == 2
    assert reason["level"] == "security_review" and level == "security_review"


def test_t21_without_rules_names_only_the_values(config):
    _, _, reasons = _decide(config, intake={"tier": "low", "data_classification": "public"},
                            registration={"tier": "low", "data_classification": "internal", "rules": []})
    [reason] = reasons
    assert reason["summary"] == "Registered as low/public; this change needs at least low/internal" and reason["refs"]


def test_t21_takes_its_level_from_the_config(config):
    config["triggers"]["T-21"] = "elevated"
    level, _, reasons = _decide(config, intake={"tier": "low", "data_classification": "internal"}, registration=REGISTRATION)
    assert [(r["trigger"], r["level"]) for r in reasons] == [("T-21", "elevated")] and level == "elevated"


def test_t21_does_not_fire_without_an_intake_record_or_a_floor(config):
    _, _, reasons = _decide(config, intake=None, registration=REGISTRATION)
    assert _triggers(reasons) == ["T-8"]
    assert _decide(config, registration=None) == ("standard", "standard", [])
