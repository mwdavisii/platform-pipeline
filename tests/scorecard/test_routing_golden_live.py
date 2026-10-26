"""The routing golden cases against a real gateway. Opt-in: it costs tokens and needs a credential. Never part of the normal run.

    GOLDEN_ROAD_SCORECARD_LIVE=1 GOLDEN_ROAD_SCORECARD_FACTORY=<routing factory dir> SCORECARD_GATEWAY_TOKEN=<token> SCORECARD_GATEWAY_MODEL=<model> \\
        PYTHONPATH=src pytest -s tests/scorecard/test_routing_golden_live.py

Settings are those of test_scorecard_golden_live.py; the report goes to GOLDEN_ROAD_SCORECARD_REPORT with a `routing-` prefix on its
file name. A live run reports what the model said, its tokens, and true and false positives per fact; it asserts nothing about answers.
"""

import json
import os
from pathlib import Path

import pytest

from golden_road.io import write_json
from scorecard.agent.gateway import GatewayClient, credential, gateway_model
from scorecard.config import load_config
from scorecard.routing.golden import load_cases, report, run_case

CASES = Path(__file__).parent / "golden_routing" / "cases.yaml"

pytestmark = pytest.mark.skipif(os.environ.get("GOLDEN_ROAD_SCORECARD_LIVE") != "1",
                                reason="live golden eval: set GOLDEN_ROAD_SCORECARD_LIVE=1 (see the module docstring)")


def test_live_routing_golden_set(tmp_path):
    factory = os.environ.get("GOLDEN_ROAD_SCORECARD_FACTORY")
    if not factory:
        pytest.fail("set GOLDEN_ROAD_SCORECARD_FACTORY to a routing factory directory with config/routing.toml")
    config = load_config(factory)
    client = GatewayClient(config, credential(config), model=gateway_model())
    cases, results = load_cases(CASES), []
    for case in cases:
        try:
            results.append(run_case(case, Path(factory), client, tmp_path))
        except Exception as exc:  # a case that raises is reported, and never stops the others
            results.append({"name": case["name"], "answers": {}, "level": None, "fired": [], "shadow": [], "tokens": 0, "tool_calls": 0,
                            "problems": [f"error: {type(exc).__name__}: {exc}"]})
    rep = report(cases, results)
    rep["model"], rep["prompt_version"] = config["agent"]["model"], config["agent"]["prompt_version"]
    rep["tokens_used"] = sum(r["tokens"] for r in results)
    target = os.environ.get("GOLDEN_ROAD_SCORECARD_REPORT")
    if target:
        path = Path(target)
        write_json(path.with_name("routing-" + path.name), rep)
    print(json.dumps(rep, indent=2))
    assert len(rep["cases"]) == len(cases)
