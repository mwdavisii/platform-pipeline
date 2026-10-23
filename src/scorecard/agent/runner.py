"""The agent loop: one conversation per batch, tools answered by code, and a structured submission checked by code.

Per batch: at most `max_turns` model calls and one correction for an invalid submission; after that the batch's
requirements are left out of the assessment, so `scorecard run` makes them `needs_human` and fires T-14. A spent token
or time budget, or a gateway that refuses the credential, stops the run: every batch not yet done is left out the same way.
The model never sees the PR's title, body, comments, or commit messages (SEC-4), and nothing it says is logged.
"""

import json
import time
from importlib import resources

from golden_road.io import InputError

from .. import git
from ..asvs import chapter_names
from ..build import Prepared, agent_scope
from ..config import model_temperature
from ..scorer import check_assessment
from .gateway import GatewayError, ModelClient
from .output import InvalidOutput, parse_batch
from .plan import Budget, estimate_tokens, plan_batches
from .prompt import batch_message, load_prompt, repo_context
from .tools import SPECS, RepoTools

MAX_CALLS_PER_TURN = 8
MAX_FINDINGS = 1000
SUBMIT_NAME = "submit_assessment"


def _submit_spec() -> dict:
    schema = json.loads((resources.files("contracts") / "schemas" / "agent-batch.v1.json").read_text(encoding="utf-8"))
    params = {k: v for k, v in schema.items() if k not in ("$schema", "$id", "title", "description")}
    return {"type": "function", "function": {
        "name": SUBMIT_NAME, "parameters": params,
        "description": "Submit the results for every requirement in this batch, and any findings. Call it once, at the end."}}


class BatchFailed(Exception):
    """This batch produced no usable output; its requirements become needs_human. The message is code-written and never
    quotes the model, because it goes to the run's stats (raw model text is never logged)."""


class Stop(Exception):
    """The run can't make more model calls (budget, time, or a refused credential)."""


def run_batch(client: ModelClient, tools: RepoTools, budget: Budget, *, system: str, user: str, batch_ids: list[str],
              max_turns: int, now: str) -> tuple[list[dict], list[dict]]:
    specs = [*SPECS, _submit_spec()]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    corrected = False
    for _ in range(max_turns):
        estimate = estimate_tokens(messages, specs)
        if why := budget.refusal(estimate):
            raise Stop(why)
        left = budget.remaining_seconds()
        if left is not None and hasattr(client, "set_time_limit"):  # the HTTP client bounds a slow response body by the run's remaining time
            client.set_time_limit(left)  # with no run deadline, each socket operation still has the gateway's timeout_seconds
        try:
            done = client.complete(messages, specs, max_tokens=budget.per_call)
        except GatewayError as exc:
            if exc.fatal:
                raise Stop(str(exc)) from None
            raise BatchFailed(str(exc)) from None
        budget.spend(done.tokens, estimate, done.message)
        message = done.message
        calls = message.get("tool_calls") or []
        skipped = max(0, len(calls) - MAX_CALLS_PER_TURN)
        if skipped:  # M6: at most MAX_CALLS_PER_TURN calls per reply run; the rest are dropped from the history too
            calls = calls[:MAX_CALLS_PER_TURN]
            message = {**message, "tool_calls": calls}
        messages.append(message)
        if not calls:  # a plain-text answer is read as the submission
            try:
                return parse_batch(message.get("content") or "", batch_ids, now=now)
            except InvalidOutput as exc:
                if corrected:
                    raise BatchFailed("invalid output after one correction") from None
                corrected = True
                messages.append({"role": "user", "content": f"error: {exc}. Call {SUBMIT_NAME} with a corrected submission."})
                continue
        rejected = False
        for call in calls:
            name, arguments = call["function"]["name"], call["function"]["arguments"]
            if rejected:  # one invalid submission ends the handling of this reply; the correction turn follows
                reply = "error: not run; fix the submission first"
            elif name == SUBMIT_NAME:
                try:
                    return parse_batch(arguments, batch_ids, now=now)
                except InvalidOutput as exc:
                    if corrected:
                        raise BatchFailed("invalid output after one correction") from None
                    corrected = rejected = True
                    reply = f"error: {exc}. Call {SUBMIT_NAME} again with a corrected submission."
            else:
                reply = tools.call(name, arguments)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": reply})
        if skipped:
            messages[-1]["content"] += f"\n[note: only the first {MAX_CALLS_PER_TURN} tool calls of your last reply were run; {skipped} were dropped]"
    raise BatchFailed(f"no valid submission within {max_turns} model calls")


def assess(prepared: Prepared, client: ModelClient, *, clock=time.monotonic) -> tuple[dict, dict]:
    """Return (assessment/v1, run stats). The stats carry no model text, only counts and code-written reasons."""
    config = prepared.config
    agent = config["agent"]
    scope = agent_scope(prepared)
    system = load_prompt(agent["prompt_version"])
    tools = RepoTools(prepared.repo_root, config)
    cap = agent["tools"]["max_diff_chars"]
    diff_text, cut = git.diff_capped(prepared.repo_root, prepared.base, prepared.head, 4 * cap + 100_000)  # bytes; enough for `cap` characters
    citable = set(tools.paths)

    def read_base(path):
        return git.show(prepared.repo_root, prepared.base, path)

    context = repo_context(prepared.changed, diff_text, patterns=config["patterns"]["injection"], max_diff_chars=cap,
                           citable=citable, read_head=tools.raw_text, read_base=read_base, raw_truncated=cut)
    names = chapter_names(prepared.requirements)
    budget = Budget(agent["budget"], clock)
    batches = plan_batches(scope["requirements"], agent["batching"]["max_requirements"])
    results, findings, failed, stopped = [], {}, [], None
    for batch in batches:
        if stopped:
            failed.append({"batch": batch["key"], "reason": stopped})
            continue
        user = batch_message(batch, intake=prepared.intake, classification=prepared.classification, names=names, context=context)
        try:
            got, found = run_batch(client, tools, budget, system=system, user=user, now=prepared.now,
                                   batch_ids=[r["asvs_id"] for r in batch["requirements"]], max_turns=agent["batching"]["max_turns"])
        except Stop as exc:
            stopped = str(exc)
            failed.append({"batch": batch["key"], "reason": stopped})
            continue
        except BatchFailed as exc:
            failed.append({"batch": batch["key"], "reason": str(exc)})
            continue
        results += got
        for finding in found:
            findings.setdefault(finding["id"], finding)
    kept = list(findings.values())
    if len(kept) > MAX_FINDINGS:
        kept, failed = kept[:MAX_FINDINGS], [*failed, {"batch": "all", "reason": f"more than {MAX_FINDINGS} findings"}]
    doc = {"schema": "assessment/v1", "tree_sha": scope["tree_sha"], "prompt_version": agent["prompt_version"],
           "model": {"version": agent["model"]["version"], "temperature": model_temperature(prepared.config)},
           "complete": not failed, "results": results, "findings": kept}
    try:
        check_assessment(doc, prepared.tree_sha)
    except InputError as exc:
        raise InputError(f"the agent produced an assessment that is not valid: {exc}") from None
    stats = {"tree_sha": scope["tree_sha"], "requirements": len(scope["requirements"]), "batches": len(batches),
             "failed_batches": failed, "tokens_used": budget.used, "complete": not failed}
    return doc, stats
