"""The routing review: one conversation, the questions from routing.toml, a harness-recorded tool log, and a checked submission."""

import json

import pytest

from contracts.schema import validate
from scorecard.agent.fake import FactsModel, tool_call
from scorecard.agent.gateway import Completion
from scorecard.agent.review2 import SUBMIT, LoggedTools, assess_facts, parse_facts
from scorecard.agent.output import InvalidOutput
from scorecard.agent.tools import RepoTools
from scorecard.build import prepare
from scorecard.routing.config import agent_facts, questions_sha256


@pytest.fixture
def prepared(pr_checkout, routing_factory):
    return prepare(**pr_checkout(), factory=routing_factory)


def test_one_conversation_answers_every_question(prepared):
    model = FactsModel(answers={"personal_data": {"answer": "unsure", "justification": "can't tell"}})
    doc, stats = assess_facts(prepared, model)
    assert validate("facts-assessment.v1", doc) == []
    names = [n for n, _ in agent_facts(prepared.config["routing"])]
    assert [a["fact"] for a in doc["answers"]] == names and doc["complete"] is True
    assert doc["questions_sha256"] == questions_sha256(prepared.config["routing"])
    assert {e["text"] for e in doc["tool_log"] if e["tool"] == "grep"} >= {"token", "jwt", "mrn"}
    assert stats["questions"] == len(names) and stats["complete"] is True
    assert {m[0]["content"] for m in model.sent} == {model.sent[0][0]["content"]}  # one system prompt, the same every turn
    user = model.sent[0][1]["content"]
    assert "own_credentials (the whole application)" in user and "mutates_on_read_route (this change only)" in user
    assert "Before answering no, grep the whole repository (glob *) for each of: token, api_key" in user
    assert "Notes" not in user  # the PR title never reaches the model


def test_a_review_that_fails_is_incomplete_with_no_answers(prepared):
    class Mute:
        def complete(self, messages, tools, *, max_tokens, tool_choice=None):
            return Completion(message={"role": "assistant", "content": "I won't."}, tokens=10, truncated=False)

    doc, stats = assess_facts(prepared, Mute())
    assert doc["complete"] is False and doc["answers"] == [] and stats["failure"].startswith("invalid output")


def test_the_submission_must_answer_every_question_once():
    names = ["a_fact", "b_fact"]
    good = {"answers": [{"fact": n, "answer": "no", "justification": "", "evidence": []} for n in names]}
    assert parse_facts(json.dumps(good), names) == good["answers"]
    for bad, kind in ((good | {"answers": good["answers"][:1]}, "coverage"),
                      (good | {"answers": good["answers"] * 2}, "coverage"),
                      ({"answers": [{"fact": "a_fact", "answer": "maybe", "justification": "", "evidence": []}]}, "schema")):
        with pytest.raises(InvalidOutput) as err:
            parse_facts(json.dumps(bad), names)
        assert err.value.kind == kind
    with pytest.raises(InvalidOutput):
        parse_facts("not json", names)


def test_the_tool_log_records_only_what_ran(tmp_path):
    (tmp_path / "app.py").write_text("token = 1\n")
    logged = LoggedTools(RepoTools(tmp_path, {"agent": {"tools": {"max_read_lines": 400, "max_output_chars": 24000, "max_grep_matches": 100,
                                                                  "max_list_files": 500, "max_diff_chars": 60000}},
                                                   "patterns": {"injection": []}}))
    logged.call("grep", json.dumps({"text": "token"}))
    logged.call("grep", json.dumps({"text": ""}))
    logged.call("read_file", json.dumps({"path": "app.py"}))
    logged.call("nope", "{}")
    assert logged.log == [{"tool": "grep", "ok": True, "text": "token"}, {"tool": "grep", "ok": False, "text": ""},
                          {"tool": "read_file", "ok": True, "path": "app.py"}]
    assert "submit_facts" in logged.call("nope", "{}")
