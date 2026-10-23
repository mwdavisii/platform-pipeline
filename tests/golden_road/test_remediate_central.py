"""golden-road remediate with central review: the scorecard comment comes from the scorecard App, a failed App check is not re-run
from the PR, and a draft (which central review posts nothing for) still gets its fix."""
import json
from pathlib import Path

from contracts.schema import validate
from golden_road import remediate
from golden_road.doctor import GhResult
from remediation_fixtures import check, drift_comment, finding, pr, scorecard_comment  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
MARKER = "<!-- golden-road:scorecard -->"
ASSESSED = remediate.CHECKS["scorecard"]
APP = 99


def line(body, login="github-actions[bot]", type_="Bot", app=None, id_=1, at="2026-10-23T10:00:00Z"):
    return json.dumps({"id": id_, "login": login, "type": type_, "app": app, "updated_at": at, "body": body})


def lines(*items):
    return "\n".join(items)


def test_the_scorecard_comment_from_the_scorecard_app_is_read_in_central_mode():
    text = line(f"{MARKER}\nfrom the app", login="scorecard-x[bot]", app=APP)
    assert remediate._marker_comments(text, scorecard_app=APP) == {"scorecard": f"{MARKER}\nfrom the app"}


def test_another_app_s_look_alike_is_ignored():
    text = line(f"{MARKER}\nlook-alike", login="other[bot]", app=100)
    assert remediate._marker_comments(text, scorecard_app=APP) == {}
    assert remediate._marker_comments(line(f"{MARKER}\nuser", login="someone", type_="User", app=APP), scorecard_app=APP) == {}, "the App's comments are Bot"


def test_a_drift_marker_from_the_scorecard_app_is_ignored():
    text = line("<!-- golden-road:drift -->\nforged", login="scorecard-x[bot]", app=APP)
    assert remediate._marker_comments(text, scorecard_app=APP) == {}


def test_github_actions_comments_still_count():
    drift = "<!-- golden-road:drift -->\nreal"
    assert remediate._marker_comments(lines(line(f"{MARKER}\nper app"), line(drift, id_=2))) == {"scorecard": f"{MARKER}\nper app", "drift": drift}
    assert remediate._marker_comments(line(drift, id_=2), scorecard_app=APP) == {"drift": drift}, "the scans and drift still come from github-actions[bot]"


def test_in_central_mode_a_github_actions_look_alike_cannot_stand_in_for_the_apps_scorecard_comment():
    forged = line(f"{MARKER}\nforged by a workflow in the app repo", id_=5, at="2026-10-23T11:00:00Z")
    real = line(f"{MARKER}\nreal", login="scorecard-x[bot]", app=APP, id_=4, at="2026-10-23T10:00:00Z")
    assert remediate._marker_comments(lines(real, forged), scorecard_app=APP) == {"scorecard": f"{MARKER}\nreal"}
    assert remediate._marker_comments(forged, scorecard_app=APP) == {}


def test_COMMENT_JQ_reads_the_app_id():
    assert ".performed_via_github_app.id" in remediate.COMMENT_JQ
    assert "app:" in remediate.COMMENT_JQ and "login: .user.login" in remediate.COMMENT_JQ


def classify(checks, central=True, **pr_kw):
    doc = remediate.classify(pr(**pr_kw), checks, {}, None, ROOT, "github.com", "o/r", scorecard_central=central)
    assert validate("remediation.v1", doc) == []
    return doc


def test_a_failed_app_check_in_central_mode_says_push_again():
    doc = classify([check(ASSESSED, workflow="")])
    [item] = doc["items"]
    assert "Push a new commit" in item["guidance"] and "e-run it once" not in item["guidance"] and item["rerun_command"] is None
    assert item["check"] == ASSESSED and item["source"] == "scorecard"


def test_a_failed_app_check_in_per_app_mode_is_unchanged():
    [item] = classify([check(ASSESSED)], central=False)["items"]
    assert "Re-run it once" in item["guidance"] and item["rerun_command"] == "gh run rerun 5 --failed --repo o/r"


def test_a_draft_in_central_mode_gets_the_mark_ready_fix_without_a_scorecard_check():
    doc = classify([check("golden-road / drift", bucket="pass", state="SUCCESS")], isDraft=True)
    [item] = doc["items"]
    assert item["category"] == "fix" and item["check"] == ASSESSED and "gh pr ready 7 --repo o/r" in item["guidance"]


def test_a_draft_in_per_app_mode_without_the_check_has_nothing_to_say():
    assert classify([check("golden-road / drift", bucket="pass", state="SUCCESS")], central=False, isDraft=True)["items"] == []


def test_a_draft_that_central_review_never_assessed_is_not_reported_twice():
    doc = classify([check(ASSESSED, workflow="")], isDraft=True)
    assert [i["category"] for i in doc["items"]] == ["fix"] and len(doc["items"]) == 1


def test_a_ready_pr_with_no_scorecard_check_yet_has_nothing_to_say_in_central_mode():
    assert classify([check("golden-road / drift", bucket="pass", state="SUCCESS")])["items"] == []


def test_the_apps_level_check_is_read_like_any_level_check():
    doc = classify([check("scorecard / level: elevated", bucket="pass", state="SUCCESS", workflow="")])
    assert [i["category"] for i in doc["items"]] == ["needs-human"]


def test_collect_reads_the_app_id_from_the_platform_config(monkeypatch):
    monkeypatch.setattr(remediate, "load_platform", lambda: {"scorecard_mode": "central", "scorecard_app_id": 7})
    assert remediate.central_app() == 7
    monkeypatch.setattr(remediate, "load_platform", lambda: {})
    assert remediate.central_app() is None
    monkeypatch.setattr(remediate, "load_platform", lambda: {"scorecard_mode": "central"})
    assert remediate.central_app() is None, "an unusable config is per-app, never a crash"

    def broken():
        raise OSError("gone")
    monkeypatch.setattr(remediate, "load_platform", broken)
    assert remediate.central_app() is None


# ---- review round: the App's comment found by its login when GitHub gives no App id ----

LOGIN = "scorecard-x[bot]"


def test_the_apps_comment_is_found_by_its_login_when_github_gives_no_app_id():
    text = line(f"{MARKER}\nby login", login=LOGIN, app=None)
    assert remediate._marker_comments(text, scorecard_app=APP, scorecard_login=LOGIN) == {"scorecard": f"{MARKER}\nby login"}
    assert remediate._marker_comments(text, scorecard_app=APP) == {}, "without the login or the id nothing identifies the App"


def test_a_look_alike_login_or_a_user_cannot_stand_in_for_the_app():
    for forged in (line(f"{MARKER}\nx", login="github-actions[bot]"), line(f"{MARKER}\nx", login="other-app[bot]"),
                   line(f"{MARKER}\nx", login="scorecard-x", type_="User"), line(f"{MARKER}\nx", login="scorecard-x[bot]x")):
        assert remediate._marker_comments(forged, scorecard_app=APP, scorecard_login=LOGIN) == {}, forged


class FakeRun:
    """Answers collect's gh reads from a table keyed by what the command starts with."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, root, host, args):
        self.calls.append(args)
        for prefix, (code, out) in self.answers.items():
            if " ".join(args).startswith(prefix):
                return GhResult(code, out, "")
        raise AssertionError(f"no answer for {args}")


def _collect(monkeypatch, app, slug_answer, comments):
    pr_doc = json.dumps({"number": 7, "headRefOid": "a" * 40, "state": "OPEN", "isDraft": False, "isCrossRepository": False, "headRefName": "f", "url": "https://github.com/o/r/pull/7"})
    fake = FakeRun({"pr view": (0, pr_doc), "pr checks": (0, "[]"), "api repos/o/r/commits/": slug_answer, "api repos/o/r/issues/7/comments": (0, comments)})
    monkeypatch.setattr(remediate, "run_gh", fake)
    monkeypatch.setattr(remediate, "central_app", lambda: app)
    return remediate.collect("o/r", 7, "github.com", ROOT), fake


def test_collect_reads_the_apps_slug_from_the_check_run_and_trusts_that_login(monkeypatch):
    comments = line(f"{MARKER}\nby login", login=LOGIN, app=None)
    (_, _, got, _, _), fake = _collect(monkeypatch, APP, (0, "scorecard-x\n"), comments)
    assert got == {"scorecard": f"{MARKER}\nby login"}
    read = next(" ".join(c) for c in fake.calls if c[:2] == ["api", f"repos/o/r/commits/{'a' * 40}/check-runs?check_name=scorecard%20%2F%20assessed&filter=latest&per_page=30"])
    assert f".app.id == {APP}" in read and ".app.slug" in read


def test_collect_without_a_readable_slug_still_trusts_the_app_id(monkeypatch):
    by_id = line(f"{MARKER}\nby id", login="scorecard-x[bot]", app=APP)
    for answer in ((1, ""), (0, ""), (0, "Not A Slug\n"), (0, "../x\n")):
        (_, _, got, _, _), _ = _collect(monkeypatch, APP, answer, by_id)
        assert got == {"scorecard": f"{MARKER}\nby id"}, answer
    forged = line(f"{MARKER}\nx", login=LOGIN, app=None)
    (_, _, got, _, _), _ = _collect(monkeypatch, APP, (0, "Not A Slug\n"), forged)
    assert got == {}


def test_collect_in_per_app_mode_makes_no_slug_read(monkeypatch):
    comments = line(f"{MARKER}\nper app")
    (_, _, got, _, _), fake = _collect(monkeypatch, None, (99, ""), comments)
    assert got == {"scorecard": f"{MARKER}\nper app"} and not [c for c in fake.calls if "commits" in " ".join(c)]
