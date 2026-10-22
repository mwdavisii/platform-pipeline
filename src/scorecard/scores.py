"""Scores are for the report. Only coverage decides anything (T-11), and an empty denominator is `null`, never 0 or 100."""

STATUSES = ("pass", "fail", "na", "needs_human")


def counts(results: list[dict]) -> dict:
    out = dict.fromkeys(STATUSES, 0)
    for r in results:
        out[r["status"]] += 1
    return out


def _percent(numerator: int, denominator: int) -> float | None:
    return round(100.0 * numerator / denominator, 1) if denominator else None


def score(c: dict) -> float | None:
    """FR-9: pass / (pass + fail). needs_human is reported separately, not counted."""
    return _percent(c["pass"], c["pass"] + c["fail"])


def coverage(agent_results: list[dict]) -> float | None:
    """FR-11 over the agent's set only: platform-provided, tool-covered, and confirmed-na requirements are reported separately."""
    c = counts(agent_results)
    return _percent(c["pass"] + c["fail"], len(agent_results))


def coverage_counts(agent_results: list[dict]) -> tuple[int, int]:
    """(assessed, total) over the agent's set, unrounded: T-11 compares these, not the displayed percentage."""
    c = counts(agent_results)
    return c["pass"] + c["fail"], len(agent_results)


def chapters(results: list[dict], names: dict[str, str]) -> list[dict]:
    by_chapter: dict[str, list] = {}
    for r in results:
        by_chapter.setdefault(r["chapter"], []).append(r)
    out = []
    for chapter in sorted(by_chapter, key=lambda c: int(c.rsplit("-", 1)[1])):
        c = counts(by_chapter[chapter])
        out.append({"chapter": chapter, "name": names[chapter], "score": score(c), "counts": c})
    return out
