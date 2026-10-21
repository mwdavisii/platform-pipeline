import datetime as dt
import json

import pytest

from contracts.schema import validate
from golden_road.gate import evaluate, load_overrides
from golden_road.io import InputError

TODAY = dt.date(2026, 10, 20)


def finding(valid_finding, **changes):
    return {**valid_finding, **changes}


def override(fid, **changes):
    base = {"finding_id": fid, "repo": "example-org/hello-app", "approver": "infosec",
            "justification": "Compensating control documented in ticket SEC-1.",
            "created": "2026-10-19", "expires": "2026-11-18"}
    return {**base, **changes}


def test_pass_with_no_blocking(valid_finding):
    result = evaluate([finding(valid_finding, severity="medium")], [], repo="example-org/hello-app", today=TODAY)
    assert validate("gate-result.v1", result) == []
    assert result["status"] == "pass"
    assert result["counts"]["by_severity"]["medium"] == 1
    assert result["counts"]["by_layer"] == {"deterministic": 1}


@pytest.mark.parametrize("severity", ["high", "critical"])
def test_high_and_critical_block(valid_finding, severity):
    f = finding(valid_finding, severity=severity)
    result = evaluate([f], [], repo="example-org/hello-app", today=TODAY)
    assert result["status"] == "fail" and result["blocking"] == [f["id"]]


def test_valid_override_unblocks(valid_finding):
    result = evaluate([valid_finding], [override(valid_finding["id"])], repo="example-org/hello-app", today=TODAY)
    assert result["status"] == "pass" and result["overrides_applied"] == [valid_finding["id"]]


@pytest.mark.parametrize("changes", [
    {"repo": "example-org/other"},
    {"expires": "2026-10-19"},
    {"created": "2026-10-21"},
    {"created": "2026-01-01", "expires": "2026-12-31"},
    {"expires": "2026-10-18"},
])
def test_override_that_does_not_apply(valid_finding, changes):
    result = evaluate([valid_finding], [override(valid_finding["id"], **changes)], repo="example-org/hello-app", today=TODAY)
    assert result["status"] == "fail"


def test_override_of_exactly_90_days_applies_and_91_does_not(valid_finding):
    ok = override(valid_finding["id"], created="2026-10-19", expires="2027-01-17")
    too_long = override(valid_finding["id"], created="2026-10-19", expires="2027-01-18")
    assert evaluate([valid_finding], [ok], repo="example-org/hello-app", today=TODAY)["status"] == "pass"
    assert evaluate([valid_finding], [too_long], repo="example-org/hello-app", today=TODAY)["status"] == "fail"


def test_unknown_layer_fails_closed_without_crashing(valid_finding):
    bad = {**valid_finding, "source": {**valid_finding["source"], "layer": "mystery"}}
    with pytest.raises(InputError):
        evaluate([bad], [], repo="example-org/hello-app", today=TODAY)


def test_load_overrides(tmp_path, valid_finding):
    (tmp_path / "a.json").write_text(json.dumps(override(valid_finding["id"])))
    assert len(load_overrides(tmp_path)) == 1
    (tmp_path / "b.json").write_text(json.dumps({"bad": True}))
    with pytest.raises(InputError):
        load_overrides(tmp_path)


def test_missing_overrides_dir_is_empty(tmp_path):
    assert load_overrides(tmp_path / "none") == []
