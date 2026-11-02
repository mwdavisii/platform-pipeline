import pytest

from contracts.schema import validate

ACCESS = {"schema": "cloudflare-access/v1", "repo": "acme/notes", "environment": "test", "gateway": "mcp-test.example.com",
          "token": {"name": "cf-mcp-acme--notes-test", "secret_path": "/golden-road/mcp-tokens/cf-mcp-acme--notes-test"},
          "servers": ["notes"]}
SKILL_YAML = {"schema": "skill/v1", "name": "incident-triage", "description": "Triage an incident report.", "environments": ["test", "prod"],
              "audience": "everyone"}


def test_the_generated_access_file_and_a_skill_yaml_validate():
    assert validate("cloudflare-access.v1", ACCESS) == []
    assert validate("cloudflare-access.v1", {**ACCESS, "servers": []}) == []
    assert validate("skill.v1", SKILL_YAML) == []
    assert validate("skill.v1", {**SKILL_YAML, "audience": ["finance-analysts"]}) == []


@pytest.mark.parametrize("change", [{"token": {"name": "cf-mcp-acme--notes-test"}}, {"servers": ["Notes"]}, {"servers": ["a"] * 21},
                                    {"token": {**ACCESS["token"], "secret_path": "/Other/x"}}, {"environment": "dev"}, {"extra": 1},
                                    {"gateway": "mcp-test.example.com\n"}, {"servers": ["notes\n"]}, {"repo": "acme/notes\n"},
                                    {"token": {**ACCESS["token"], "name": "cf-mcp-acme--notes-test\n"}},
                                    {"token": {**ACCESS["token"], "secret_path": ACCESS["token"]["secret_path"] + "\n"}}])
def test_a_bad_access_file_is_refused(change):
    assert validate("cloudflare-access.v1", {**ACCESS, **change})


@pytest.mark.parametrize("change", [{"environments": []}, {"environments": ["dev"]}, {"environments": ["test", "test"]}, {"audience": []},
                                    {"audience": "some"}, {"audience": ["a\nb"]}, {"description": ""}, {"name": "A"}, {"secret": 1},
                                    {"name": "incident-triage\n"}, {"description": "Triage.\n"}, {"audience": ["finance-analysts\n"]}])
def test_a_bad_skill_yaml_is_refused(change):
    assert validate("skill.v1", {**SKILL_YAML, **change})
