"""config/routing.toml: the schema, the code checks, and how it merges with scorecard.yaml's mechanics."""

import re

import pytest

from golden_road.io import InputError
from scorecard.config import engine, load_config
from scorecard.routing.config import agent_facts, fact_sha256, load_routing, questions_sha256


def _edit(factory, old: str, new: str, *, count: int = 1) -> None:
    path = factory / "config" / "routing.toml"
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new, count))


@pytest.mark.xfail(reason="review-2 is released in Task 5", strict=True)
def test_the_starting_routing_factory_loads(routing_config):
    assert engine(routing_config) == "routing"
    assert routing_config["tier_floor"] == {"low": "standard", "moderate": "standard", "high": "security_review"}
    assert routing_config["registry"]["timeout_seconds"] == 10 and routing_config["registry"]["new_package_days"] == 90
    assert routing_config["patterns"]["injection"] and routing_config["paths"]["workflow_files"]
    ids = [r["id"] for r in routing_config["routing"]["rules"]]
    assert ids[:3] == ["T-2", "T-3", "T-4"] and "T-22" in ids


def test_the_asvs_factory_is_unchanged(config):
    assert engine(config) == "asvs" and "routing" not in config and "triggers" in config


def test_every_fact_is_used(routing_factory):
    _, warnings = load_routing(routing_factory)
    assert warnings == []


def test_an_unused_fact_is_a_warning(routing_factory):
    _edit(routing_factory, "[facts.tier_raised]", '[facts.spare]\nfrom = "spec"\nmeasure = "role_count"\n\n[facts.tier_raised]')
    _, warnings = load_routing(routing_factory)
    assert warnings == ["fact spare is not used by any rule"]


@pytest.mark.parametrize("old,new,message", [
    ('id = "T-3"', 'id = "T-2"', "duplicate rule id T-2"),
    ('{ fact = "tier_raised", is = true }', '{ fact = "nope", is = true }', "names a missing fact nope"),
    ('{ fact = "tier_raised", is = true }', '{ fact = "tier_raised", over = 2 }', "over does not fit tier_raised"),
    ('{ fact = "read_role_write_grants", changed = "added" }', '{ fact = "read_role_write_grants", changed = "up" }', "changed up does not fit read_role_write_grants"),
    ('id = "T-4"\nwhen = { fact = "control_removed", is = true }\nlevel = "security_review"\non_unsure = "elevated"\n',
     'id = "T-4"\nwhen = { fact = "control_removed", is = true }\nlevel = "security_review"\n', "T-4 needs on_unsure"),
    ('id = "T-2"\nwhen = { fact = "spec_security_change", is = true }\nlevel = "security_review"\n',
     'id = "T-2"\nwhen = { fact = "spec_security_change", is = true }\nlevel = "security_review"\non_unsure = "elevated"\n', "T-2 can't have on_unsure"),
    ('summary = "Resource tier raised"', 'summary = "Resource tier raised to {head}"', "placeholder {head}"),
    ('summary = "Resource tier raised"', 'summary = "Resource tier raised {"', "placeholder"),
    ('injection = [\n', "injection = [\n  '(unclosed',\n", "invalid regex"),
])
def test_bad_routing_is_refused(routing_factory, old, new, message):
    _edit(routing_factory, old, new)
    with pytest.raises(InputError, match=re.escape(message)):
        load_config(routing_factory)


def test_an_unknown_key_is_refused(routing_factory):
    _edit(routing_factory, 'schema = "routing/v1"', 'schema = "routing/v1"\nsurprise = 1')
    with pytest.raises(InputError, match="not a valid routing/v1"):
        load_config(routing_factory)


def test_an_any_value_outside_the_enum_is_refused(routing_factory):
    _edit(routing_factory, '[facts.tier_raised]', '[facts.classification]\nfrom = "spec"\nmeasure = "data_classification"\n\n[facts.tier_raised]')
    _edit(routing_factory, '{ fact = "tier_raised", is = true }', '[{ fact = "tier_raised", is = true }, { fact = "classification", any = ["secret"] }]')
    with pytest.raises(InputError, match="secret is not a data_classification value"):
        load_config(routing_factory)


def test_review_2_needs_routing_and_review_1_refuses_it(factory, routing_factory):
    (routing_factory / "config" / "routing.toml").unlink()
    with pytest.raises(InputError):
        load_config(routing_factory)  # scorecard-config/v2 without routing.toml is not a v1 config
    path = factory / "config" / "scorecard.yaml"
    path.write_text(path.read_text().replace("prompt_version: review-1", "prompt_version: review-2"))
    with pytest.raises(InputError, match="needs config/routing.toml"):
        load_config(factory)


def test_the_question_hash_follows_the_questions(routing_factory):
    doc, _ = load_routing(routing_factory)
    before = questions_sha256(doc)
    names = [n for n, _ in agent_facts(doc)]
    assert names == sorted(names) and "own_credentials" in names
    _edit(routing_factory, "instead of relying on the platform identity header", "instead of the platform identity header")
    after, _ = load_routing(routing_factory)
    assert questions_sha256(after) != before
    assert fact_sha256(after["facts"]["personal_data"]) == fact_sha256(doc["facts"]["personal_data"])


def test_a_symlinked_routing_file_is_refused(routing_factory, tmp_path):
    path = routing_factory / "config" / "routing.toml"
    target = tmp_path / "elsewhere.toml"
    target.write_text(path.read_text())
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(InputError, match="symlink"):
        load_config(routing_factory)
