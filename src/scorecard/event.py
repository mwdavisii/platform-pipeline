"""re-review-event/v1: one assessed run's signals, for the portal's re-review list.

Built only from code-written values: trigger IDs, levels and the level engine's summaries from the scorecard's reasons, item
counts, and the recorded-only signals (versions, tier names, host:port values from builder-spec.json). It never carries refs,
excerpts, justifications or finding text, so no model text can reach it.
"""

import re

from builder import VERSION as BUILDER_VERSION
from contracts.schema import validate
from golden_road.io import InputError


CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # re-review-event/v1 refuses these in a summary: the portal stores and shows it


def _one_line(signal: dict) -> dict:
    """A routing summary can carry a fact's value: a control character in it becomes a space, so the event stays valid."""
    return {**signal, "summary": CONTROL.sub(" ", signal["summary"])}


def _order(signal: dict) -> tuple:
    # Triggers first, in the level engine's numeric order (T-2 before T-14), then the recorded-only signals by name.
    return (0, int(signal["id"][2:]), "") if signal["kind"] == "trigger" else (1, 0, signal["id"])


def re_review_event(card: dict, *, pr: int | None, head_sha: str, recorded: list[dict]) -> dict:
    fired = [{"id": r["trigger"], "kind": "trigger", "level": r["level"], "summary": r["summary"], "count": r.get("total", len(r["refs"]))}
             for r in card["reasons"]]
    signals, seen = [], set()
    for signal in sorted((_one_line(s) for s in fired + [dict(s) for s in recorded]), key=_order):
        if signal["id"] not in seen:
            seen.add(signal["id"])
            signals.append(signal)
    doc = {"schema": "re-review-event/v1", "repo": card["app"]["repo"], "pr": pr, "head_sha": head_sha,
           "tree_sha": card["app"]["tree_sha"], "scorecard_version": BUILDER_VERSION, "created": card["generated_at"],
           "level": card["level"], "floor": card["floor"], "complete": card["assessment"]["complete"], "signals": signals}
    errors = validate("re-review-event.v1", doc)
    if errors:
        raise InputError(f"the re-review event is not a valid re-review-event/v1: {errors[0]}")
    return doc
