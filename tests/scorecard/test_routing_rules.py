"""Rules over facts: enforced rules raise the level, shadow rules are recorded, unsure answers follow on_unsure."""

from scorecard.routing.rules import evaluate


def _bool(value, refs=None):
    return {"kind": "builtin", "type": "bool", "value": value, "refs": refs if refs is not None else ([{"source": "x"}] if value else []), "params": {}}


def _agent(value, why=None, refs=None):
    return {"kind": "agent", "type": "answer", "value": value, "why": why,
            "refs": refs if refs is not None else ([{"file": "app.py", "line": 3}] if value else [{"source": f"q: {why}"}] if why else [])}


def _spec(kind, base, head, **extra):
    return {"kind": "spec", "type": kind, "value": head, "base": base, "head": head, "refs": [{"file": "builder-spec.json"}], **extra}


def _rule(id, when, level="security_review", **extra):
    return {"id": id, "when": when, "level": level, "summary": f"summary {id}", "reason": f"reason {id}", "guidance": f"guidance {id}", **extra}


def test_nothing_fires_and_the_floor_holds():
    level, reasons, shadow, undecided = evaluate({"rules": [_rule("T-2", {"fact": "a", "is": True})]}, {"a": _bool(False)},
                                                 classification=None, floor="standard")
    assert (level, reasons, shadow, undecided) == ("standard", [], [], [])


def test_an_enforced_rule_raises_and_a_shadow_rule_only_records():
    routing = {"rules": [_rule("T-22", {"fact": "a", "is": True}, mode="shadow"), _rule("T-9", {"fact": "a", "is": True}, level="elevated")]}
    level, reasons, shadow, _ = evaluate(routing, {"a": _bool(True)}, classification=None, floor="standard")
    assert level == "elevated"
    assert reasons == [{"trigger": "T-9", "level": "elevated", "summary": "summary T-9", "refs": [{"source": "x"}], "total": 1,
                        "reason": "reason T-9", "guidance": "guidance T-9", "unsure": False}]
    assert [s["trigger"] for s in shadow] == ["T-22"]


def test_the_floor_is_never_lowered():
    level, *_ = evaluate({"rules": [_rule("T-9", {"fact": "a", "is": True}, level="elevated")]}, {"a": _bool(True)},
                         classification=None, floor="security_review")
    assert level == "security_review"


def test_any_condition_in_a_list_fires_and_refs_join():
    routing = {"rules": [_rule("T-3", [{"fact": "a", "is": True}, {"fact": "b", "is": True}], on_unsure="elevated")]}
    _, reasons, _, _ = evaluate(routing, {"a": _bool(True, [{"source": "x"}]), "b": _agent(True)}, classification=None, floor="standard")
    assert reasons[0]["refs"] == [{"source": "x"}, {"file": "app.py", "line": 3}] and reasons[0]["summary"] == "summary T-3 (2 items)"


def test_a_yes_beats_an_unsure_in_the_same_rule():
    routing = {"rules": [_rule("T-3", [{"fact": "a", "is": True}, {"fact": "b", "is": True}], on_unsure="elevated")]}
    _, reasons, _, _ = evaluate(routing, {"a": _bool(True), "b": _agent(None, "not answered")}, classification=None, floor="standard")
    assert reasons[0]["level"] == "security_review" and reasons[0]["unsure"] is False


def test_unsure_follows_on_unsure():
    facts = {"q": _agent(None, "token was not searched")}
    for how, want in (("elevated", "elevated"), ("rule_level", "security_review")):
        level, reasons, _, undecided = evaluate({"rules": [_rule("T-22", {"fact": "q", "is": True}, on_unsure=how)]}, facts,
                                                classification=None, floor="standard")
        assert level == want and undecided == []
        assert reasons[0]["unsure"] is True and reasons[0]["summary"] == "Not determined: summary T-22"
        assert reasons[0]["undetermined"] == ["q: token was not searched"]
    level, reasons, _, undecided = evaluate({"rules": [_rule("T-26", {"fact": "q", "is": True}, on_unsure="standard")]}, facts,
                                            classification=None, floor="standard")
    assert (level, reasons) == ("standard", []) and undecided == [{"trigger": "T-26", "mode": "enforce", "undetermined": ["q: token was not searched"]}]


def test_a_no_answer_does_not_fire():
    _, reasons, _, _ = evaluate({"rules": [_rule("T-22", {"fact": "q", "is": True}, on_unsure="elevated")]}, {"q": _agent(False)},
                                classification=None, floor="standard")
    assert reasons == []


def test_escalate_for_sensitive_classifications():
    routing = {"rules": [_rule("T-10", {"fact": "q", "is": True}, level="elevated", on_unsure="standard",
                               escalate={"classifications": ["restricted", "phi"], "level": "security_review"})]}
    assert evaluate(routing, {"q": _agent(True)}, classification="phi", floor="standard")[0] == "security_review"
    assert evaluate(routing, {"q": _agent(True)}, classification="internal", floor="standard")[0] == "elevated"


def test_spec_conditions():
    facts = {"cls": _spec("ordered", "internal", "phi", order=["public", "internal", "restricted", "phi"]),
             "roles": _spec("number", 2, 3), "hosts": _spec("list", [], ["api.example.com"], added=["api.example.com"]),
             "grants": _spec("grants", [], [{"justification": "search uses POST bodies"}], added=[{"justification": "search uses POST bodies"}]),
             "none": _spec("ordered", None, None, order=["default", "medium"])}
    def fired(cond):
        return bool(evaluate({"rules": [_rule("T-30", cond)]}, facts, classification=None, floor="standard")[1])
    assert fired({"fact": "cls", "changed": "up"}) and fired({"fact": "cls", "is": "phi"}) and fired({"fact": "cls", "any": ["restricted", "phi"]})
    assert fired({"fact": "roles", "over": 2}) and fired({"fact": "roles", "at_least": 3}) and not fired({"fact": "roles", "over": 3})
    assert fired({"fact": "hosts", "changed": "added"}) and fired({"fact": "hosts", "any": ["api.example.com"]})
    assert fired({"fact": "grants", "changed": "added"}) and not fired({"fact": "none", "changed": "up"})
    assert not fired({"fact": "none", "is": "medium"})


def test_placeholders_are_filled():
    rule = _rule("T-24", {"fact": "grants", "changed": "added"}, level="elevated")
    rule["reason"] = "Justification: {justification}"
    facts = {"grants": _spec("grants", [], [{"justification": "search uses POST bodies"}], added=[{"justification": "search uses POST bodies"}])}
    _, reasons, _, _ = evaluate({"rules": [rule]}, facts, classification=None, floor="standard")
    assert reasons[0]["reason"] == "Justification: search uses POST bodies"
    big = _rule("T-19", {"fact": "big", "is": True}, level="elevated", summary="Large change: {lines} lines in {files} files")
    facts = {"big": {**_bool(True), "params": {"lines": 1200, "files": 3}}}
    assert evaluate({"rules": [big]}, facts, classification=None, floor="standard")[1][0]["summary"] == "Large change: 1200 lines in 3 files"


def test_refs_are_capped():
    refs = [{"source": f"s{i}"} for i in range(25)]
    _, reasons, _, _ = evaluate({"rules": [_rule("T-14", {"fact": "a", "is": True}, level="elevated")]}, {"a": _bool(True, refs)},
                                classification=None, floor="standard")
    assert len(reasons[0]["refs"]) == 20 and reasons[0]["total"] == 25 and reasons[0]["summary"].endswith("(showing 20 of 25)")


def test_reasons_are_sorted_by_number():
    routing = {"rules": [_rule("T-21", {"fact": "a", "is": True}), _rule("T-3", {"fact": "a", "is": True})]}
    _, reasons, _, _ = evaluate(routing, {"a": _bool(True)}, classification=None, floor="standard")
    assert [r["trigger"] for r in reasons] == ["T-3", "T-21"]


def test_long_placeholder_values_are_cut_and_the_event_still_validates():
    from scorecard.event import re_review_event
    from scorecard.routing.rules import MAX_SUMMARY, MAX_TEXT

    rule = _rule("T-24", {"fact": "g", "changed": "added"}, level="elevated")
    rule.update(summary="Granted: {justification}", reason="Because: {justification}", guidance="Fix: {justification}")
    grants = [{"justification": "x" * 5000}, {"justification": "y"}]
    fact = _spec("grants", [], grants, added=grants)
    fact["refs"] = [{"file": "builder-spec.json"}, {"file": "builder-spec.json", "line": 3}]
    level, [entry], *_ = evaluate({"rules": [rule]}, {"g": fact}, classification=None, floor="standard")
    assert entry["summary"].startswith("Granted: xxx") and entry["summary"].endswith("… (2 items)")
    assert len(entry["summary"]) == MAX_SUMMARY + len(" (2 items)")
    assert len(entry["reason"]) == len(entry["guidance"]) == MAX_TEXT and entry["reason"].endswith("…")
    card = {"generated_at": "2026-10-23T08:00:00Z", "level": level, "floor": "standard", "app": {"repo": "acme/notes", "tree_sha": "c" * 40},
            "assessment": {"complete": True}, "reasons": [entry]}
    assert re_review_event(card, pr=1, head_sha="a" * 40, recorded=[])["signals"][0]["id"] == "T-24"


def test_short_values_are_not_cut():
    rule = _rule("T-9", {"fact": "a", "is": True}, level="elevated")
    _, [entry], *_ = evaluate({"rules": [rule]}, {"a": _bool(True)}, classification=None, floor="standard")
    assert entry["summary"] == "summary T-9" and entry["reason"] == "reason T-9"


def test_an_undecided_rule_records_its_mode():
    facts = {"q": _agent(None, why="unsure")}
    for mode in ("enforce", "shadow"):
        _, _, _, undecided = evaluate({"rules": [_rule("T-26", {"fact": "q", "is": True}, on_unsure="standard", mode=mode)]}, facts,
                                      classification=None, floor="standard")
        assert undecided[0]["mode"] == mode
