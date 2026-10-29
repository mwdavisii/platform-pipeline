import pytest

from contracts.schema import validate
from golden_road.io import InputError
from golden_road.record import build_gate_record, build_promotion_record

COMMIT = "c" * 40
IMAGE = "ghcr.io/org/hello-app@sha256:" + "d" * 64
BUILDER = "ghcr.io/org/golden-road-builder@sha256:" + "e" * 64
NOW = "2026-10-20T15:30:00Z"
RELEASE = "test-20261020153000-ccccccc"
MANIFESTS = "a" * 64


def gate(**overrides):
    args = dict(repo="org/hello-app", commit=COMMIT, image=IMAGE, builder_image=BUILDER,
                drift={"status": "pass", "counts": {"by_severity": {s: 0 for s in ("critical", "high", "medium", "low", "info")}, "by_layer": {}}, "blocking": [], "overrides_applied": []},
                checks={"passed": True, "required_checks": [{"name": "scanner-a", "state": "success"}]},
                authz={"status": "pass", "cases": 16, "denied": 9, "failures": []}, created=NOW,
                release=RELEASE, manifests_sha256=MANIFESTS)
    args.update(overrides)
    return build_gate_record(**args)


def test_pass_record_validates():
    record = gate()
    assert validate("gate-record.v1", record) == []
    assert record["status"] == "pass"


@pytest.mark.parametrize("override", [
    {"drift": {"status": "fail", "counts": {"by_severity": {s: 0 for s in ("critical", "high", "medium", "low", "info")}, "by_layer": {}}, "blocking": ["f" * 64], "overrides_applied": []}},
    {"checks": {"passed": False, "required_checks": [{"name": "scanner-a", "state": "failure"}]}},
    {"checks": {"passed": True, "required_checks": [{"name": "scanner-a", "state": "pending"}]}},
    {"authz": {"status": "fail", "cases": 4, "denied": 2, "failures": [{"method": "POST", "path": "/x", "roles": "reader", "status": 200}]}},
])
def test_any_failure_fails_record(override):
    assert gate(**override)["status"] == "fail"


def test_not_declared_authz_passes():
    assert gate(authz={"status": "not-declared"})["status"] == "pass"


def test_bad_inputs_raise():
    with pytest.raises(InputError):
        gate(image="ghcr.io/org/hello-app:latest")
    with pytest.raises(InputError):
        gate(drift={"status": "pass"})


def approval(login, state="approved", env="prod", comment="ok"):
    return {"state": state, "comment": comment, "user": {"login": login}, "environments": [{"name": env}]}


def promo(approvals, triggered_by="mike", actor="mike", run_attempt=1, **extra):
    return build_promotion_record(**extra, release=RELEASE, manifests_sha256=MANIFESTS, image=IMAGE, approvals=approvals,
                                  triggered_by=triggered_by, actor=actor, run_attempt=run_attempt,
                                  run_url="https://github.com/org/hello-app/actions/runs/1", created=NOW)


def logins(record):
    return [a["login"] for a in record["approvals"]]


def test_promotion_by_other_reviewer():
    record = promo([approval("deputy")])
    assert validate("promotion-record.v1", record) == []
    assert record["self_approved"] is False and record["gate2_single_approver"] is False
    assert logins(record) == ["deputy"]


@pytest.mark.parametrize("approvals", [
    [],
    [approval("mike")],
    [approval("deputy", state="rejected")],
    [approval("deputy", env="staging")],
    [{"state": "approved"}],
    "not a list",
])
def test_promotion_without_valid_independent_approval_raises(approvals):
    with pytest.raises(InputError):
        promo(approvals)


@pytest.mark.parametrize("login", ["Mike", "MIKE", "mIkE"])
def test_triggerer_cannot_approve_under_any_case(login):
    with pytest.raises(InputError):
        promo([approval(login)], triggered_by="mike")


def test_self_approval_ignored_but_independent_one_counts():
    record = promo([approval("Mike"), approval("deputy")])
    assert logins(record) == ["deputy"]


def test_approval_for_another_environment_does_not_count_alongside_others():
    with pytest.raises(InputError):
        promo([approval("deputy", env="staging"), approval("Mike")])


def test_malformed_checks_and_authz_raise():
    with pytest.raises(InputError):
        gate(authz="pass")
    with pytest.raises(InputError):
        gate(checks={"passed": True, "required_checks": ["scanner-a"]})


def test_pass_authz_with_failures_fails_record():
    assert gate(authz={"status": "pass", "cases": 4, "denied": 2, "failures": [{"method": "GET"}]})["status"] == "fail"


@pytest.mark.parametrize("created", ["not a date", "2026-10-20", "2026-10-20T15:30:00", "2026-10-20T15:30:00+00:00", "2026-10-20T15:30:00Z\n"])
def test_created_format_enforced(created):
    with pytest.raises(InputError):
        gate(created=created)
    with pytest.raises(InputError):
        build_promotion_record(release=RELEASE, manifests_sha256=MANIFESTS, image=IMAGE, approvals=[approval("deputy")],
                               triggered_by="mike", actor="mike", run_attempt=1, run_url="https://github.com/org/hello-app/actions/runs/1", created=created)


def test_pass_authz_with_no_denials_fails_record():
    assert gate(authz={"status": "pass", "cases": 4, "denied": 0, "failures": []})["status"] == "fail"


def bot(login="dependabot[bot]", **user):
    item = approval(login)
    item["user"].update(user)
    return item


def test_bot_approvals_are_skipped_not_fatal():
    assert logins(promo([bot(), bot("ci-helper", type="Bot"), approval("deputy")])) == ["deputy"]


def test_bot_approval_alone_raises():
    with pytest.raises(InputError):
        promo([bot()])
    with pytest.raises(InputError):
        promo([bot("ci-helper", type="Bot")])


def test_not_declared_with_failures_does_not_pass():
    assert gate(authz={"status": "not-declared", "failures": [{"method": "GET"}]})["status"] == "fail"
    assert gate(authz={"status": "not-declared", "failures_total": 3})["status"] == "fail"


def test_failures_total_must_be_zero_for_a_pass():
    ok = {"status": "pass", "cases": 4, "denied": 2, "failures": [], "failures_total": 0}
    assert gate(authz=ok)["status"] == "pass"
    assert gate(authz={**ok, "failures_total": 1})["status"] == "fail"
    with pytest.raises(InputError):
        gate(authz={**ok, "failures_total": True})


def test_actor_is_excluded_too_case_insensitively():
    with pytest.raises(InputError):
        promo([approval("Rerunner")], triggered_by="mike", actor="rerunner")
    with pytest.raises(InputError):
        promo([approval("MIKE")], triggered_by="mike", actor="someone")
    assert logins(promo([approval("Rerunner"), approval("deputy")], actor="rerunner")) == ["deputy"]


@pytest.mark.parametrize("attempt", [2, 3, 0, -1, True, "1", 1.0, None])
def test_reruns_and_bad_attempts_refused(attempt):
    with pytest.raises(InputError):
        promo([approval("deputy")], run_attempt=attempt)


def test_rerun_error_message_says_dispatch_a_new_run():
    with pytest.raises(InputError, match="dispatching a new run"):
        promo([approval("deputy")], run_attempt=2)


@pytest.mark.parametrize("env", ["Prod", "PROD"])
def test_prod_environment_is_case_insensitive(env):
    assert logins(promo([approval("deputy", env=env)])) == ["deputy"]


def test_record_shape_and_duplicate_approvals():
    record = promo([approval("Deputy", comment=""), approval("deputy", comment="ship it")])
    assert validate("promotion-record.v1", record) == []
    assert record["approvals"] == [{"login": "Deputy", "comment": "ship it"}]
    assert record["actor"] == "mike" and record["run_attempt"] == 1
    assert "approved_by" not in record and "comments" not in record


def test_trailing_newline_rejected_in_every_patterned_field():
    from contracts.schema import validate as v
    record = gate()
    for field in ("repo", "commit", "image", "builder_image", "created", "release", "manifests_sha256"):
        assert v("gate-record.v1", {**record, field: record[field] + "\n"}), field
    promotion = promo([approval("deputy")])
    assert v("promotion-record.v1", {**promotion, "approvals": [{**promotion["approvals"][0], "login": "deputy\n"}]})
    for field in ("release", "image", "triggered_by", "actor", "run_url", "created", "manifests_sha256"):
        assert v("promotion-record.v1", {**promotion, field: promotion[field] + "\n"}), field


def test_records_bind_release_and_manifests():
    record = gate()
    assert record["release"] == RELEASE and record["manifests_sha256"] == MANIFESTS
    assert promo([approval("deputy")])["manifests_sha256"] == MANIFESTS
    assert promo([approval("deputy")])["release"] == RELEASE


@pytest.mark.parametrize("field,value", [
    ("manifests_sha256", "A" * 64), ("manifests_sha256", "a" * 63), ("manifests_sha256", "a" * 64 + "\n"),
    ("release", "prod-20261020153000-ccccccc"), ("release", "test-1-ccccccc"), ("release", RELEASE + "\n"),
])
def test_bad_release_or_manifests_digest_refused(field, value):
    with pytest.raises(InputError):
        gate(**{field: value})
    with pytest.raises(InputError):
        build_promotion_record(**{**dict(release=RELEASE, manifests_sha256=MANIFESTS, image=IMAGE, approvals=[approval("deputy")],
                                         triggered_by="mike", actor="mike", run_attempt=1,
                                         run_url="https://github.com/org/hello-app/actions/runs/1", created=NOW), field: value})


def test_single_approver_counts_self_approval_and_marks_it():
    record = promo([approval("Mike")], single_approver=True)
    assert validate("promotion-record.v1", record) == []
    assert logins(record) == ["Mike"]
    assert record["self_approved"] is True and record["gate2_single_approver"] is True
    assert promo([approval("mike")], triggered_by="other", actor="mike", single_approver=True)["self_approved"] is True


def test_single_approver_with_another_approver_is_not_self_approved():
    record = promo([approval("mike"), approval("deputy")], single_approver=True)
    assert logins(record) == ["deputy", "mike"]
    assert record["self_approved"] is False and record["gate2_single_approver"] is True
    assert promo([approval("deputy")], single_approver=True)["self_approved"] is False


def test_flag_off_is_never_self_approved_and_still_refuses_self():
    assert promo([approval("deputy")], single_approver=False)["self_approved"] is False
    with pytest.raises(InputError):
        promo([approval("mike")], single_approver=False)


@pytest.mark.parametrize("approvals", [[], [approval("mike", state="rejected")], [approval("mike", env="staging")],
                                       [{"state": "approved", "user": {"login": "ci[bot]", "type": "Bot"}, "environments": [{"name": "prod"}]}]])
def test_single_approver_still_needs_a_real_prod_approval(approvals):
    with pytest.raises(InputError):
        promo(approvals, single_approver=True)


def test_single_approver_still_refuses_reruns():
    with pytest.raises(InputError):
        promo([approval("mike")], run_attempt=2, single_approver=True)


@pytest.mark.parametrize("value", [1, "true", None])
def test_single_approver_must_be_a_bool(value):
    with pytest.raises(InputError):
        promo([approval("mike")], single_approver=value)


def test_schema_requires_self_approved_boolean():
    record = promo([approval("deputy")])
    assert validate("promotion-record.v1", {k: v for k, v in record.items() if k != "self_approved"})
    assert validate("promotion-record.v1", {**record, "self_approved": "false"})
    assert validate("promotion-record.v1", {**record, "self_approved": 0})


def test_schema_requires_gate2_single_approver_boolean():
    record = promo([approval("deputy")])
    assert validate("promotion-record.v1", {k: v for k, v in record.items() if k != "gate2_single_approver"})
    assert validate("promotion-record.v1", {**record, "gate2_single_approver": "false"})
    assert validate("promotion-record.v1", {**record, "gate2_single_approver": 0})


def test_the_gate_record_names_the_binding_or_none():
    with_binding = gate(binding={"path": "bindings/acme/notes.yaml", "sha256": "c" * 64})
    assert with_binding["binding"] == {"path": "bindings/acme/notes.yaml", "sha256": "c" * 64}
    assert validate("gate-record.v1", with_binding) == []
    assert gate()["binding"] is None
    with pytest.raises(InputError):
        gate(binding={"path": "../evil.yaml", "sha256": "c" * 64})
