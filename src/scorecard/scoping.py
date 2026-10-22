"""ASVS scoping: decide what code can decide, and hand the rest to the agent.

Order: platform-provided, tool-covered, human-confirmed `na` (na/ files, then the chapter N/A
rules in config). Everything left is the agent's set, and only that set counts toward coverage.
"""

from .asvs import matches
from .lockfiles import normalize


def make_result(req: dict, status: str, source: str, *, justification: str = "", confidence: str = "high",
                evidence: list | None = None, evidence_verified: bool = False, **extra) -> dict:
    result = {
        "schema": "requirement-result/v1", "asvs_id": req["id"], "chapter": req["chapter"], "section": req["section"],
        "level": req["level"], "status": status, "confidence": confidence, "justification": justification,
        "evidence": list(evidence or []), "finding_ids": [], "evidence_verified": evidence_verified, "status_source": source,
    }
    result.update(extra)
    return result


def _platform(req: dict, entry: dict, drift_green: bool) -> dict:
    if drift_green:
        return make_result(req, "pass", "platform", justification=f"Met by the platform: {entry['control']}.", control=entry["control"])
    return make_result(req, "needs_human", "platform", control=entry["control"],
                       justification=f"Met by the platform ({entry['control']}) only when drift is green, and it is not green for this commit.")


def scope(requirements: list[dict], *, config: dict, confirmed_na: list[dict], packages: list[dict],
          has_authz: bool, drift_green: bool, lockfile_problems: list | bool = False) -> tuple[list[dict], list[dict]]:
    """Return (results decided without the agent, requirements for the agent).

    `lockfile_problems` is the problem list from `read_packages` (or a bool). When there is any, the package list is
    incomplete, so a package's absence is unknown and the package-absence N/A rules are not applied.
    """
    platform = {e["id"]: e for e in config["platform_provided"] if has_authz or not e["requires_authz"]}
    tools = {e["id"]: e["tool"] for e in config["tool_covered"]}
    confirmed = {e["asvs_id"]: e for e in confirmed_na}
    present = {normalize(p["name"]) for p in packages}
    rules = [] if lockfile_problems else [
        r for r in config["na_rules"] if not present & {normalize(n) for n in r["unless_packages"]}]
    decided, agent = [], []
    for req in requirements:
        rid = req["id"]
        rule = next((r for r in rules if matches(rid, r["selector"])), None)
        if rid in platform:
            decided.append(_platform(req, platform[rid], drift_green))
        elif rid in tools:
            decided.append(make_result(req, "needs_human", "scanner", covered_by=tools[rid],
                                       justification=f"Covered by {tools[rid]}, owned by its tool team. The agent does not evaluate it."))
        elif rid in confirmed:
            entry = confirmed[rid]
            decided.append(make_result(req, "na", "human_na",
                                       justification=f"{entry['justification']} (confirmed by {entry['confirmed_by']} on {entry['confirmed']})"))
        elif rule is not None:
            decided.append(make_result(req, "na", "human_na", justification=f"{rule['justification']} (chapter N/A rule in config)"))
        else:
            agent.append(req)
    return decided, agent
