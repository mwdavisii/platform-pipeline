"""agent_file.py is one module in three places (decided while planning: item 6): the builder's, the portal's and the runtime's."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COPIES = ("bootstrap/golden-road-portal/portal/agent_file.py", "images/agent-runtime/agent_runtime/agent_file.py")


def test_the_copies_are_the_contracts_byte_for_byte():
    source = (ROOT / "src/contracts/agent_file.py").read_bytes()
    for rel in COPIES:
        assert (ROOT / rel).read_bytes() == source, rel
