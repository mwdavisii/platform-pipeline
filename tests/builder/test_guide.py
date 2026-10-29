"""builder guide: its shape, and that it can't fall behind the schema and the validators it describes."""

import ast
import json
from importlib import resources
from pathlib import Path

import pytest

from builder import VERSION
from builder.config import load_base_images, load_platform, platform_sha256
from builder.errors import EXIT_DRIFT, EXIT_INVALID, EXIT_NEEDS_INPUT, EXIT_OK
from builder.generate import BUILDER_DIRS, render_all
from builder.guide import FIELDS, guide
from builder.lock import NEVER_EDIT, SPEC_PATH
from builder.render.agents import BEGIN, END, MANAGED_FILES
from builder.spec import RESERVED_ENV, RESERVED_ENV_PREFIXES
from contracts.schema import validate

HERE = Path(__file__).parent
SPEC_PY = HERE.parents[1] / "src/builder/spec.py"
# Keys spec.py reads that aren't spec fields: route results from find_routes, the platform config, package.json.
NOT_SPEC_KEYS = {"method", "model_gateway_hosts", "scripts"}
# Fields spec.py checks beyond the schema. The guide must say how.
CHECKED_BEYOND_SCHEMA = {"entrypoint", "server", "port", "egress[].host", "env", "env[].name", "authz", "authz.roles",
                         "authz.roles[].access", "authz.rules[].roles", "authz.rules[].justification"}
USER_FIELDS = {"data_classification", "egress", "egress[].host", "egress[].port", "egress[].reason", "authz", "authz.roles",
               "authz.roles[].name", "authz.roles[].access", "authz.rules", "authz.rules[].methods", "authz.rules[].path",
               "authz.rules[].roles", "authz.rules[].justification"}
BOUNDS = ("pattern", "minimum", "maximum", "minLength", "maxLength", "minItems", "uniqueItems")


def schema_paths(node=None, prefix=""):
    """{path: (property, required)} for every property in builder-spec/v1, walked here independently of the guide."""
    if node is None:
        node = json.loads((resources.files("contracts") / "schemas" / "builder-spec.v1.json").read_text(encoding="utf-8"))
    found = {}
    for name, prop in node.get("properties", {}).items():
        found[prefix + name] = (prop, name in node.get("required", []))
        if "properties" in prop:
            found.update(schema_paths(prop, f"{prefix}{name}."))
        if "properties" in prop.get("items", {}):
            found.update(schema_paths(prop["items"], f"{prefix}{name}[]."))
    return found


@pytest.fixture
def by_field():
    return {entry["field"]: entry for entry in guide()["fields"]}


def test_guide_is_valid_and_names_this_builder():
    doc = guide()
    assert validate("builder-guide.v1", doc) == []
    assert doc["builder_version"] == VERSION and doc["platform_config_sha256"] == platform_sha256()
    assert doc["model_gateway_hosts"] == load_platform()["model_gateway_hosts"]


def test_every_schema_field_is_in_the_guide_and_nothing_else(by_field):
    assert set(by_field) == set(schema_paths()) == set(FIELDS)


def test_types_required_and_bounds_match_the_schema(by_field):
    for path, (prop, required) in schema_paths().items():
        entry = by_field[path]
        assert entry["required"] is required, path
        assert entry["type"] == prop.get("type", "string"), path
        for key in BOUNDS:
            assert entry.get(key) == prop.get(key), (path, key)
        items = prop.get("items", {})
        if items and "properties" not in items:
            for key in BOUNDS:
                assert entry["items"].get(key) == items.get(key), (path, "items", key)


def test_every_allowed_value_is_in_the_guide(by_field):
    for path, (prop, _) in schema_paths().items():
        values = prop.get("enum", []) + ([prop["const"]] if "const" in prop else []) + prop.get("items", {}).get("enum", [])
        entry = by_field[path]
        listed = entry.get("enum", []) + ([entry["const"]] if "const" in entry else []) + entry.get("items", {}).get("enum", [])
        assert values == listed, path


def test_every_reserved_env_name_and_prefix_is_in_the_guide():
    reserved = guide()["reserved_env"]
    assert reserved["names"] == RESERVED_ENV
    assert reserved["prefixes"] == {f"{prefix}*": why for prefix, why in RESERVED_ENV_PREFIXES.items()}


def validator_keys():
    """Every string key spec.py reads with x["key"] or x.get("key")."""
    keys = set()
    for node in ast.walk(ast.parse(SPEC_PY.read_text())):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            keys.add(node.slice.value)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
              and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            keys.add(node.args[0].value)
    return keys - NOT_SPEC_KEYS


def test_every_field_a_validator_reads_is_in_the_guide(by_field):
    last_segments = {path.rsplit(".", 1)[-1].removesuffix("[]") for path in by_field}
    assert validator_keys() and validator_keys() <= last_segments
    for path in CHECKED_BEYOND_SCHEMA:
        assert by_field[path]["rules"], path


def test_decided_by(by_field):
    assert {path for path, entry in by_field.items() if entry["decided_by"] == "user"} == USER_FIELDS


@pytest.mark.parametrize("name", ["python-fastapi", "node-express", "static-site"])
def test_never_edit_covers_every_generated_file(name, monkeypatch):
    monkeypatch.delenv("BUILDER_PLATFORM_CONFIG", raising=False)
    root = HERE / "fixtures" / name
    files = render_all(json.loads((root / "builder-spec.json").read_text()), root, load_platform(), load_base_images())
    never_edit = guide()["never_edit"]
    for rel in files:
        assert rel in never_edit or any(d.endswith("/") and rel.startswith(d) for d in never_edit), rel


def test_rules_name_what_generate_and_verify_enforce():
    doc = guide()
    rules = {rule["id"]: rule["text"] for rule in doc["rules"]}
    assert set(rules) == {"never-edit", "gateway-only", "ask-the-user", "never-public", "no-links", "regenerate-after-upgrade",
                          "locked-dependencies", "single-line", "high-findings-hold-the-merge"}
    assert all(rel in rules["never-edit"] for rel in NEVER_EDIT) and all(name in rules["never-edit"] for name in MANAGED_FILES)
    assert all(host in rules["gateway-only"] for host in doc["model_gateway_hosts"])
    assert all(rel in rules["no-links"] for rel in (SPEC_PATH, "--input", *MANAGED_FILES, *BUILDER_DIRS))
    assert doc["never_edit"] == list(NEVER_EDIT)
    assert doc["managed_blocks"] == {"files": list(MANAGED_FILES), "begin": BEGIN, "end": END}


def test_exit_codes():
    codes = {command: set(texts) for command, texts in guide()["exit_codes"].items()}
    assert codes == {"detect": {str(EXIT_OK), str(EXIT_INVALID)},
                     "generate": {str(EXIT_OK), str(EXIT_INVALID), str(EXIT_NEEDS_INPUT)},
                     "verify": {str(EXIT_OK), str(EXIT_INVALID), str(EXIT_DRIFT)},
                     "guide": {str(EXIT_OK), str(EXIT_INVALID)}}


def test_locked_dependencies_rule_names_the_pin_and_gunicorn_questions():
    text = {rule["id"]: rule["text"] for rule in guide()["rules"]}["locked-dependencies"]
    assert "pinned" in text and "gunicorn 22" in text and "hashes" in text


def test_guide_follows_the_platform_config(tmp_path, monkeypatch):
    platform = {**load_platform(), "model_gateway_hosts": ["llm.example.org"]}
    path = tmp_path / "platform.json"
    path.write_text(json.dumps(platform))
    monkeypatch.setenv("BUILDER_PLATFORM_CONFIG", str(path))
    doc = guide()
    assert doc["model_gateway_hosts"] == ["llm.example.org"] and doc["platform_config_sha256"] == platform_sha256()
    assert "llm.example.org" in {rule["id"]: rule["text"] for rule in doc["rules"]}["gateway-only"]
