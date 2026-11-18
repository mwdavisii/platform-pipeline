"""The agent runtime's tests: the runtime's root goes on sys.path so `import agent_runtime` works from this repo's suite."""

import sys
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # runtime_fakes, whichever directory pytest was started from
