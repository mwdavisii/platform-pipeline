"""Load the platform JSON Schemas and validate documents against them."""

import json
from functools import cache
from importlib import resources

from jsonschema import Draft202012Validator


@cache
def _validator(name: str) -> Draft202012Validator:
    text = (resources.files("contracts") / "schemas" / f"{name}.json").read_text(encoding="utf-8")
    schema = json.loads(text)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def validate(name: str, document) -> list[str]:
    """Return human-readable errors; an empty list means the document is valid."""
    errors = sorted(_validator(name).iter_errors(document), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors]
