"""The CLI with a routing factory: assess writes facts-assessment/v1, run writes scorecard/v2 and the usual files."""

import json

from contracts.schema import validate
from scorecard.agent.fake import FactsModel
from scorecard.cli import main


def _common(kw, factory, tmp_path):
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps(kw["checks_doc"]))
    event = tmp_path / "event.json"
    event.write_text(json.dumps(kw["event"]))
    return ["--repo-root", str(kw["repo_root"]), "--repo", kw["repo"], "--base", kw["base_ref"], "--factory", str(factory),
            "--checks", str(checks), "--event", str(event), "--now", kw["now"]]


class _Registry:
    def lookup(self, ecosystem, name, version):
        return {"exists": True, "version_exists": True, "first_release": "2019-01-01", "maintainers": 5, "has_wheel": True}


def test_assess_then_run(pr_checkout, routing_factory, tmp_path, capsys):
    common = _common(pr_checkout(), routing_factory, tmp_path)
    assessment = tmp_path / "assessment.json"
    assert main(["assess", *common, "--out", str(assessment)], model=FactsModel()) == 0
    assert validate("facts-assessment.v1", json.loads(assessment.read_text())) == []
    out = tmp_path / "out"
    out.mkdir()
    assert main(["run", *common, "--assessment", str(assessment), "--out", str(out)], registry=_Registry()) == 0
    card = json.loads((out / "scorecard.json").read_text())
    assert card["schema"] == "scorecard/v2" and card["level"] == "standard"
    comment = (out / "comment.md").read_text()
    assert comment.startswith("<!-- golden-road:scorecard -->") and "### Reasons" in comment
    from golden_road.pr_comments import parse_scorecard
    assert parse_scorecard(comment)["level"] == "standard"
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"level": "standard", "triggers_fired": []}


def test_scope_is_refused(pr_checkout, routing_factory, tmp_path, capsys):
    common = _common(pr_checkout(), routing_factory, tmp_path)
    assert main(["scope", *common, "--out", str(tmp_path / "scope.json")]) == 1
    assert "no ASVS scope" in capsys.readouterr().err


def test_check_factory_accepts_the_routing_factory(routing_factory, capsys):
    assert main(["check-factory", "--factory", str(routing_factory)]) == 0
    assert "warnings" not in json.loads(capsys.readouterr().out)


def test_check_factory_reports_routing_warnings(routing_factory, capsys, monkeypatch):
    import scorecard.routing.config as rc
    real = rc.load_routing
    monkeypatch.setattr(rc, "load_routing", lambda f: (real(f)[0], ["T-99 is a shadow rule with no effect"]))
    assert main(["check-factory", "--factory", str(routing_factory)]) == 0
    assert json.loads(capsys.readouterr().out)["warnings"] == ["T-99 is a shadow rule with no effect"]
