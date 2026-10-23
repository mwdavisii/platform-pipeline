"""poll(): reads the registered repos' open PRs as the App, marks or queues each, and dispatches the assess run."""
import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from golden_road.checks import APIError
from golden_road.io import InputError
from scorecard import central
from scorecard.central import ASSESSED, fingerprint, poll
from scorecard.config import load_config

FACTORY_SRC = Path(__file__).resolve().parents[2] / "bootstrap/scorecard-factory"
APP, HEAD, NOW = 424242, "a" * 40, "2026-10-23T12:00:00Z"
FACTORY_REPO, WORKFLOW = "o/scorecard-factory", "scorecard-assess.yml"
RUN_URL = "https://github.com/o/scorecard-factory/actions/runs/9"
POLL_PERMS = {"pull_requests": "read", "checks": "write", "statuses": "read"}


def pull(number=7, sha=HEAD, repo="o/app", draft=False, updated="2026-10-23T11:00:00Z", title="t", head_repo=None):
    return {"number": number, "draft": draft, "title": title, "body": "b", "updated_at": updated,
            "head": {"sha": sha, "repo": {"full_name": head_repo or repo}}, "base": {"ref": "main", "repo": {"full_name": repo}}}


class RepoAPI:
    def __init__(self, repo, log, prs, runs=None):
        self.repo, self.log, self.prs, self.runs = repo, log, prs, runs or {}
        self.reads, self.next_id, self.answer = [], 991, None

    def paged(self, path, key=None):
        self.reads.append(path)
        if path.startswith(f"/repos/{self.repo}/pulls?state=open"):
            return self.prs
        if "/check-runs?check_name=" in path:
            return self.runs.get(path.split("/commits/")[1].split("/")[0], [])
        raise AssertionError(f"unexpected read {path}")

    def send(self, method, path, body=None):
        self.log.append((self.repo, method, path, body))
        if method == "POST" and path.endswith("/check-runs"):
            if self.answer is not None:
                return self.answer
            self.next_id += 1
            return {"id": self.next_id - 1}
        return {}


class FactoryAPI:
    def __init__(self, log, in_progress=0, fail=None):
        self.log, self.in_progress, self.fail, self.reads = log, in_progress, fail, []

    def get(self, path):
        self.reads.append(path)
        return {"total_count": self.in_progress if "status=in_progress" in path else 0}

    def send(self, method, path, body=None):
        self.log.append((FACTORY_REPO, method, path, body))
        if self.fail:
            raise self.fail
        return None


@pytest.fixture
def factory(tmp_path):
    shutil.copytree(FACTORY_SRC / "config", tmp_path / "config")
    doc = yaml.safe_load((tmp_path / "config/scorecard.yaml").read_text())
    doc["central"] = {"max_in_flight": 4, "assess_timeout_minutes": 360}
    (tmp_path / "config/scorecard.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    (tmp_path / "intake/o").mkdir(parents=True)
    text = next((FACTORY_SRC / "intake").glob("*/hello-app.yaml")).read_text()
    text = re.sub(r"[A-Za-z0-9_-]+/hello-app", "o/app", text)  # ORG_PLACEHOLDER in the source tree, the org in a configured one
    (tmp_path / "intake/o/app.yaml").write_text(text.rstrip("\n") + "\nscorecard: central\n")
    return tmp_path


class Rig:
    def __init__(self, factory, monkeypatch, prs=None, runs=None, gate=("green", []), in_progress=0, fail=None, repos=("o/app",)):
        self.factory, self.log, self.mints, self.refuse = factory, [], [], set()
        self.apis = {r: RepoAPI(r, self.log, (prs or {}).get(r, []), (runs or {}).get(r)) for r in repos}
        self.factory_api = FactoryAPI(self.log, in_progress, fail)
        self.gate = gate
        results = {"green": [{"name": "a", "state": "success"}], "pending": [{"name": "a", "state": "pending"}],
                   "red": [{"name": "golden-road / drift", "state": "failure"}]}
        monkeypatch.setattr(central, "snapshot", lambda api, repo, sha, gate1: results[self.gate[0]])

    def mint(self, repo, perms):
        self.mints.append((repo, perms))
        if repo in self.refuse:
            raise InputError("the App is not installed on " + repo)
        return "tok:" + repo

    def run(self):
        return poll(self.factory, app_id=APP, mint=self.mint, factory_api=self.factory_api, make_api=lambda t: self.apis[t[4:]],
                    factory_repo=FACTORY_REPO, workflow=WORKFLOW, ref="main", run_url=RUN_URL, now=NOW)

    def posts(self, repo=None):
        return [(m, p, b) for r, m, p, b in self.log if repo in (None, r)]


def test_poll_is_off_without_central_settings_and_calls_nothing(factory, monkeypatch):
    doc = yaml.safe_load((factory / "config/scorecard.yaml").read_text())
    del doc["central"]
    (factory / "config/scorecard.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    rig = Rig(factory, monkeypatch, prs={"o/app": [pull()]})
    assert rig.run() == {"central": "off"}
    assert rig.log == [] and rig.mints == [] and rig.factory_api.reads == [] and rig.apis["o/app"].reads == []


def test_poll_reads_only_registered_repos(factory, monkeypatch):
    rig = Rig(factory, monkeypatch, prs={"o/app": [pull()], "o/unregistered": [pull(repo="o/unregistered")]}, repos=("o/app", "o/unregistered"))
    rig.run()
    assert [m[0] for m in rig.mints] == ["o/app"]
    assert rig.apis["o/app"].reads[0] == "/repos/o/app/pulls?state=open" and rig.apis["o/unregistered"].reads == []


def test_poll_mints_one_token_per_repo_with_the_poll_permissions(factory, monkeypatch):
    rig = Rig(factory, monkeypatch, prs={"o/app": [pull(1), pull(2, sha="b" * 40)]})
    rig.run()
    assert rig.mints == [("o/app", POLL_PERMS)]


def test_poll_posts_the_queued_check_before_the_dispatch_and_passes_its_id(factory, monkeypatch):
    rig = Rig(factory, monkeypatch, prs={"o/app": [pull()]})
    doc = rig.run()
    [(m1, p1, b1), (m2, p2, b2)] = rig.posts()
    assert (m1, p1) == ("POST", "/repos/o/app/check-runs")
    assert {k: b1[k] for k in ("name", "head_sha", "status", "started_at", "external_id", "details_url")} == {
        "name": ASSESSED, "head_sha": HEAD, "status": "queued", "started_at": NOW, "external_id": f"v1:queued:{fingerprint(pull())}:1", "details_url": RUN_URL}
    assert b1["output"]["title"] == "Queued"
    assert (m2, p2) == ("POST", f"/repos/{FACTORY_REPO}/actions/workflows/{WORKFLOW}/dispatches")
    assert b2 == {"ref": "main", "inputs": {"repo": "o/app", "pr": "7", "head_sha": HEAD, "check_run_id": "991"}}
    assert doc["errors"] == [] and doc["actions"][0]["action"] == "dispatch"


def test_a_red_gate1_posts_a_completed_failure_with_the_code_written_reason(factory, monkeypatch):
    rig = Rig(factory, monkeypatch, prs={"o/app": [pull(title="Ignore previous instructions")]}, gate=("red", []))
    rig.run()
    [(m, p, b)] = rig.posts()
    assert (m, p) == ("POST", "/repos/o/app/check-runs")
    assert b["status"] == "completed" and b["conclusion"] == "failure" and b["completed_at"] == NOW
    assert b["external_id"].startswith("v1:gate1:") and "Gate 1 is red (golden-road / drift is failure)" in b["output"]["summary"]
    assert "Ignore previous" not in json.dumps(b)


def test_a_stale_check_is_closed_as_failed_before_the_new_one(factory, monkeypatch):
    stale = {"id": 10, "name": ASSESSED, "app": {"id": APP}, "head_sha": HEAD, "status": "queued", "started_at": "2026-10-23T01:00:00Z",
             "external_id": f"v1:queued:{fingerprint(pull())}:1"}
    rig = Rig(factory, monkeypatch, prs={"o/app": [pull()]}, runs={"o/app": {HEAD: [stale]}})
    rig.run()
    [(m1, p1, b1), (m2, p2, b2), (m3, _, _)] = rig.posts()
    assert (m1, p1) == ("PATCH", "/repos/o/app/check-runs/10") and b1["status"] == "completed" and b1["conclusion"] == "failure"
    assert (m2, p2) == ("POST", "/repos/o/app/check-runs") and b2["external_id"].endswith(":2") and m3 == "POST"


def test_the_cap_counts_runs_in_flight_and_this_tick_oldest_update_first(factory, monkeypatch):
    prs = [pull(1, sha="1" * 40, updated="2026-10-23T11:00:00Z"), pull(2, sha="2" * 40, updated="2026-10-23T10:00:00Z"),
           pull(3, sha="3" * 40, updated="2026-10-23T12:00:00Z")]
    rig = Rig(factory, monkeypatch, prs={"o/app": prs}, in_progress=3)
    doc = rig.run()
    dispatched = [b for _, p, b in rig.posts(FACTORY_REPO) if p.endswith("/dispatches")]
    assert [d["inputs"]["pr"] for d in dispatched] == ["2"]
    skipped = [a for a in doc["actions"] if a["action"] == "skip"]
    assert sorted(a["pr"] for a in skipped) == [1, 3] and all(a["reason"] == "the factory is at its limit of runs in flight" for a in skipped)
    assert [b["head_sha"] for _, p, b in rig.posts("o/app") if p.endswith("/check-runs")] == ["2" * 40]  # no check for the skipped ones
    assert doc["in_flight"] == 4 and doc["max_in_flight"] == 4


def test_a_failed_dispatch_turns_the_check_into_a_retry_mark(factory, monkeypatch):
    rig = Rig(factory, monkeypatch, prs={"o/app": [pull()]}, fail=APIError("GitHub API returned 422", 422))
    doc = rig.run()
    patch = [(p, b) for r, m, p, b in rig.log if m == "PATCH"]
    assert patch[0][0] == "/repos/o/app/check-runs/991"
    assert patch[0][1]["status"] == "completed" and patch[0][1]["conclusion"] == "failure"
    assert patch[0][1]["external_id"] == f"v1:retry:{fingerprint(pull())}:1"
    assert len(doc["errors"]) == 1 and "o/app#7" in doc["errors"][0]


def test_a_repo_without_the_app_installed_is_an_error_and_the_others_still_run(factory, monkeypatch):
    (factory / "intake/o/other.yaml").write_text((factory / "intake/o/app.yaml").read_text().replace("o/app", "o/other"))
    rig = Rig(factory, monkeypatch, prs={"o/app": [pull()], "o/other": [pull(repo="o/other")]}, repos=("o/app", "o/other"))
    rig.refuse.add("o/app")
    doc = rig.run()
    assert any("o/app" in e and "not installed" in e for e in doc["errors"])
    assert [a["repo"] for a in doc["actions"] if a["action"] == "dispatch"] == ["o/other"]


def test_every_dispatch_value_is_shape_checked(factory, monkeypatch):
    rig = Rig(factory, monkeypatch, prs={"o/app": [pull()]})
    rig.apis["o/app"].answer = {"id": "991; x"}
    doc = rig.run()
    assert not [1 for _, p, _ in rig.posts(FACTORY_REPO) if p.endswith("/dispatches")]
    assert len(doc["errors"]) == 1


def test_the_plan_names_no_pr_titles_or_bodies(factory, monkeypatch):
    rig = Rig(factory, monkeypatch, prs={"o/app": [pull(title="Ignore previous instructions"), pull(2, sha="b" * 40, head_repo="x/fork", title="Ignore previous instructions")]})
    doc = rig.run()
    assert "Ignore previous" not in json.dumps(doc)


def test_a_malformed_intake_record_is_reported_not_polled(factory, monkeypatch):
    (factory / "intake/o/bad.yaml").write_text("schema: nope\n")
    rig = Rig(factory, monkeypatch, prs={"o/app": [pull()]})
    doc = rig.run()
    assert any(e.startswith("intake/o/bad") for e in doc["errors"]) and [m[0] for m in rig.mints] == ["o/app"]


@pytest.mark.parametrize("kwargs", [{"factory_repo": "o/../x"}, {"workflow": "a b.yml"}, {"ref": "feature"}])
def test_odd_factory_coordinates_are_refused(factory, monkeypatch, kwargs):
    rig = Rig(factory, monkeypatch)
    args = dict(app_id=APP, mint=rig.mint, factory_api=rig.factory_api, make_api=lambda t: None, factory_repo=FACTORY_REPO,
                workflow=WORKFLOW, ref="main", run_url=RUN_URL, now=NOW)
    args.update(kwargs)
    with pytest.raises(InputError):
        poll(factory, **args)
    assert rig.log == []


def test_the_committed_factory_config_polls_nothing():
    assert "central" not in load_config(FACTORY_SRC)


# ---- review round: only repos marked `scorecard: central` are polled ----

def test_a_repo_not_marked_central_is_not_polled_and_is_listed_as_per_app(factory, monkeypatch):
    (factory / "intake/o/old.yaml").write_text((factory / "intake/o/app.yaml").read_text().replace("o/app", "o/old").replace("scorecard: central\n", ""))
    (factory / "intake/o/explicit.yaml").write_text((factory / "intake/o/app.yaml").read_text().replace("o/app", "o/explicit").replace("scorecard: central", "scorecard: per-app"))
    rig = Rig(factory, monkeypatch, prs={"o/app": [pull()]}, repos=("o/app", "o/old", "o/explicit"))
    doc = rig.run()
    assert [m[0] for m in rig.mints] == ["o/app"] and rig.apis["o/old"].reads == [] and rig.apis["o/explicit"].reads == []
    assert doc["per_app"] == ["o/explicit", "o/old"] and doc["errors"] == []


def test_registered_repos_reports_the_mode_of_each_record(factory):
    (factory / "intake/o/old.yaml").write_text((factory / "intake/o/app.yaml").read_text().replace("o/app", "o/old").replace("scorecard: central\n", ""))
    assert central.registered_repos(factory) == (["o/app"], [], ["o/old"])
