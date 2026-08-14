"""Test path setup.

`flightdx` lives in the sibling `ardupilot-log-analyzer/src/` tree rather than being installed,
so `sentinel` imports it only when that directory is on `sys.path`. The live scripts get away
with this because they are launched from the project root; pytest is not, and collection fails
at import time with a bare ModuleNotFoundError that reads like the package is missing.

Prefer an editable install (`pip install -e ../ardupilot-log-analyzer`) if the environment ever
gets one; this shim exists so tests run without requiring it.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FLIGHTDX_SRC = ROOT.parent / "ardupilot-log-analyzer" / "src"

for p in (ROOT, FLIGHTDX_SRC):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))
