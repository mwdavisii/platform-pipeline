"""The level engine: a floor from intake, raised by every trigger that fired. Nothing here ever lowers a level.

A verified `fail` outside the blocking chapters raises no trigger (it is reported in the scores only); T-1 is for blocking chapters.
"""

from golden_road.io import InputError

from .asvs import in_any
from .levels import floor as intake_floor
from .levels import effective_classification, highest

TRIGGER_FOR_VC = {"VC-1": "T-3", "VC-2": "T-4", "VC-3": "T-5", "VC-4": "T-6", "VC-INJ": "T-7", "VC-5": "T-9", "VC-6": "T-10"}
SUMMARIES = {
    "T-1": "Verified fail in a blocking chapter",
    "T-2": "Security-relevant builder-spec.json change",
    "T-3": "Tests deleted, skipped, or weakened (VC-1)",
    "T-4": "Security control removed or weakened (VC-2)",
    "T-5": "Risky new dependency (VC-3)",
    "T-6": "Debug or admin endpoint, or an auth bypass (VC-4)",
    "T-7": "Text aimed at the reviewer (VC-INJ)",
    "T-8": "No intake record for this repo",
    "T-9": "Behavior beyond the stated purpose, or PURPOSE.md changed",
    "T-10": "Sensitive data to logs, errors, telemetry, or third parties (VC-6)",
    "T-11": "ASVS coverage below the threshold",
    "T-12": "Needs-human or unconfirmed na in a blocking chapter, or an agent result or finding that failed a code check",
    "T-14": "Assessment incomplete, or an input could not be read or looked up",
    "T-15": "Scanner suppression added or widened",
    "T-16": "Workflow, CODEOWNERS, or harness-shadowing change",
}
MAX_REFS = 20


def triggers_of(finding: dict) -> set[str]:
    """Every trigger a finding fires. An agent finding maps only through its VC standards: it can't name a trigger directly.

    The lookup is strict: an unknown VC ID is an error, never skipped (check_assessment rejects them before this runs).
    """
    out = set()
    for standard in finding["standards"]:
        if standard["framework"] == "golden-road-trigger" and finding["source"]["layer"] != "agentic":
            out.add(standard["id"])
        elif standard["framework"] == "golden-road-vc":
            if standard["id"] not in TRIGGER_FOR_VC:
                raise InputError(f"unknown verification check {standard['id']}")
            out.add(TRIGGER_FOR_VC[standard["id"]])
    return out


def _finding_ref(finding: dict) -> dict:
    ref = {"finding_id": finding["id"], "file": finding["location"]["file"]}
    if finding["location"].get("start_line"):
        ref["line"] = finding["location"]["start_line"]
    return ref


def decide(*, config: dict, intake: dict | None, spec_classification: str | None, results: list[dict], findings: list[dict],
           covered: int, total: int, flags: dict, lookup_failures: list[dict], pr_text_hits: list[dict],
           problems: list[dict] = ()) -> tuple[str, str, list[dict]]:
    """Return (level, floor, reasons). `findings` are already suppression-filtered; agent findings carry re-verified evidence.

    `covered` and `total` are the raw coverage counts over the agent's set; T-11 compares them unrounded, and no agent set to
    judge (total 0) fires it. The data classification is the more sensitive of the intake's and the builder-spec's.
    """
    fired: dict[str, list[dict]] = {}

    def fire(trigger, ref):
        fired.setdefault(trigger, []).append(ref)

    classification = effective_classification(intake["data_classification"] if intake else None, spec_classification)
    if intake is None:
        fire("T-8", {"source": "factory intake"})
    # T-12 first from the failed code checks, then from blocking chapters, so the capped refs show the suspicious ones.
    for ref in flags["unverified"]:
        fire("T-12", ref)
    for asvs_id in flags["ignored"]:
        fire("T-12", {"asvs_id": asvs_id})
    for finding in findings:
        agentic = finding["source"]["layer"] == "agentic"
        if agentic and not finding["evidence_verified"]:
            continue  # raises only T-12, through flags
        triggers = triggers_of(finding)
        if agentic and not triggers:
            fire("T-12", _finding_ref(finding))
        for trigger in triggers:
            fire(trigger, _finding_ref(finding))
    for hit in pr_text_hits:
        fire("T-7", {"source": hit["source"]})
    for result in results:
        if not in_any(result["asvs_id"], config["blocking"]):
            continue
        if result["status"] == "fail":
            fire("T-1", {"asvs_id": result["asvs_id"]})
        elif result["status"] == "needs_human" and result["status_source"] != "scanner":
            fire("T-12", {"asvs_id": result["asvs_id"]})
    if total == 0 or 100 * covered < config["coverage_threshold"] * total:
        shown = "no requirements for the agent to cover" if total == 0 else f"coverage {covered} of {total} is below {config['coverage_threshold']}%"
        fire("T-11", {"source": shown})
    if flags["incomplete"]:
        fire("T-14", {"source": "agent assessment"})
    # Registry lookups and every unreadable input (tree scan, change comparison, lockfiles) are {source, detail}; none is skipped quietly.
    for failure in [*lookup_failures, *problems]:
        fire("T-14", {"source": failure["source"]})

    reasons = []
    for trigger in sorted(fired, key=lambda t: int(t[2:])):
        level = config["triggers"][trigger]
        if trigger == "T-10" and classification in config["sensitive_classifications"]:
            level = "security_review"
        refs = []
        for ref in fired[trigger]:
            if ref not in refs:
                refs.append(ref)
        summary = SUMMARIES[trigger]
        if trigger == "T-12" and flags["ignored"]:
            summary += "; the agent answered requirements it was not asked about"
        if len(refs) > MAX_REFS:
            summary += f" (showing {MAX_REFS} of {len(refs)})"
        elif len(refs) > 1:
            summary += f" ({len(refs)} items)"
        reasons.append({"trigger": trigger, "level": level, "summary": summary, "refs": refs[:MAX_REFS]})
    base = intake_floor(config, intake)
    return highest([base] + [r["level"] for r in reasons]), base, reasons
