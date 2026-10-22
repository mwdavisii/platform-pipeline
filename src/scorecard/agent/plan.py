"""Batches and the per-run budget. One batch per ASVS chapter; a chapter with more requirements than
`max_requirements` is split by section, and a section that is still too large into numbered parts."""

import json
import time

TOKENS_PER_CHAR = 1 / 3  # deliberately high (most text is nearer 1/4), so the estimate errs toward stopping early


def plan_batches(requirements: list[dict], max_requirements: int) -> list[dict]:
    """[{"key", "requirements"}] in scope order. `requirements` are agent-scope/v1 entries."""
    chapters: dict[str, list[dict]] = {}
    for req in requirements:
        chapters.setdefault(req["chapter"], []).append(req)
    batches = []
    for chapter, reqs in chapters.items():
        if len(reqs) <= max_requirements:
            batches.append({"key": chapter, "requirements": reqs})
            continue
        sections: dict[str, list[dict]] = {}
        for req in reqs:
            sections.setdefault(req["section"], []).append(req)
        for section, items in sections.items():
            parts = [items[i:i + max_requirements] for i in range(0, len(items), max_requirements)]
            for n, part in enumerate(parts, 1):
                batches.append({"key": section if len(parts) == 1 else f"{section} part {n}", "requirements": part})
    return batches


def estimate_tokens(*items) -> int:
    return int(len(json.dumps(items, ensure_ascii=False)) * TOKENS_PER_CHAR) + 1


class Budget:
    """Tokens and wall-clock time for one run. A call is allowed only if its worst case fits in what is left."""

    def __init__(self, budget: dict, clock=time.monotonic):
        self.limit, self.per_call, self.used = budget["max_total_tokens"], budget["max_tokens_per_call"], 0
        self.clock = clock
        self.deadline = clock() + budget["max_run_seconds"]

    def remaining_seconds(self) -> float:
        return self.deadline - self.clock()

    def refusal(self, prompt_estimate: int) -> str | None:
        """Why the next call can't be made, or None when it can."""
        if self.clock() >= self.deadline:
            return "the run's time budget ran out"
        if self.used + prompt_estimate + self.per_call > self.limit:
            return "the run's token budget ran out"
        return None

    def spend(self, reported: int | None, prompt_estimate: int, reply) -> None:
        """Charge the larger of what the gateway reported and our own high estimate, so a gateway that reports 0 (or
        nothing) can't make the run unlimited."""
        estimate = prompt_estimate + estimate_tokens(reply)
        self.used += max(reported or 0, estimate)
