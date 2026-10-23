"""T-19's input: how large the change is, from the base and head diff.

Lines are added plus removed; a binary file counts as one changed file and no lines. A comparison that fails is an input
problem (T-14), and T-19 does not fire.
"""

from golden_road.io import InputError

from .. import git


def change_size(repo, base: str, head: str, problems: list) -> dict | None:
    try:
        rows = git.numstat(repo, base, head)
    except InputError as exc:
        problems.append({"source": "change size", "detail": f"can't count the changed lines: {exc}"[:300]})
        return None
    return {"lines": sum((added or 0) + (removed or 0) for added, removed, _ in rows), "files": len(rows)}
