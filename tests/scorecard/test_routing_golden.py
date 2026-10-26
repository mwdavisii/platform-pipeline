"""The routing golden cases in fake mode: a fixed fake gives a fixed scorecard, so every expectation must hold exactly."""

from pathlib import Path

import pytest

from scorecard.routing.golden import fake_client, load_cases, report, run_case

CASES = load_cases(Path(__file__).parent / "golden_routing" / "cases.yaml")


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_case(case, routing_factory, tmp_path):
    result = run_case(case, routing_factory, fake_client(case), tmp_path)
    assert result["problems"] == []


def test_the_set_covers_every_agent_fact_both_ways(routing_config):
    from scorecard.routing.config import agent_facts
    names = {n for n, _ in agent_facts(routing_config["routing"])}
    wanted = {(f, a) for c in CASES for f, a in c["expect"].get("answers", {}).items()}
    for fact in ("own_credentials", "mutates_on_read_route", "personal_data"):
        assert (fact, "yes") in wanted and (fact, "no") in wanted, fact
    assert {f for f, _ in wanted} <= names


def test_the_false_alarm_set_is_standard():
    quiet = [c for c in CASES if c["expect"].get("level") == "standard" and not c["expect"].get("fired")]
    assert len(quiet) >= 5
