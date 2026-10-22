from scorecard.asvs import for_level, load_asvs
from scorecard.scoping import scope


def _scope(config, **kw):
    args = dict(config=config, confirmed_na=[], packages=[], has_authz=True, drift_green=True)
    args.update(kw)
    return scope(for_level(load_asvs(), 2), **args)


def _by_id(results):
    return {r["asvs_id"]: r for r in results}


def test_platform_provided_passes_only_with_green_drift(config):
    decided, agent = _scope(config)
    result = _by_id(decided)["v5.0.0-8.2.1"]
    assert result["status"] == "pass" and result["status_source"] == "platform" and result["control"]
    decided, _ = _scope(config, drift_green=False)
    assert _by_id(decided)["v5.0.0-8.2.1"]["status"] == "needs_human"


def test_authz_controls_need_an_authz_block(config):
    decided, agent = _scope(config, has_authz=False)
    assert "v5.0.0-8.2.1" not in _by_id(decided)
    assert "v5.0.0-8.2.1" in {r["id"] for r in agent}
    assert _by_id(decided)["v5.0.0-12.2.1"]["status"] == "pass"


def test_tool_covered_is_never_sent_to_the_agent(config):
    decided, agent = _scope(config)
    result = _by_id(decided)["v5.0.0-1.2.4"]
    assert result["status"] == "needs_human" and result["status_source"] == "scanner" and result["covered_by"] == "sast"
    assert "v5.0.0-1.2.4" not in {r["id"] for r in agent}


def test_chapter_na_rule_applies_only_when_the_subject_is_absent(config):
    decided, _ = _scope(config)
    assert _by_id(decided)["v5.0.0-10.4.1"]["status"] == "na"
    assert _by_id(decided)["v5.0.0-10.4.1"]["status_source"] == "human_na"
    oauth = [{"ecosystem": "pypi", "name": "Authlib", "version": "1.3.0"}]
    decided, agent = _scope(config, packages=oauth)
    assert "v5.0.0-10.4.1" not in _by_id(decided)
    assert "v5.0.0-10.4.1" in {r["id"] for r in agent}


def test_confirmed_na_from_the_factory(config):
    entry = {"asvs_id": "v5.0.0-6.6.1", "justification": "No out-of-band authentication.", "confirmed_by": "lead", "confirmed": "2026-10-01"}
    decided, agent = _scope(config, confirmed_na=[entry])
    result = _by_id(decided)["v5.0.0-6.6.1"]
    assert result["status"] == "na" and "confirmed by lead" in result["justification"]


def test_everything_else_goes_to_the_agent_once(config):
    decided, agent = _scope(config)
    ids = [r["asvs_id"] for r in decided] + [r["id"] for r in agent]
    assert sorted(ids) == sorted(r["id"] for r in for_level(load_asvs(), 2))
    assert "v5.0.0-8.2.2" in {r["id"] for r in agent}


def test_lockfile_problems_disable_the_package_absence_rules(config):
    from scorecard.lockfiles import read_packages

    packages, problems = read_packages({"requirements.txt": "authlib===1.4\n"}.get)
    assert packages == [] and problems
    decided, agent = _scope(config, packages=packages, lockfile_problems=problems)
    assert "v5.0.0-10.4.1" not in _by_id(decided) and "v5.0.0-10.4.1" in {r["id"] for r in agent}
    assert "v5.0.0-17.1.1" in {r["id"] for r in agent}
    decided, _ = _scope(config, packages=packages, lockfile_problems=[])
    assert _by_id(decided)["v5.0.0-10.4.1"]["status"] == "na"
