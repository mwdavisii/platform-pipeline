from scorecard.agent.plan import Budget, estimate_tokens, plan_batches


def req(asvs_id):
    chapter, section, _ = asvs_id.removeprefix("v5.0.0-").split(".")
    return {"asvs_id": asvs_id, "chapter": f"v5.0.0-{chapter}", "section": f"v5.0.0-{chapter}.{section}", "level": 1, "text": "Verify."}


def test_one_batch_per_chapter_split_by_section_when_large():
    reqs = [req("v5.0.0-8.1.1"), req("v5.0.0-8.2.1"), req("v5.0.0-6.1.1"), req("v5.0.0-6.1.2"), req("v5.0.0-6.1.3"),
            req("v5.0.0-6.2.1")]
    batches = plan_batches(reqs, 2)
    assert [(b["key"], [r["asvs_id"] for r in b["requirements"]]) for b in batches] == [
        ("v5.0.0-8", ["v5.0.0-8.1.1", "v5.0.0-8.2.1"]),
        ("v5.0.0-6.1 part 1", ["v5.0.0-6.1.1", "v5.0.0-6.1.2"]),
        ("v5.0.0-6.1 part 2", ["v5.0.0-6.1.3"]),
        ("v5.0.0-6.2", ["v5.0.0-6.2.1"]),
    ]
    assert plan_batches([], 5) == []


def test_every_requirement_is_in_exactly_one_batch():
    from scorecard.asvs import for_level, load_asvs

    reqs = [{"asvs_id": r["id"], "chapter": r["chapter"], "section": r["section"], "level": r["level"], "text": r["text"]}
            for r in for_level(load_asvs(), 3)]
    batches = plan_batches(reqs, 12)
    ids = [r["asvs_id"] for b in batches for r in b["requirements"]]
    assert sorted(ids) == sorted(r["asvs_id"] for r in reqs) and len(ids) == len(set(ids))
    assert max(len(b["requirements"]) for b in batches) <= 12


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


LIMITS = {"max_total_tokens": 10_000, "max_tokens_per_call": 1000, "max_run_seconds": 60}


def test_budget_refuses_a_call_that_could_overrun():
    budget = Budget(LIMITS, clock=Clock())
    assert budget.refusal(8000) is None
    budget.spend(5000, 100, {})  # reported tokens above the estimate are honoured
    assert budget.used == 5000
    assert budget.refusal(4001) == "the run's token budget ran out"
    assert budget.refusal(3999) is None


def test_a_gateway_reporting_zero_or_nothing_is_charged_the_estimate():
    """M6: the charge is max(reported, estimate), so a gateway that reports 0 can't make the run unlimited."""
    budget = Budget(LIMITS, clock=Clock())
    reply = {"content": "x" * 300}
    for reported in (0, 1, None):
        before = budget.used
        budget.spend(reported, 100, reply)
        assert budget.used - before == 100 + estimate_tokens(reply) > 200
    budget.spend(50_000, 100, reply)
    assert budget.used >= 50_000
    assert budget.refusal(1) == "the run's token budget ran out"


def test_time_budget():
    clock = Clock()
    budget = Budget(LIMITS, clock=clock)
    assert budget.remaining_seconds() == 60
    clock.now += 60
    assert budget.refusal(1) == "the run's time budget ran out"


def test_zero_tokens_means_no_token_limit_but_still_counts():
    budget = Budget({**LIMITS, "max_total_tokens": 0}, clock=Clock())
    budget.spend(50_000_000, 100, {})
    budget.spend(None, 100, {})
    assert budget.used > 50_000_000 and budget.refusal(10**9) is None


def test_zero_seconds_means_no_deadline():
    clock = Clock()
    budget = Budget({**LIMITS, "max_run_seconds": 0}, clock=clock)
    clock.now += 10**9
    assert budget.remaining_seconds() is None and budget.refusal(1) is None
