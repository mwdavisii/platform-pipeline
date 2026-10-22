import pytest

from golden_road.io import InputError
from scorecard.observations import checks_from_doc, drift_green, observe, read_conclusions
from scorecard.precondition import not_assessed as _not_assessed

GATE1 = [{"name": "golden-road / drift", "app_id": 15368}, {"name": "golden-road / preview", "app_id": 15368}]
GREEN = [{"name": "golden-road / drift", "state": "success"}, {"name": "golden-road / preview", "state": "success"}]
SHA = "a" * 40
EVENT = {"pull_request": {"draft": False, "head": {"repo": {"full_name": "acme/notes"}}, "base": {"repo": {"full_name": "acme/notes"}}}}


def not_assessed(gate1, checks, passed, event=EVENT, head=SHA, checked=SHA):
    return _not_assessed(gate1, checks, passed, event, head_sha=head, checks_sha=checked)


def test_green_gate1_is_assessed():
    assert not_assessed(GATE1, GREEN, True) == []


def test_empty_gate1_list_is_not_assessed():
    """AC-15, as a pure function of the check list."""
    assert not_assessed([], [], True) == ["no Gate 1 checks configured"]


def test_failing_or_missing_gate1_check():
    checks = [{"name": "golden-road / drift", "state": "failure"}]
    assert not_assessed(GATE1, checks, False) == ["Gate 1 check 'golden-road / drift' is failure", "Gate 1 check 'golden-road / preview' is missing"]
    assert not_assessed(GATE1, None, None) == ["no Gate 1 result"]
    assert not_assessed(GATE1, GREEN, False) == ["Gate 1 is not green"]


def test_a_green_duplicate_does_not_hide_a_failing_check():
    checks = [{"name": "golden-road / drift", "state": "failure"}, {"name": "golden-road / drift", "state": "success"}, *GREEN[1:]]
    assert not_assessed(GATE1, checks, False) == ["Gate 1 check 'golden-road / drift' is failure"]


@pytest.mark.parametrize(
    "event, reason",
    [
        ({"pull_request": {**EVENT["pull_request"], "draft": True}}, "the PR is a draft"),
        ({"pull_request": {**EVENT["pull_request"], "head": {"repo": {"full_name": "someone/notes"}}}}, "fork"),
        ({"push": {}}, "not a pull_request event"),
    ],
)
def test_draft_fork_and_other_events(event, reason):
    assert any(reason in r for r in not_assessed(GATE1, GREEN, True, event))


def test_event_is_required():
    assert any("no event payload" in r for r in not_assessed(GATE1, GREEN, True, None))


def test_gate1_result_must_be_for_the_assessed_commit():
    other = "b" * 40
    for kwargs in ({"checked": other}, {"checked": None}, {"head": None}):
        assert any("not for the commit" in r for r in not_assessed(GATE1, GREEN, True, **kwargs))
    assert not any("not for the commit" in r for r in not_assessed(GATE1, None, None))


def test_checks_doc_accepts_a_sha_and_rejects_a_malformed_one():
    assert checks_from_doc({"passed": True, "required_checks": GREEN, "sha": SHA}) == GREEN
    with pytest.raises(InputError):
        checks_from_doc({"passed": True, "required_checks": GREEN, "sha": "main"})
    with pytest.raises(InputError):
        checks_from_doc({"passed": True, "required_checks": GREEN, "extra": 1})


def test_drift_needs_every_entry_green():
    observed = observe([{"name": "d", "state": "success"}, {"name": "d", "state": "failure"}], "2026-10-21T12:00:00Z")
    assert not drift_green(observed, "d")
    assert not drift_green([], "d")


def test_checks_doc_is_strict():
    assert checks_from_doc({"passed": True, "required_checks": GREEN}) == GREEN
    for bad in ({"passed": "yes", "required_checks": []}, {"passed": True, "required_checks": [{"name": "x", "state": "ok"}]}, []):
        with pytest.raises(InputError):
            checks_from_doc(bad)


def test_observations_and_drift():
    observed = observe(GREEN, "2026-10-21T12:00:00Z")
    assert observed[0] == {"name": "golden-road / drift", "state": "success", "observed_at": "2026-10-21T12:00:00Z"}
    assert drift_green(observed, "golden-road / drift")
    assert not drift_green(observe([{"name": "golden-road / drift", "state": "pending"}], "2026-10-21T12:00:00Z"), "golden-road / drift")


class _API:
    def paged(self, path, key=None):
        if path.endswith("/check-runs"):
            return [{"id": 1, "name": "golden-road / drift", "status": "completed", "conclusion": "success", "app": {"id": 15368}, "head_sha": "a" * 40}]
        return []


def test_live_read_uses_the_pinned_snapshot():
    observed = read_conclusions(_API(), "acme/notes", "a" * 40, GATE1, "2026-10-21T12:00:00Z")
    assert [(o["name"], o["state"]) for o in observed] == [("golden-road / drift", "success"), ("golden-road / preview", "missing")]
