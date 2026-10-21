#!/bin/bash
# Harness² under the paper's protocol on OfficeQA: one stream of the test split through `harnessadapter train`, as the
# CL-Bench examples run theirs.
#
#   FIREWORKS_API_KEY=... SEED=0 WM_PORT=3591 examples/officeqa_paper/harness2_stream/launch.sh [overrides...]
#   MAX_TASKS=2 TAG=h2-smoke ...                           # a smoke run on the first questions of the stream
#
# Writes upstream's stream order for SEED, regenerates H0 (../harness2/seed_adapter), starts a wire meter in front of
# Fireworks for the editor sessions (bucket TAG; ledger runs/TAG.wire_ledger.json), and trains with the editor model
# bound to the meter. Needs the `harness2` and `skillopt` extras, the Claude Code CLI and bubblewrap.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
FRAMEWORK=$(cd "$HERE/../../.." && pwd)
PY=${MW_FRAMEWORK_PYTHON:-$FRAMEWORK/.venv/bin/python}
SEED=${SEED:-0}
TAG=${TAG:-h2-stream-s$SEED}
WM_PORT=${WM_PORT:?set WM_PORT, the wire meter port for the editor sessions}
: "${FIREWORKS_API_KEY:?set FIREWORKS_API_KEY}"
command -v claude > /dev/null || { echo "REFUSING: the Claude Code CLI (claude) is not on PATH"; exit 1; }
export PYTHONPATH=$FRAMEWORK${PYTHONPATH:+:$PYTHONPATH}
cd "$HERE"
mkdir -p runs
"$PY" ../harness2/make_seed.py
"$PY" stream.py --seed "$SEED" --split ../utils/splits/test.txt --out "stream_s$SEED.txt" ${MAX_TASKS:+--max "$MAX_TASKS"}
# The meter restarts if it dies; each start keeps its own ledger (sum runs/$TAG.wire_ledger-*.json for the bill).
if ! curl -s -m 2 "http://127.0.0.1:$WM_PORT/health" > /dev/null; then
  setsid nohup bash -c "while true; do PORT=$WM_PORT UPSTREAM=https://api.fireworks.ai \
      LEDGER=$HERE/runs/$TAG.wire_ledger-\$(date -u +%Y%m%dT%H%M%SZ).json '$PY' -m harnessadapter.runtime.wire_meter; \
      sleep 5; done" >> "runs/$TAG.wire_meter.log" 2>&1 < /dev/null &
  sleep 3
fi
# Claude Code updates itself: the run uses a copy of today's binary.
CLAUDE_BIN=$(readlink -f "$(command -v claude)")
PINNED="$HERE/runs/claude-$(basename "$CLAUDE_BIN")"
[ -x "$PINNED" ] || cp "$CLAUDE_BIN" "$PINNED"
# The answer files: the sessions' sandbox is checked to show neither.
ABSENT=$("$PY" -c "import json; d = json.load(open('../utils/data.json'));
from pathlib import Path; print(json.dumps([str(Path(d['answers']).parent), d['csv']]))")
exec "$PY" -m harnessadapter.cli train --config run.yaml "trainer.run_name=$TAG" "data.train=stream_s$SEED.txt" \
  "backends.learner={from: fireworks, base_url: 'http://127.0.0.1:$WM_PORT/$TAG/inference/v1'}" \
  "evolver.params.model=learner/accounts/fireworks/models/glm-5p3" "evolver.params.probe_absent=$ABSENT" \
  "evolver.params.claude_bin=$PINNED" "$@"
