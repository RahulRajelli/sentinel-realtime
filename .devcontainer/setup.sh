#!/usr/bin/env bash
# Codespaces / devcontainer bootstrap.
#
# Exists so the two-clone install is not the reason someone bounces off this repo. After this
# runs, `pytest -q` and the reproduce command in QUICKSTART.md both work with no further setup
# and no API key.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT="$(dirname "$REPO_DIR")"
SIBLING="$PARENT/ardupilot-log-analyzer"

echo "==> repo:    $REPO_DIR"
echo "==> sibling: $SIBLING"

# flightdx resolves by path in places, so it must be a SIBLING of this repo, not a subdirectory.
if [ ! -d "$SIBLING" ]; then
  echo "==> cloning ardupilot-log-analyzer (the detectors; not on PyPI)"
  git clone --depth 1 https://github.com/RahulRajelli/ardupilot-log-analyzer "$SIBLING"
else
  echo "==> sibling already present, skipping clone"
fi

python -m pip install --upgrade pip
python -m pip install -e "$REPO_DIR"
python -m pip install -e "$SIBLING"

echo
echo "==> verifying the checkout is healthy"
cd "$REPO_DIR"
# The figures gate is ~30s of this; it re-scores every committed verdict file and checks the
# published numbers still regenerate. If setup silently produced a broken install, this is where
# it surfaces -- better here than when the reader assumes their own machine is at fault.
python -m pytest -q

cat <<'EOF'

  Ready. The headline result, offline and with no API key:

    python scripts/e4_report.py --bundles bundles \
      --verdicts results/crossmodel/gpt2_run1.json --only compass_offset

  Expect B0 -- the free rule, no model in it -- to score 0.00.
  Walkthrough with expected output at each step: QUICKSTART.md

EOF
