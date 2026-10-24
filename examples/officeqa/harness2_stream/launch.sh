#!/bin/bash
# Harness² under the paper's protocol on OfficeQA: one stream of the test split through `hermit train`, as the
# CL-Bench examples run theirs.
#
#   FIREWORKS_API_KEY=... SEED=0 WM_PORT=3591 examples/officeqa_paper/harness2_stream/launch.sh [overrides...]
#   HARNESS=codex ...                                      # Codex as the acting agent (run_codex.yaml), as the paper
#   EDITOR_MODEL=accounts/fireworks/models/glm-5p3 ...     # the editors' model (default: direct glm-5p3, codex glm-5p3-flash)
#   MAX_TASKS=2 TAG=h2-smoke ...                           # a smoke run on the first questions of the stream
#   SKIP_TASKS=10 ...                                      # drop the stream's first questions (continuing a run that took them)
#   LIBRARY_FROM=runs/h2-stream-s0 TAG=h2-stream-s0-rest STREAM=stream_rest_s0.txt \
#     SPLITS="../utils/splits/train.txt ../utils/splits/val.txt" CLAUDE_BIN=runs/.claude-pin/<version>/claude ...
#                                                          # that stream continued on other questions (paths: from here)
#
# Writes upstream's stream order for SEED, regenerates H0 (../harness2/seed_adapter, or seed_adapter_codex for
# HARNESS=codex), starts a wire meter in front of Fireworks for the editor sessions (bucket TAG; ledger
# runs/TAG.wire_ledger.json), and trains with the editor model bound to the meter. Needs the `harness2` and `skillopt`
# extras, the Claude Code CLI and bubblewrap; HARNESS=codex also needs the Codex harness's container (../utils/agent/
# codex_session.py --check names what is missing; docker/ builds it).
set -eu -o pipefail
if [ -n "${LIBRARY_FROM:-}" ]; then          # a continuation names what differs from the stream it continues
  for v in TAG SPLITS STREAM CLAUDE_BIN; do
    [ -n "${!v:-}" ] || { echo "REFUSING: a continuation (LIBRARY_FROM) needs $v"; exit 1; }
  done
fi
HERE=$(cd "$(dirname "$0")" && pwd)
FRAMEWORK=$(cd "$HERE/../../.." && pwd)
PY=${MW_FRAMEWORK_PYTHON:-$FRAMEWORK/.venv/bin/python}
SEED=${SEED:-0}
HARNESS=${HARNESS:-direct}                   # direct: SkillOpt's rollout (run.yaml); codex: a Codex session (run_codex.yaml)
case "$HARNESS" in
  direct) CONFIG=run.yaml; TAG=${TAG:-h2-stream-s$SEED}; EDITOR_MODEL=${EDITOR_MODEL:-accounts/fireworks/models/glm-5p3};;
  codex) CONFIG=run_codex.yaml; TAG=${TAG:-h2-codex-s$SEED}; EDITOR_MODEL=${EDITOR_MODEL:-accounts/fireworks/models/glm-5p3-flash};;
  *) echo "REFUSING: HARNESS is direct or codex, not $HARNESS"; exit 1;; esac
SPLITS=${SPLITS:-../utils/splits/test.txt}   # the stream's questions (several files: their union)
STREAM=${STREAM:-stream_s$SEED.txt}
LIBRARY_FROM=${LIBRARY_FROM:-}               # a finished run whose library this run starts from: a continuation
WM_PORT=${WM_PORT:?set WM_PORT, the wire meter port for the editor sessions}
: "${FIREWORKS_API_KEY:?set FIREWORKS_API_KEY}"
command -v claude > /dev/null || { echo "REFUSING: the Claude Code CLI (claude) is not on PATH"; exit 1; }
export PYTHONPATH=$FRAMEWORK${PYTHONPATH:+:$PYTHONPATH}
cd "$HERE"
if [ -n "$LIBRARY_FROM" ]; then
  [ -d "$LIBRARY_FROM" ] || { echo "REFUSING: LIBRARY_FROM=$LIBRARY_FROM is not a directory"; exit 1; }
  LIBRARY_FROM=$(readlink -f "$LIBRARY_FROM")
  [ "$(readlink -f "runs/$TAG")" != "$LIBRARY_FROM" ] || { echo "REFUSING: TAG=$TAG is the run continued"; exit 1; }
fi
mkdir -p runs
if [ "$HARNESS" = codex ]; then
  "$PY" make_seed_codex.py
  "$PY" ../utils/agent/codex_session.py --check "$("$PY" -c "import json; print(json.load(open('../utils/data.json'))['docs'])")" \
    || { echo "REFUSING: the Codex harness's container is not ready (see ../utils/agent/codex_session.py)"; exit 1; }
else
  "$PY" ../harness2/make_seed.py
fi
"$PY" stream.py --seed "$SEED" --split $SPLITS --out "$STREAM" ${MAX_TASKS:+--max "$MAX_TASKS"} ${SKIP_TASKS:+--skip "$SKIP_TASKS"}
# The meter restarts if it dies; each start keeps its own ledger (sum runs/$TAG.wire_ledger-*.json for the bill).
if ! curl -s -m 2 "http://127.0.0.1:$WM_PORT/health" > /dev/null; then
  setsid nohup bash -c "while true; do PORT=$WM_PORT UPSTREAM=https://api.fireworks.ai \
      LEDGER=$HERE/runs/$TAG.wire_ledger-\$(date -u +%Y%m%dT%H%M%SZ).json '$PY' -m hermit.runtime.wire_meter; \
      sleep 5; done" >> "runs/$TAG.wire_meter.log" 2>&1 < /dev/null &
  sleep 3
fi
# Claude Code updates itself: the run uses a copy of today's binary (or of CLAUDE_BIN, e.g. the stream it continues
# pinned), alone in its directory (the sessions' sandbox shows the directory a program lives in).
if [ -n "${CLAUDE_BIN:-}" ]; then
  CLAUDE_BIN=$(readlink -f "$CLAUDE_BIN"); VERSION=$("$CLAUDE_BIN" --version | awk '{print $1}')
else
  CLAUDE_BIN=$(readlink -f "$(command -v claude)"); VERSION=$(basename "$CLAUDE_BIN")
fi
[[ "$VERSION" =~ ^[0-9]+(\.[0-9]+)+$ ]] || { echo "REFUSING: no version for $CLAUDE_BIN ($VERSION)"; exit 1; }
PINNED="$HERE/runs/.claude-pin/$VERSION/claude"
if [ -e "$PINNED" ]; then
  cmp -s "$CLAUDE_BIN" "$PINNED" || { echo "REFUSING: $PINNED is not $CLAUDE_BIN"; exit 1; }
else
  mkdir -p "$(dirname "$PINNED")" && cp "$CLAUDE_BIN" "$PINNED.part.$$" && mv "$PINNED.part.$$" "$PINNED"
fi
# The answer files and the run continued (its records carry the judge's feedback): the sessions' sandbox is checked to
# show none of them.
ABSENT=$("$PY" -c "import json, sys; d = json.load(open('../utils/data.json'));
from pathlib import Path; print(json.dumps([str(Path(d['answers']).parent), d['csv'], *sys.argv[1:]]))" \
  ${LIBRARY_FROM:+"$LIBRARY_FROM"})
exec "$PY" -m hermit.cli train --config "$CONFIG" "trainer.run_name=$TAG" "data.train=$STREAM" \
  "backends.learner={from: fireworks, base_url: 'http://127.0.0.1:$WM_PORT/$TAG/inference/v1'}" \
  "evolver.params.model=learner/$EDITOR_MODEL" "evolver.params.probe_absent=$ABSENT" \
  "evolver.params.claude_bin=$PINNED" ${LIBRARY_FROM:+"evolver.params.library_from=$LIBRARY_FROM"} "$@"
