import sys
from pathlib import Path

# Tests import the src/ modules directly (agent.py, replay.py, etc. use
# flat imports like `from guardrails import ...`), so src/ must be on the
# path exactly like it is when running e.g. `python src/replay.py`.
SRC = Path(__file__).parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
