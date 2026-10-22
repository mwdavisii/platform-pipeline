"""The golden set against a real gateway. Opt-in: it costs tokens and needs a credential. Never part of the normal run.

    GOLDEN_ROAD_SCORECARD_LIVE=1 GOLDEN_ROAD_SCORECARD_FACTORY=<factory dir> SCORECARD_GATEWAY_TOKEN=<token> PYTHONPATH=src \\
        pytest -s tests/scorecard/test_scorecard_golden_live.py

Settings:
- Environment: GOLDEN_ROAD_SCORECARD_LIVE=1 (the opt-in; anything else skips the test with no network call),
  GOLDEN_ROAD_SCORECARD_FACTORY (a factory directory with config/scorecard.yaml; required), the gateway credential in the
  variable the config names (`agent.gateway.credential_env`, SCORECARD_GATEWAY_TOKEN in the sample), and optionally
  GOLDEN_ROAD_SCORECARD_REPORT (a file to write the report JSON to).
- Config (agent block of that scorecard.yaml): gateway.url and gateway.auth, model name and version, prompt_version, budget
  and batching. Nothing about the gateway is read from anywhere else.

Only the `agent` block is used: the cases run against the fixture's Gate 1 checks, patterns and thresholds.

The report (false positives and false negatives per trigger and per chapter, and each case's verdict) is printed and,
with GOLDEN_ROAD_SCORECARD_REPORT, written to a file. It has two views: the top level is absolute (every trigger and fail on
every case, so the model's noise on clean PRs counts); `baseline_relative` counts only what differs from each case's baseline
twin (default clean-python) in the same run, and is the one to read for accuracy. A case that raises is reported as an error. Accuracy is reported, not asserted: thresholds aren't calibrated
yet (O6). Registry facts come from the cases, so only the model is measured.
"""

import json
import os
from pathlib import Path

import pytest

from golden_road.io import write_json
from scorecard.agent.gateway import GatewayClient, credential
from scorecard.config import load_config
from scorecard.golden import load_cases, report, run_cases

GOLDEN = Path(__file__).parent / "golden"

pytestmark = pytest.mark.skipif(os.environ.get("GOLDEN_ROAD_SCORECARD_LIVE") != "1",
                                reason="live golden eval: set GOLDEN_ROAD_SCORECARD_LIVE=1 (see the module docstring)")


# The cases always run against the fixture's deterministic config (Gate 1 checks, patterns, thresholds); only the `agent` block
# of the live factory is used, so a real factory can't change what a case means.
FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "factory" / "config" / "scorecard.yaml"


def test_live_golden_set(tmp_path):
    factory = os.environ.get("GOLDEN_ROAD_SCORECARD_FACTORY")
    if not factory:
        pytest.fail("set GOLDEN_ROAD_SCORECARD_FACTORY to a factory directory with config/scorecard.yaml")
    live_config = Path(factory) / "config" / "scorecard.yaml"
    config = load_config(factory)
    token = credential(config)
    apps, cases = load_cases(GOLDEN)
    # A case that raises is recorded in the report (an `error` verdict); it never stops the others.
    results = run_cases(cases, apps, config_path=FIXTURE_CONFIG, agent_config_path=live_config, workdir=tmp_path,
                        model_for=lambda case, root: GatewayClient(config, token))
    rep = report(cases, results)
    rep["model"], rep["prompt_version"] = config["agent"]["model"], config["agent"]["prompt_version"]
    rep["tokens_used"] = sum(r.stats.get("tokens_used", 0) for r in results)
    if os.environ.get("GOLDEN_ROAD_SCORECARD_REPORT"):
        write_json(Path(os.environ["GOLDEN_ROAD_SCORECARD_REPORT"]), rep)
    print(json.dumps(rep, indent=2))
    assert len(rep["cases"]) == len(cases)
