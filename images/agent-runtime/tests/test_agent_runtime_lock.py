"""The runtime's lock (decided while planning: item 1): the portal's hash-pinned lines for exactly the closure of the runtime's direct
dependencies, nothing re-resolved."""

import importlib.metadata as md
import re
from pathlib import Path

from packaging.requirements import Requirement

RUNTIME = Path(__file__).resolve().parents[1]
PORTAL_LOCK = RUNTIME.parents[1] / "bootstrap/golden-road-portal/requirements.txt"
IMAGE = {"python_version": "3.12", "python_full_version": "3.12.0", "extra": ""}  # the image's interpreter, not the suite's
LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+) \\$")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def blocks(path: Path) -> dict:
    """{name: (version, frozenset of --hash values)} from a uv-compiled requirements file."""
    found, current = {}, None
    for line in path.read_text().splitlines():
        if (m := LINE.match(line)):
            current = canonical(m.group(1))
            found[current] = (m.group(2), set())
        elif line.strip().startswith("--hash=") and current:
            found[current][1].add(line.strip().rstrip(" \\"))
        elif not line.startswith("    "):
            current = None
    return {name: (version, frozenset(hashes)) for name, (version, hashes) in found.items()}


def direct() -> list[str]:
    return [canonical(line) for line in (RUNTIME / "requirements.in").read_text().splitlines() if line and not line.startswith("#")]


def closure() -> set:
    seen, todo = set(), direct()
    while todo:
        name = canonical(todo.pop())
        if name in seen:
            continue
        seen.add(name)
        for text in md.requires(name) or []:
            requirement = Requirement(text)
            if requirement.marker is None or requirement.marker.evaluate(IMAGE):
                todo.append(requirement.name)
    return seen


def test_every_pin_is_the_portal_s_byte_for_byte():
    portal, runtime = blocks(PORTAL_LOCK), blocks(RUNTIME / "requirements.txt")
    assert runtime and all(portal.get(name) == pin for name, pin in runtime.items()), sorted(set(runtime.items()) - set(portal.items()))


def test_the_lock_is_exactly_the_closure():
    assert set(blocks(RUNTIME / "requirements.txt")) == closure()


def test_every_pin_has_a_hash():
    assert all(hashes for _, hashes in blocks(RUNTIME / "requirements.txt").values())


def test_every_text_cut_says_it_is_not_a_budget():
    """No budgets (A10; the final review's M2): the runtime's few text cuts are sizes of a description or an error summary, never a
    token or time budget, and each says so where it is."""
    import re
    from pathlib import Path
    package = Path(__file__).resolve().parents[1] / "agent_runtime"
    cuts = []
    for path in sorted(package.glob("*.py")):
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            if re.search(r"\)\[:\d+\]", line):
                cuts.append((path.name, i + 1, line))
                window = " ".join(lines[max(0, i - 1):i + 2])
                assert "not a token or time budget" in window, f"{path.name}:{i + 1} cuts text without saying it isn't a budget"
    assert {(name, re.search(r"\[:(\d+)\]", line).group(1)) for name, _, line in cuts} >= {
        ("registry.py", "1024"), ("loop.py", "500"), ("mcp.py", "200"), ("mcp.py", "500"), ("databricks.py", "300")}
