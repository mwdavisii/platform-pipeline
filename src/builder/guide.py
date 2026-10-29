"""builder guide: the spec fields and rules of this builder version, as JSON for a coding agent.

Built from the schema and from the constants spec.py, generate.py and verify.py enforce, so it can't drift from them.
tests/builder/test_guide.py fails if a validated field, an allowed value or a reserved env name is missing here.
"""

import json
from importlib import resources

from . import VERSION
from .config import load_platform, platform_sha256
from .errors import EXIT_DRIFT, EXIT_INVALID, EXIT_NEEDS_INPUT, EXIT_OK
from .generate import BUILDER_DIRS
from .lock import NEVER_EDIT, SPEC_PATH
from .render.agents import BEGIN, END, MANAGED_FILES
from .spec import RESERVED_ENV, RESERVED_ENV_PREFIXES, STATIC_PORT

USER, AGENT = "user", "agent"
# Who decides each field, and the rules spec.py checks beyond the schema. Keys are builder-spec/v1 paths ("egress[].host"
# is the host of each egress entry). `user` fields are always the user's call; `agent` fields are ones detect infers.
FIELDS = {
    "app_name": (AGENT, []),
    "app_type": (AGENT, []),
    "entrypoint": (AGENT, [
        "python: module:attr (needs server) or a .py file (no server, no authz)",
        "node: the JS file that starts the server",
        "static: the directory to serve, or the build output directory when package.json has a build script",
        "must be inside the app directory",
    ]),
    "server": (AGENT, ["python module:attr entrypoints only"]),
    "port": (AGENT, [f"static sites must use {STATIC_PORT}", "node apps must listen on process.env.PORT"]),
    "health_path": (AGENT, ["without it the health probe is TCP only"]),
    "data_classification": (USER, ["ask the user; never infer it"]),
    "egress": (USER, ["every external host the app calls, with the reason"]),
    "egress[].host": (USER, ["model providers are rejected: model calls go to a model_gateway_hosts entry", "cloud metadata hosts are rejected"]),
    "egress[].port": (USER, []),
    "egress[].reason": (USER, []),
    "env": (AGENT, ["each name once", "reserved names and prefixes are rejected: see reserved_env"]),
    "env[].name": (AGENT, ["not a reserved name or prefix"]),
    "env[].secret": (AGENT, ["true: the value comes from the <app_name>-secrets Secret; false: from the <app_name>-config ConfigMap"]),
    "resources_tier": (AGENT, []),
    "authz": (USER, ["needed when a python or node app has write endpoints", "not for static sites", "python apps need a module:attr entrypoint"]),
    "authz.roles": (USER, ["role names are unique"]),
    "authz.roles[].name": (USER, []),
    "authz.roles[].access": (USER, ["write methods go only to write roles, unless the rule has a justification"]),
    "authz.default": (AGENT, ["always deny"]),
    "authz.rules": (USER, []),
    "authz.rules[].methods": (USER, []),
    "authz.rules[].path": (USER, ["* matches one segment, ** any depth, {id} one segment"]),
    "authz.rules[].roles": (USER, ["every role named must be declared in authz.roles"]),
    "authz.rules[].justification": (USER, ["required to grant a write method to a read role"]),
}
SCALAR_KEYS = ("enum", "const", "pattern", "minimum", "maximum", "minLength", "maxLength")
ARRAY_KEYS = ("minItems", "uniqueItems")
EXIT_CODES = {
    "detect": {EXIT_OK: "done; read questions (detect exits 0 even when it has some)",
               EXIT_INVALID: "the path is not a directory, or the command is malformed"},
    "generate": {EXIT_OK: "files written; commit them with the spec",
                 EXIT_INVALID: "the spec breaks a platform rule, or a file is in the way; read errors",
                 EXIT_NEEDS_INPUT: "a decision or a command is needed; read questions"},
    "verify": {EXIT_OK: "clean",
               EXIT_INVALID: "the path is not a directory, or the command is malformed",
               EXIT_DRIFT: "drift; apply each finding's fix.steps"},
    "guide": {EXIT_OK: "the guide is on stdout",
              EXIT_INVALID: "the platform config can't be read, or the command is malformed; read errors"},
}


def _spec_schema() -> dict:
    return json.loads((resources.files("contracts") / "schemas" / "builder-spec.v1.json").read_text(encoding="utf-8"))


def schema_fields() -> dict[str, dict]:
    """Every field path in builder-spec/v1 with its type, whether it is required, and its allowed values and bounds."""
    found = {}

    def walk(node, prefix):
        required = set(node.get("required", []))
        for name, prop in node.get("properties", {}).items():
            path = prefix + name
            found[path] = _describe(prop, name in required)
            if "properties" in prop:
                walk(prop, path + ".")
            elif "properties" in prop.get("items", {}):
                walk(prop["items"], path + "[].")

    walk(_spec_schema(), "")
    return found


def _describe(prop: dict, required: bool) -> dict:
    out = {"type": prop.get("type", "string"), "required": required}
    out.update({key: prop[key] for key in SCALAR_KEYS + ARRAY_KEYS if key in prop})
    items = prop.get("items")
    if items is not None and "properties" not in items:
        out["items"] = {"type": items.get("type", "string"), **{key: items[key] for key in SCALAR_KEYS if key in items}}
    return out


def _rules(gateways: list[str]) -> list[dict]:
    managed = " and ".join(MANAGED_FILES)
    links = ", ".join([SPEC_PATH, "--input", *MANAGED_FILES, *BUILDER_DIRS])
    return [
        {"id": "never-edit", "text": f"Never edit {', '.join(NEVER_EDIT)}, or the text between the golden-road markers in {managed}. "
                                     f"Change {SPEC_PATH} and run: builder generate ."},
        {"id": "gateway-only", "text": f"Call models only through the platform gateway ({', '.join(gateways)}). "
                                       "Egress to a model provider or a cloud metadata host is rejected."},
        {"id": "ask-the-user", "text": "Ask the user every question detect or generate returns, and about every field decided_by user. Never answer them yourself."},
        {"id": "never-public", "text": "Never make the repository public."},
        {"id": "no-links", "text": f"The builder refuses links: {links} must be real files and directories."},
        {"id": "regenerate-after-upgrade", "text": "After a builder upgrade, run builder generate . again and commit the result, or verify reports drift."},
        {"id": "locked-dependencies", "text": "Python apps need requirements.txt with hashes (--hash lines) and the server pinned (==); use gunicorn 22 or newer. "
                                              "Node apps need package.json and package-lock.json."},
        {"id": "single-line", "text": "Text fields in the spec are single lines."},
        {"id": "high-findings-hold-the-merge", "text": "A high or critical scorecard finding holds the merge (the `scorecard / findings` check). "
                                                      "Fix it and push again. Only a person can merge past it; never try to."},
    ]


def guide() -> dict:
    gateways = list(load_platform()["model_gateway_hosts"])
    fields = []
    for path, described in schema_fields().items():
        decided_by, rules = FIELDS[path]
        fields.append({"field": path, **described, "decided_by": decided_by, "rules": list(rules)})
    return {
        "builder_version": VERSION,
        "platform_config_sha256": platform_sha256(),
        "model_gateway_hosts": gateways,
        "fields": fields,
        "reserved_env": {"names": dict(RESERVED_ENV), "prefixes": {f"{prefix}*": why for prefix, why in RESERVED_ENV_PREFIXES.items()}},
        "never_edit": list(NEVER_EDIT),
        "managed_blocks": {"files": list(MANAGED_FILES), "begin": BEGIN, "end": END},
        "rules": _rules(gateways),
        "exit_codes": {command: {str(code): text for code, text in codes.items()} for command, codes in EXIT_CODES.items()},
    }
