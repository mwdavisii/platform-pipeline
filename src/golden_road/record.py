"""Signed records: the gate record a test release carries, and the promotion record Gate 2 adds."""

from contracts.schema import validate

from .io import InputError


def _authz_ok(authz) -> bool:
    status = authz.get("status")
    if status not in ("pass", "not-declared"):
        return False
    failures = authz.get("failures", [] if status == "not-declared" else None)
    total = authz.get("failures_total", 0)
    if failures != [] or type(total) is not int or total != 0:
        return False
    return status == "not-declared" or (type(authz.get("denied")) is int and authz["denied"] >= 1)


def build_gate_record(*, repo, commit, image, builder_image, drift, checks, authz, created, release, manifests_sha256, binding=None) -> dict:
    if validate("gate-result.v1", drift):
        raise InputError("drift input is not a gate result")
    if not isinstance(checks, dict) or not isinstance(checks.get("required_checks"), list) \
            or not all(isinstance(c, dict) for c in checks["required_checks"]):
        raise InputError("checks input is not a checks result")
    if not isinstance(authz, dict):
        raise InputError("authz input is not a probe result")
    checks_ok = checks.get("passed") is True and all(c.get("state") == "success" for c in checks["required_checks"])
    status = "pass" if drift["status"] == "pass" and checks_ok and _authz_ok(authz) else "fail"
    record = {
        "schema": "gate-record/v1",
        "status": status,
        "repo": repo,
        "commit": commit,
        "release": release,
        "manifests_sha256": manifests_sha256,
        "image": image,
        "builder_image": builder_image,
        "drift": {"status": drift["status"], "blocking": drift["blocking"]},
        "required_checks": checks["required_checks"],
        "authz_probe": authz,
        "created": created,
        "binding": binding,
    }
    errors = validate("gate-record.v1", record)
    if errors:
        raise InputError(f"invalid gate record: {errors[0]}")
    return record


def _is_bot(user: dict, login) -> bool:
    return user.get("type") == "Bot" or (isinstance(login, str) and login.lower().endswith("[bot]"))


def build_promotion_record(*, release, manifests_sha256, image, approvals, triggered_by, actor, run_attempt, run_url, created, single_approver=False) -> dict:
    if type(single_approver) is not bool:
        raise InputError("single_approver must be a boolean")
    if not isinstance(approvals, list):
        raise InputError("approvals must be a list")
    if not isinstance(triggered_by, str) or not isinstance(actor, str):
        raise InputError("triggered_by and actor must be strings")
    if type(run_attempt) is not int or run_attempt < 1:
        raise InputError("run_attempt must be a positive integer")
    if run_attempt > 1:
        raise InputError("a promotion can't be a re-run (run attempt above 1); re-run promotions by dispatching a new run")
    triggerers = {triggered_by.lower(), actor.lower()}
    excluded = set() if single_approver else triggerers
    approved: dict[str, dict] = {}
    for item in approvals:
        if not isinstance(item, dict) or item.get("state") != "approved":
            continue
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        login = user.get("login")
        if _is_bot(user, login):
            continue  # automation can't be a named reviewer; it neither counts nor blocks
        envs = {e["name"].lower() for e in item.get("environments") or [] if isinstance(e, dict) and isinstance(e.get("name"), str)}
        # GitHub logins and environment names are case-insensitive.
        if isinstance(login, str) and "prod" in envs and login.lower() not in excluded:
            entry = approved.setdefault(login.lower(), {"login": login, "comment": ""})
            if isinstance(item.get("comment"), str) and item["comment"].strip():
                entry["comment"] = item["comment"].strip()[:500]
    if not approved:
        raise InputError("no prod approval" if single_approver else
                         "no prod approval by someone other than the person who triggered or re-triggered the promotion")
    record = {
        "schema": "promotion-record/v1",
        "release": release,
        "manifests_sha256": manifests_sha256,
        "image": image,
        "gate2_single_approver": single_approver,
        "self_approved": single_approver and all(login in triggerers for login in approved),
        "approvals": sorted(approved.values(), key=lambda a: a["login"].lower()),
        "triggered_by": triggered_by,
        "actor": actor,
        "run_attempt": run_attempt,
        "run_url": run_url,
        "created": created,
    }
    errors = validate("promotion-record.v1", record)
    if errors:
        raise InputError(f"invalid promotion record: {errors[0]}")
    return record
