"""`builder-bumped`, recorded only: the app's .builder.lock names a different builder (and so new base images) than the base.

A platform builder release causes it and the platform vets it, so it raises no trigger. Only a well-formed X.Y.Z on both
sides is recorded, so the summary is code-checked repo data.
"""

import json
import re

from builder.lock import LOCK_PATH as LOCK

_VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")


def _version(text: str | None) -> str | None:
    if text is None:
        return None
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return None
    value = doc.get("builder_version") if isinstance(doc, dict) else None
    return value if isinstance(value, str) and _VERSION.fullmatch(value) else None


def builder_bumped(base_text: str | None, head_text: str | None) -> list[dict]:
    before, after = _version(base_text), _version(head_text)
    if before is None or after is None or before == after:
        return []
    return [{"id": "builder-bumped", "kind": "recorded", "summary": f"builder {before} -> {after}"}]
