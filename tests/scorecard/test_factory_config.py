import pytest
import yaml

from golden_road.io import InputError
from scorecard.config import load_config, load_confirmed_na, load_intake, repo_parts
from scorecard.levels import asvs_level, effective_classification, floor, highest


def _edit_config(factory, **changes):
    path = factory / "config" / "scorecard.yaml"
    doc = yaml.safe_load(path.read_text())
    doc.update(changes)
    path.write_text(yaml.safe_dump(doc))


def test_sample_config_loads(config):
    assert config["blocking"] == ["v5.0.0-6", "v5.0.0-7", "v5.0.0-8", "v5.0.0-9", "v5.0.0-13.3"]
    assert config["triggers"]["T-9"] == "elevated"
    assert config["gate1_checks"][0] == {"name": "golden-road / drift", "app_id": 15368}


@pytest.mark.parametrize(
    "changes",
    [
        {"blocking": ["V8"]},
        {"coverage_threshold": 170},
        {"triggers": {"T-1": "security_review"}},
        {"triggers": {**{f"T-{n}": "elevated" for n in (1,2,3,4,5,6,7,8,9,10,11,12,14,15,16,17,18,19)}, "T-9": "standard"}},  # a trigger that fires can't leave the level at standard
        {"gate1_checks": [{"name": "drift"}]},
        {"patterns": {"skip_markers": ["("], "suppression_markers": [], "injection": [], "platform_import": [], "test_function": []}},
        {"unknown_key": 1},
        {"evidence": {}},
        {"evidence": {"excluded_paths": "docs/*"}},
        {"triggers": {**{f"T-{n}": "elevated" for n in range(1, 20)}}},  # T-13 is reserved
        {"drift_check": "golden-road / not-a-gate1-check"},
    ],
)
def test_bad_config_is_an_input_error(factory, changes):
    _edit_config(factory, **changes)
    with pytest.raises(InputError):
        load_config(factory)


def test_missing_config_is_an_input_error(factory):
    (factory / "config" / "scorecard.yaml").unlink()
    with pytest.raises(InputError):
        load_config(factory)


def test_intake_and_dates_stay_strings(factory):
    intake = load_intake(factory, "acme/notes")
    assert intake["tier"] == "moderate"
    assert load_confirmed_na(factory, "acme/notes")[0]["confirmed"] == "2026-10-01"


def test_missing_intake_and_na_are_empty(factory):
    assert load_intake(factory, "acme/unknown") is None
    assert load_confirmed_na(factory, "acme/unknown") == []


def test_intake_for_another_repo_is_refused(factory):
    path = factory / "intake" / "acme" / "notes.yaml"
    path.write_text(path.read_text().replace("repo: acme/notes", "repo: acme/other"))
    with pytest.raises(InputError, match="not acme/notes"):
        load_intake(factory, "acme/notes")


@pytest.mark.parametrize("repo", ["acme", "acme/../x", "../acme", "acme/..", "a/b/c", "acme/no tes"])
def test_repo_names_cannot_leave_the_factory(repo):
    with pytest.raises(InputError):
        repo_parts(repo)


def test_levels_and_classifications(config):
    notes = {"tier": "moderate", "data_classification": "internal"}
    assert highest([]) == "standard"
    assert highest(["elevated", "standard"]) == "elevated"
    assert effective_classification("internal", None, "phi") == "phi"
    assert asvs_level(config, notes, None) == 2
    assert asvs_level(config, {"tier": "low", "data_classification": "public"}, "restricted") == 2
    assert asvs_level(config, None, "public") == 3
    assert floor(config, {"tier": "high", "data_classification": "public"}) == "security_review"
    assert floor(config, None) == "standard"


def test_duplicate_yaml_keys_are_refused(factory):
    path = factory / "intake" / "acme" / "notes.yaml"
    path.write_text(path.read_text() + "tier: low\n")
    with pytest.raises(InputError, match="duplicate key 'tier'"):
        load_intake(factory, "acme/notes")
    config = factory / "config" / "scorecard.yaml"
    config.write_text(config.read_text().replace("coverage_threshold: 70", "coverage_threshold: 70\ncoverage_threshold: 0"))
    with pytest.raises(InputError, match="duplicate key 'coverage_threshold'"):
        load_config(factory)


def test_duplicate_keys_in_a_nested_mapping_are_refused(factory):
    path = factory / "na" / "acme" / "notes.yaml"
    path.write_text(path.read_text() + "    confirmed_by: \"someone-else\"\n")
    with pytest.raises(InputError, match="duplicate key 'confirmed_by'"):
        load_confirmed_na(factory, "acme/notes")


def test_symlinked_records_are_refused(factory, tmp_path):
    target = tmp_path / "elsewhere.yaml"
    path = factory / "intake" / "acme" / "notes.yaml"
    target.write_text(path.read_text())
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(InputError, match="symlink"):
        load_intake(factory, "acme/notes")
    config = factory / "config" / "scorecard.yaml"
    target.write_text(config.read_text())
    config.unlink()
    config.symlink_to(target)
    with pytest.raises(InputError, match="symlink"):
        load_config(factory)


def test_a_dangling_symlink_is_an_error_not_a_missing_record(factory, tmp_path):
    path = factory / "na" / "acme" / "notes.yaml"
    path.unlink()
    path.symlink_to(tmp_path / "nowhere.yaml")
    with pytest.raises(InputError):
        load_confirmed_na(factory, "acme/notes")


def test_unreadable_files_are_input_errors(factory):
    import os

    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    path = factory / "intake" / "acme" / "notes.yaml"
    path.chmod(0)
    try:
        with pytest.raises(InputError):
            load_intake(factory, "acme/notes")
    finally:
        path.chmod(0o644)


@pytest.mark.parametrize(
    "old, new",
    [("tier: moderate", "tier: critical"), ("owner:", "extra: 1\nowner:"), ('purpose: "Stores short team notes. It does not send email, call external APIs, or store files."', 'purpose: "short"')],
)
def test_bad_intake_is_an_input_error(factory, old, new):
    path = factory / "intake" / "acme" / "notes.yaml"
    assert old in path.read_text()
    path.write_text(path.read_text().replace(old, new))
    with pytest.raises(InputError):
        load_intake(factory, "acme/notes")


def test_bad_na_is_an_input_error(factory):
    path = factory / "na" / "acme" / "notes.yaml"
    path.write_text(path.read_text().replace("v5.0.0-6.6.1", "V6.6.1"))
    with pytest.raises(InputError):
        load_confirmed_na(factory, "acme/notes")


def test_na_for_another_repo_is_refused(factory):
    path = factory / "na" / "acme" / "notes.yaml"
    path.write_text(path.read_text().replace("repo: acme/notes", "repo: acme/other"))
    with pytest.raises(InputError, match="not acme/notes"):
        load_confirmed_na(factory, "acme/notes")


def _agent(factory, **changes):
    path = factory / "config" / "scorecard.yaml"
    doc = yaml.safe_load(path.read_text())
    for dotted, value in changes.items():
        *parents, last = dotted.split(".")
        node = doc["agent"]
        for key in parents:
            node = node[key]
        node[last] = value
    path.write_text(yaml.safe_dump(doc))


def test_a_factory_config_that_still_names_the_model_is_refused_without_echoing_it(factory):
    _agent(factory, model={"name": "sentinel-old-name", "version": "pinned-1"})
    with pytest.raises(InputError, match="'name' was unexpected") as caught:
        load_config(factory)
    assert "sentinel-old-name" not in str(caught.value)


def test_a_model_placeholder_in_the_path_is_accepted(factory):
    url = "https://gateway.example.invalid/openai/deployments/{model}/chat/completions?api-version=2024-10-21"
    _agent(factory, **{"gateway.url": url})
    assert load_config(factory)["agent"]["gateway"]["url"] == url


def test_a_misplaced_placeholder_message_names_the_rule(factory):
    _agent(factory, **{"gateway.url": "https://gateway.example.invalid/v1/chat/completions?deployment={model}"})
    with pytest.raises(InputError, match=r"\{model\} only in its path"):
        load_config(factory)


def test_model_temperature_defaults_to_zero_and_may_be_null_or_a_number(factory):
    from scorecard.config import model_temperature
    assert model_temperature(load_config(factory)) == 0
    for value in (None, 0, 1.5, 2):
        _agent(factory, **{"model.temperature": value})
        assert model_temperature(load_config(factory)) == value


def test_a_nan_temperature_is_refused(factory):
    _agent(factory, **{"model.temperature": float("nan")})
    with pytest.raises(InputError, match="agent.model.temperature must be a finite number"):
        load_config(factory)


def test_an_empty_temperature_means_null(factory):
    path = factory / "config" / "scorecard.yaml"
    path.write_text(path.read_text().replace("model: {version: pinned-1}", "model: {version: pinned-1, temperature: }"))
    assert load_config(factory)["agent"]["model"]["temperature"] is None


def test_sample_agent_config(config):
    agent = config["agent"]
    assert agent["prompt_version"] == "review-1" and agent["model"] == {"version": "pinned-1"}
    assert agent["gateway"]["auth"] == {"scheme": "bearer"} and agent["gateway"]["credential_env"] == "SCORECARD_GATEWAY_TOKEN"


@pytest.mark.parametrize("changes", [
    {"prompt_version": "review-0"},
    {"prompt_version": "Review 1"},
    {"gateway.url": "http://gateway.example.invalid/v1/chat/completions"},
    {"gateway.url": "https://user@gateway.example.invalid/v1/chat/completions"},
    {"gateway.url": "https://gateway.example.invalid"},
    {"gateway.url": "https://gateway.example.invalid/openai/deployments/{model}/{model}/chat/completions"},
    {"gateway.url": "https://gateway.example.invalid/v1/chat/completions?deployment={model}"},
    {"gateway.url": "https://gateway.example.invalid/openai/deployments/{deployment}/chat/completions"},
    {"gateway.url": "https://gateway.example.invalid/openai/deployments/{model/chat/completions"},
    {"gateway.url": "https://gateway.example.invalid/openai/deployments/model}/chat/completions"},
    {"gateway.credential_env": "GITHUB_TOKEN"},
    {"gateway.credential_env": "ACTIONS_ID_TOKEN_REQUEST_TOKEN"},
    {"gateway.credential_env": "lower_case"},
    {"gateway.credential_env": "GITHUB_TOKEN\n"},
    {"gateway.credential_env": "GITHUB_PAT"},
    {"gateway.credential_env": "ACTIONS_STUFF"},
    {"gateway.credential_env": "RUNNER_TEMP"},
    {"gateway.auth": {"scheme": "header", "header": "api-key\n"}},
    {"model.version": "pinned\n"},
    {"model": {"name": "scorecard-reviewer", "version": "pinned-1"}},
    {"prompt_version": "review-1\n"},
    {"gateway.auth": {"scheme": "header"}},
    {"gateway.auth": {"scheme": "header", "header": "Host"}},
    {"gateway.auth": {"scheme": "header", "header": "CF-Access-Client-Id"}},
    {"gateway.auth": {"scheme": "header", "header": "cf-access-client-secret"}},
    {"gateway.auth": {"scheme": "basic"}},
    {"gateway.auth": {"scheme": "header", "header": "User-Agent"}},
    {"budget.max_total_tokens": 999},
    {"budget.max_total_tokens": -1},
    {"budget.max_run_seconds": -1},
    {"budget.max_run_seconds": 14101},
    {"budget.max_run_seconds": 0.5},
    {"batching.max_turns": 1},
    {"tools.max_read_lines": 100000},
    {"model.extra": "x"},
    {"model.temperature": 3},
    {"model.temperature": -0.1},
    {"model.temperature": "0"},
    {"model.temperature": True},
])
def test_bad_agent_config_is_an_input_error(factory, changes):
    _agent(factory, **changes)
    with pytest.raises(InputError):
        load_config(factory)


@pytest.mark.parametrize("changes", [{"budget.max_total_tokens": 0}, {"budget.max_run_seconds": 0}, {"budget.max_total_tokens": 0, "budget.max_run_seconds": 0},
                                     {"budget.max_total_tokens": 1000, "budget.max_run_seconds": 14100}])
def test_zero_budget_means_no_limit_and_is_accepted(factory, changes):
    _agent(factory, **changes)
    cfg = load_config(factory)
    for key, value in changes.items():
        assert cfg["agent"]["budget"][key.split(".")[1]] == value


def test_an_api_key_header_is_accepted(factory):
    _agent(factory, **{"gateway.auth": {"scheme": "header", "header": "api-key"},
                       "gateway.url": "https://gateway.example.invalid/openai/deployments/reviewer/chat/completions?api-version=2024-10-21"})
    assert load_config(factory)["agent"]["gateway"]["auth"]["header"] == "api-key"


def test_sample_config_has_the_re_review_settings(config):
    assert config["large_change"] == {"lines": 1000, "files": 40}
    assert {config["triggers"][t] for t in ("T-17", "T-18", "T-19")} == {"elevated"}


@pytest.mark.parametrize("edit", [
    lambda d: d.pop("large_change"),
    lambda d: d["large_change"].pop("files"),
    lambda d: d["large_change"].update(lines=0),
    lambda d: d["large_change"].update(files=0),
    lambda d: d["large_change"].update(lines="1000"),
    lambda d: d["large_change"].update(extra=1),
    lambda d: d["triggers"].pop("T-17"),
    lambda d: d["triggers"].pop("T-18"),
    lambda d: d["triggers"].pop("T-19"),
    lambda d: d["triggers"].update({"T-20": "elevated"}),
])
def test_the_re_review_settings_are_required_and_checked(factory, edit):
    path = factory / "config" / "scorecard.yaml"
    doc = yaml.safe_load(path.read_text())
    edit(doc)
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(InputError):
        load_config(factory)
