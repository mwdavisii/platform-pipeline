"""Load the platform JSON Schemas and validate documents against them.

Every schema in `schemas/` is registered under its `$id`, so one schema can `$ref` another
(scorecard/v1 embeds finding/v2 and requirement-result/v1) without any network lookup.
"""

import datetime as dt
import json
from functools import cache
from importlib import resources

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


def _schema_files():
    return sorted((p for p in (resources.files("contracts") / "schemas").iterdir() if p.name.endswith(".json")), key=lambda p: p.name)


@cache
def _registry() -> Registry:
    pairs, seen = [], set()
    for path in _schema_files():
        schema = json.loads(path.read_text(encoding="utf-8"))
        if not schema["$id"].endswith(f"/{path.name}"):
            raise ValueError(f"{path.name}: $id {schema['$id']} does not match the file name")
        if schema["$id"] in seen:
            raise ValueError(f"{path.name}: duplicate $id {schema['$id']}")
        seen.add(schema["$id"])
        pairs.append((schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012)))
    return Registry().with_resources(pairs)


@cache
def _validator(name: str) -> Draft202012Validator:
    text = (resources.files("contracts") / "schemas" / f"{name}.json").read_text(encoding="utf-8")
    schema = json.loads(text)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=_registry(), format_checker=Draft202012Validator.FORMAT_CHECKER)


# `format: date-time` is checked only when an optional package is installed, so these fields are parsed here as well: the
# schema's pattern fixes the shape, this proves the instant exists (no month 99, no February 30).
DATE_TIME_FIELDS = {"intake.v2": ("approved_at", "floor_override.at"), "identity-link.v1": ("created_at",), "binding.v1": ("linked_at",),
                   "binding.v2": ("linked_at",)}


def _at(document, dotted):
    for part in dotted.split("."):
        document = document.get(part) if isinstance(document, dict) else None
    return document


def _bad_instants(name: str, document) -> list[str]:
    errors = []
    for field in DATE_TIME_FIELDS.get(name, ()):
        value = _at(document, field)
        if isinstance(value, str):
            try:
                dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                errors.append(f"{field}: {value!r} is not a real UTC date and time")
    return errors


# JSON Schema can't say "unique by one key", so these are checked here: each name is a list of objects whose key must not repeat.
UNIQUE_KEYS = {"binding.v2": (("secrets", "env", "secrets: env names must be unique"),)}


def _repeats(name: str, document) -> list[str]:
    errors = []
    for field, key, message in UNIQUE_KEYS.get(name, ()):
        items = document.get(field) if isinstance(document, dict) else None
        keys = [i.get(key) for i in items if isinstance(i, dict)] if isinstance(items, list) else []
        if len(keys) != len(set(map(repr, keys))):
            errors.append(message)
    return errors


def validate(name: str, document) -> list[str]:
    """Return human-readable errors; an empty list means the document is valid."""
    errors = sorted(_validator(name).iter_errors(document), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors] + _bad_instants(name, document) + _repeats(name, document)
