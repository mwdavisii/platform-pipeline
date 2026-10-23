"""Central review: the optional config section, and the poller's pure decisions."""
import shutil
from pathlib import Path

import pytest
import yaml

from golden_road.io import InputError
from scorecard.config import load_config

FACTORY = Path(__file__).resolve().parents[2] / "bootstrap/scorecard-factory"


def factory_with(tmp_path, central):
    shutil.copytree(FACTORY / "config", tmp_path / "config")
    doc = yaml.safe_load((tmp_path / "config/scorecard.yaml").read_text())
    if central is not None:
        doc["central"] = central
    (tmp_path / "config/scorecard.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return tmp_path


def test_the_committed_factory_has_no_central_section():
    assert "central" not in load_config(FACTORY)


def test_central_settings_load_when_valid(tmp_path):
    assert load_config(factory_with(tmp_path, {"max_in_flight": 4, "assess_timeout_minutes": 360}))["central"] == {"max_in_flight": 4, "assess_timeout_minutes": 360}


@pytest.mark.parametrize("central", [{"max_in_flight": 0, "assess_timeout_minutes": 360}, {"max_in_flight": 21, "assess_timeout_minutes": 360},
                                     {"max_in_flight": 4, "assess_timeout_minutes": 339}, {"max_in_flight": "4", "assess_timeout_minutes": 360},
                                     {"max_in_flight": 4, "assess_timeout_minutes": 360, "poll_minutes": 5}, {"max_in_flight": 4}])
def test_bad_central_settings_are_refused(tmp_path, central):
    with pytest.raises(InputError):
        load_config(factory_with(tmp_path, central))


def test_the_smallest_timeout_is_340(tmp_path):
    assert load_config(factory_with(tmp_path, {"max_in_flight": 4, "assess_timeout_minutes": 340}))["central"]["assess_timeout_minutes"] == 340


# ---- the poller's pure decisions ----
from golden_road import remediate  # noqa: E402
from scorecard.central import ASSESSED, LEVEL_PREFIX, MAX_ATTEMPTS, decide, fingerprint, gate1_state  # noqa: E402

APP, REPO, HEAD, NOW = 424242, "o/app", "a" * 40, "2026-10-23T12:00:00Z"


def pr(**kw):
    d = dict(number=7, draft=False, title="t", body="b", updated_at="2026-10-23T11:00:00Z",
             head={"sha": HEAD, "repo": {"full_name": REPO}}, base={"ref": "main", "repo": {"full_name": REPO}})
    d.update(kw)
    return d


def check(kind="queued", fp=None, attempt=1, status="completed", started="2026-10-23T11:30:00Z", **kw):
    return {"id": kw.get("id", 10), "name": kw.get("name", ASSESSED), "app": {"id": kw.get("app", APP)}, "head_sha": kw.get("head", HEAD),
            "status": status, "started_at": started, "external_id": f"v1:{kind}:{fp or fingerprint(pr())}:{attempt}"}


GREEN, PENDING = (lambda: ("green", [])), (lambda: ("pending", []))
RED = lambda: ("red", ["golden-road / drift is failure"])  # noqa: E731


def go(p=None, runs=(), gate=GREEN):
    return decide(p or pr(), REPO, list(runs), gate, app_id=APP, now=NOW, timeout_minutes=360)


def test_a_draft_is_skipped_and_never_marked():
    assert go(pr(draft=True)).kind == "skip"


def test_a_fork_pr_is_marked_once_with_the_maintainer_text():
    fork = pr(head={"sha": HEAD, "repo": {"full_name": "x/fork"}})
    a = go(fork)
    assert a.kind == "mark" and "forks are reviewed by a maintainer" in a.reason and a.external_id.startswith("v1:fork:")
    assert go(fork, [check("fork", fp=fingerprint(fork))]).kind == "skip"
    assert go(pr(head={"sha": HEAD, "repo": None})).kind == "mark"  # a deleted fork


def test_gate1_pending_waits_for_the_next_tick():
    assert go(gate=PENDING).kind == "skip"


def test_gate1_red_is_marked_at_once_and_only_once():
    a = go(gate=RED)
    assert a.kind == "mark" and "Gate 1 is red (golden-road / drift is failure)" in a.reason and a.external_id.startswith("v1:gate1:")
    assert go(runs=[check("gate1")], gate=RED).kind == "skip"


def test_gate1_turning_green_after_a_red_mark_dispatches():
    a = go(runs=[check("gate1")], gate=GREEN)
    assert a.kind == "dispatch" and a.external_id == f"v1:queued:{fingerprint(pr())}:1"


def test_an_assessed_head_is_skipped_without_reading_gate1():
    assert go(runs=[check("queued")], gate=lambda: pytest.fail("Gate 1 read")).kind == "skip"


@pytest.mark.parametrize("change", [{"base": {"ref": "staging", "repo": {"full_name": REPO}}}, {"title": "new"}, {"body": "new"}])
def test_a_retargeted_base_or_an_edited_title_or_body_is_assessed_again(change):
    assert go(pr(**change), [check("queued")]).kind == "dispatch"


def test_the_fingerprint_ignores_everything_else():
    assert fingerprint(pr(updated_at="2030-01-01T00:00:00Z", number=8)) == fingerprint(pr())


def test_a_fresh_queued_check_is_left_alone():
    assert go(runs=[check(status="queued", started="2026-10-23T09:00:00Z")]).kind == "skip"


def test_a_stale_queued_check_is_expired_and_dispatched_again():
    a = go(runs=[check(status="queued", started="2026-10-23T05:59:00Z", id=10)])
    assert a.kind == "dispatch" and a.expire == 10 and a.attempt == 2 and a.external_id.endswith(":2")


def test_after_two_stale_attempts_the_head_is_marked_final():
    a = go(runs=[check(status="queued", attempt=MAX_ATTEMPTS, started="2026-10-23T01:00:00Z")])
    assert a.kind == "mark" and a.expire == 10 and a.external_id.startswith("v1:final:") and "platform team" in a.reason


def test_a_stale_check_while_gate1_is_pending_is_not_expired():
    assert go(runs=[check(status="queued", started="2026-10-23T01:00:00Z")], gate=PENDING).expire is None


@pytest.mark.parametrize("kw", [{"app": 15368}, {"head": "b" * 40}, {"name": "scorecard / assess"}])
def test_a_check_from_another_app_head_or_name_does_not_count(kw):
    assert go(runs=[check(**kw)]).kind == "dispatch"  # a same-named Actions job can't stand in for the App's check


def test_an_unrecognised_external_id_is_skipped_never_redispatched():
    run = check()
    run["external_id"] = "something else"
    assert go(runs=[run]).kind == "skip"


@pytest.mark.parametrize("bad", [{"number": True}, {"number": 0}, {"number": 10**10}, {"head": {"sha": "A" * 40, "repo": {"full_name": REPO}}},
                                 {"head": {"sha": "a" * 39, "repo": {"full_name": REPO}}}, {"base": {"ref": "main", "repo": {"full_name": "o/other"}}}])
def test_odd_pull_requests_are_skipped(bad):
    assert go(pr(**bad)).kind == "skip"


def test_gate1_state_maps_the_snapshot():
    assert gate1_state([{"name": "a", "state": "success"}]) == ("green", [])
    assert gate1_state([{"name": "a", "state": "success"}, {"name": "b", "state": "missing"}]) == ("pending", [])
    assert gate1_state([{"name": "a", "state": "pending"}, {"name": "b", "state": "failure"}]) == ("red", ["b is failure"])
    assert gate1_state([]) == ("pending", [])  # an empty list is never green


def test_the_check_names_are_remediate_s():
    assert ASSESSED == remediate.CHECKS["scorecard"]
    assert remediate.LEVEL_CHECK.search(LEVEL_PREFIX + "elevated")


# ---- review round: retryable outcomes, unknown ages, name case ----
from scorecard.central import QUEUED_TEXT, RETRY_BACKOFF_MINUTES, RETRY_MAX  # noqa: E402


def retry_check(attempt=1, completed="2026-10-23T11:00:00Z", fp=None):
    run = check("retry", fp=fp, attempt=attempt)
    run["completed_at"] = completed
    return run


def test_a_retry_mark_is_dispatched_again_after_the_backoff_with_the_next_attempt():
    a = go(runs=[retry_check(attempt=1)])
    assert a.kind == "dispatch" and a.attempt == 2 and a.external_id == f"v1:queued:{fingerprint(pr())}:2" and a.expire is None


def test_a_retry_mark_inside_the_backoff_waits():
    assert RETRY_BACKOFF_MINUTES == 10
    assert go(runs=[retry_check(completed="2026-10-23T11:55:00Z")]).kind == "skip"
    assert go(runs=[retry_check(completed="2026-10-23T11:49:00Z")]).kind == "dispatch"


def test_a_retry_mark_with_no_completion_time_is_not_stuck():
    run = retry_check()
    del run["completed_at"]
    assert go(runs=[run]).kind == "dispatch"


def test_retries_stop_at_the_cap_and_the_head_is_marked_final():
    a = go(runs=[retry_check(attempt=RETRY_MAX)])
    assert RETRY_MAX == 3 and a.kind == "mark" and a.external_id.startswith("v1:final:") and "platform team" in a.reason and "push" in a.reason
    assert go(runs=[check("final", attempt=RETRY_MAX)]).kind == "skip", "a final mark is not looked at again"


def test_a_retry_mark_for_an_older_fingerprint_starts_over():
    a = go(pr(title="edited"), [retry_check(attempt=2)])
    assert a.kind == "dispatch" and a.attempt == 1


def test_an_unknown_age_is_in_flight_not_stale():
    run = check(status="queued", started=None)
    assert go(runs=[run]).kind == "skip" and go(runs=[run]).expire is None


def test_the_queued_time_in_the_output_stands_in_for_a_missing_started_at():
    run = check(status="queued", started=None)
    run["output"] = {"title": "Queued", "summary": QUEUED_TEXT.format(now="2026-10-23T01:00:00Z")}
    assert go(runs=[run]).expire == 10
    run["output"]["summary"] = QUEUED_TEXT.format(now="2026-10-23T11:30:00Z")
    assert go(runs=[run]).kind == "skip"


def test_a_repository_name_that_differs_only_in_case_is_named_in_the_skip_reason():
    odd = pr(base={"ref": "main", "repo": {"full_name": "O/App"}}, head={"sha": HEAD, "repo": {"full_name": "O/App"}})
    a = go(odd)
    assert a.kind == "skip" and "differs in case" in a.reason and "O/App" in a.reason and REPO in a.reason
    assert "unexpected shape" in go(pr(number=0)).reason


# ---- review round: the intake field that gates the poller ----
from contracts.schema import validate  # noqa: E402

V1 = {"schema": "intake/v1", "repo": "o/app", "tier": "low", "purpose": "A notes API for the team", "owner": "team", "data_classification": "internal"}


@pytest.mark.parametrize("value,ok", [("central", True), ("per-app", True), ("both", False), ("", False), (True, False), (None, False)])
def test_intake_v1_takes_an_optional_scorecard_mode(value, ok):
    assert (validate("intake.v1", {**V1, "scorecard": value}) == []) is ok
    assert validate("intake.v1", V1) == [], "absent means per-app"


def test_the_portals_copy_of_intake_v2_carries_the_same_field():
    import json
    root = Path(__file__).resolve().parents[2]
    a = json.loads((root / "src/contracts/schemas/intake.v2.json").read_text())
    b = json.loads((root / "bootstrap/golden-road-portal/portal/schemas/intake.v2.json").read_text())
    assert a == b and a["properties"]["scorecard"] == {"enum": ["per-app", "central"]}
