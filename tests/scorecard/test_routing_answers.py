"""Code's check of the agent's answers: yes needs verified citations (into a changed file for a change question), no needs
the required searches in the tool log, and anything else is unsure."""

import pytest

from golden_road.io import InputError
from scorecard.routing.answers import check_answers, check_facts_assessment
from scorecard.routing.config import questions_sha256

TREE = "a" * 40
APP = "from fastapi import FastAPI\n\napp = FastAPI()\nAPI_KEYS = {}\n"
ROUTING = {"facts": {
    "own_credentials": {"from": "agent", "applies_to": "tree", "question": "Does it check its own keys?", "must_check": ["api_key", "token"]},
    "mutates_on_read_route": {"from": "agent", "applies_to": "change", "question": "Does a read route change data?"},
    "workflow_change": {"from": "builtin", "measure": "workflow_change"}}}
CITE = {"file": "app.py", "start_line": 4, "snippet": "API_KEYS = {}"}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "app.py").write_text(APP)
    return tmp_path


def _doc(answers, log=(), complete=True):
    return {"schema": "facts-assessment/v1", "tree_sha": TREE, "prompt_version": "review-2", "model": {"version": "pinned-1", "temperature": 0},
            "questions_sha256": questions_sha256(ROUTING), "complete": complete, "answers": list(answers), "tool_log": list(log)}


def _answer(fact, answer, evidence=(), justification="because"):
    return {"fact": fact, "answer": answer, "justification": justification, "evidence": list(evidence)}


def _grep(text, glob="*", ok=True):
    return {"tool": "grep", "ok": ok, "text": text, "glob": glob}


def _check(repo, doc, changed=("app.py",)):
    return check_answers(doc, ROUTING, repo_root=repo, citable={"app.py"}, changed=set(changed))


def test_no_assessment_makes_every_answer_unsure(repo):
    facts = _check(repo, None)
    assert set(facts) == {"own_credentials", "mutates_on_read_route"}
    assert facts["own_credentials"]["value"] is None and facts["own_credentials"]["why"] == "no assessment"
    assert facts["own_credentials"]["refs"] == [{"source": "own_credentials: no assessment"}]


def test_a_verified_yes_is_accepted(repo):
    fact = _check(repo, _doc([_answer("own_credentials", "yes", [CITE])]))["own_credentials"]
    assert fact["value"] is True and fact["answer"] == "yes" and fact["refs"] == [{"file": "app.py", "line": 4}]


def test_a_yes_with_a_bad_citation_is_unsure(repo):
    bad = {**CITE, "snippet": "API_KEYS = load()"}
    fact = _check(repo, _doc([_answer("own_credentials", "yes", [bad])]))["own_credentials"]
    assert fact["value"] is None and fact["why"] == "the evidence could not be confirmed"


def test_a_yes_with_no_evidence_is_unsure(repo):
    assert _check(repo, _doc([_answer("own_credentials", "yes")]))["own_credentials"]["why"] == "the evidence could not be confirmed"


def test_a_change_yes_must_cite_a_changed_file(repo):
    doc = _doc([_answer("mutates_on_read_route", "yes", [CITE])])
    assert _check(repo, doc)["mutates_on_read_route"]["value"] is True
    fact = _check(repo, doc, changed=("other.py",))["mutates_on_read_route"]
    assert fact["value"] is None and fact["why"] == "the evidence cites no file this pull request changed"


def test_a_no_needs_every_required_search(repo):
    doc = _doc([_answer("own_credentials", "no")], log=[_grep("API_KEY"), _grep("token", glob="*.py")])
    fact = _check(repo, doc)["own_credentials"]
    assert fact["value"] is None and fact["why"] == "token was not searched" and fact["searched"] == ["api_key"]
    doc = _doc([_answer("own_credentials", "no")], log=[_grep("api_key"), _grep("Token")])
    fact = _check(repo, doc)["own_credentials"]
    assert fact["value"] is False and fact["searched"] == ["api_key", "token"] and fact["refs"] == []


def test_a_failed_search_does_not_count(repo):
    doc = _doc([_answer("own_credentials", "no")], log=[_grep("api_key"), _grep("token", ok=False)])
    assert _check(repo, doc)["own_credentials"]["why"] == "token was not searched"


def test_a_no_without_required_searches_is_accepted(repo):
    assert _check(repo, _doc([_answer("mutates_on_read_route", "no")]))["mutates_on_read_route"]["value"] is False


def test_unsure_and_missing_answers(repo):
    facts = _check(repo, _doc([_answer("own_credentials", "unsure")]))
    assert facts["own_credentials"]["why"] == "the reviewer was unsure"
    assert facts["mutates_on_read_route"]["why"] == "not answered"
    assert _check(repo, _doc([], complete=False))["own_credentials"]["why"] == "the review did not finish"


def test_unverified_snippets_and_justifications_are_redacted(repo):
    secret = "ghp_" + "A" * 36
    fact = _check(repo, _doc([_answer("own_credentials", "yes", [{**CITE, "snippet": secret}], justification=f"key {secret}")]))["own_credentials"]
    assert secret not in str(fact["evidence"]) and secret not in fact["justification"]


CONFIG = {"routing": ROUTING, "agent": {"prompt_version": "review-2", "model": {"version": "pinned-1", "temperature": 0}}}


def test_the_assessment_itself_is_checked():
    check_facts_assessment(_doc([]), tree_sha=TREE, config=CONFIG)
    with pytest.raises(InputError, match="for tree"):
        check_facts_assessment(_doc([]), tree_sha="b" * 40, config=CONFIG)
    with pytest.raises(InputError, match="other questions"):
        check_facts_assessment({**_doc([]), "questions_sha256": "0" * 64}, tree_sha=TREE, config=CONFIG)
    with pytest.raises(InputError, match="prompt"):
        check_facts_assessment({**_doc([]), "prompt_version": "review-1"}, tree_sha=TREE, config=CONFIG)
    with pytest.raises(InputError, match="more than once"):
        check_facts_assessment(_doc([_answer("own_credentials", "no"), _answer("own_credentials", "yes")]), tree_sha=TREE, config=CONFIG)
    with pytest.raises(InputError, match="not a valid facts-assessment/v1"):
        check_facts_assessment({**_doc([]), "complete": "yes"}, tree_sha=TREE, config=CONFIG)
