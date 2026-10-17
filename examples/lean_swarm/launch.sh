#!/bin/bash
# Run the Lean swarm on one problem: examples/lean_swarm/launch.sh <problem.lean> <theorem> [options]
# Uses the framework's own virtualenv when there is one (.venv at the repository root), else the current python.
HERE=$(cd "$(dirname "$0")" && pwd)
PY="$HERE/../../.venv/bin/python"; [ -x "$PY" ] || PY=python3
exec "$PY" "$HERE/launch.py" "$@"
