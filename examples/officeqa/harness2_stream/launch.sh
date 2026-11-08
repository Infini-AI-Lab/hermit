#!/bin/bash
# Harness² under the paper's protocol on OfficeQA: one stream of the test split through `hermit train`, as the
# CL-Bench examples run theirs.
#
#   FIREWORKS_API_KEY=... SEED=0 WM_PORT=3591 examples/officeqa/harness2_stream/launch.sh [overrides...]
#   EDITOR_MODEL=accounts/fireworks/models/glm-5p3 ...     # the editors' model (default glm-5p3-flash)
#   CONFIG=run_codex_luna.yaml examples/officeqa/harness2_stream/launch.sh [overrides...]
#                                                          # the Flash row with GPT-5.6-Luna: executor and editors on the
#                                                          # machine's ChatGPT login, no key and no meter; the editors are
#                                                          # Codex sessions of a pinned copy of the
#                                                          # machine's Codex (CODEX_BIN, default the `codex` on PATH)
#   MAX_TASKS=2 TAG=h2-smoke ...                           # a smoke run on the first questions of the stream
#   SKIP_TASKS=10 ...                                      # drop the stream's first questions (continuing a run that took them)
#   LIBRARY_FROM=runs/h2-codex-s0 TAG=h2-codex-s0-rest STREAM=stream_rest_s0.txt \
#     SPLITS="../utils/splits/train.txt ../utils/splits/val.txt" CLAUDE_BIN=runs/.claude-pin/<version>/claude ...
#                                                          # that stream continued on other questions (paths: from here)
#
# Writes upstream's stream order for SEED, regenerates H0 (seed_adapter_codex) and trains. With Claude Code editors
# (run_codex.yaml) it starts a wire meter in front of Fireworks for the editor sessions (bucket TAG; ledger
# runs/TAG.wire_ledger.json) and trains with the editor model bound to the meter; with Codex editors
# (run_codex_luna.yaml, `evolver.params.backend: codex`) the config names the editors' model and the sessions reach it
# through the run's allowlist proxy. Needs the `harness2` and `skillopt` dependency groups (python -m pip install -e .
# --group harness2 --group skillopt), the Claude Code CLI (or Codex, for run_codex_luna.yaml), bubblewrap and the Codex
# agent's container (the image, the network and the egress proxy of the config; docker/codex/ builds them; the
# agent names what is missing before its first session).
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
CONFIG=${CONFIG:-run_codex.yaml}
[ -f "$HERE/$CONFIG" ] || { echo "REFUSING: no $CONFIG beside this launcher"; exit 1; }
# The editors' program: Claude Code unless the config says `evolver.params.backend: codex`. The run's name: the
# config's run name (h2-codex-s0, luna-h2-s0) with the seed in place of its 0.
EDITORS=$("$PY" -c "import yaml; c = yaml.safe_load(open('$HERE/$CONFIG'));
print(((c.get('evolver') or {}).get('params') or {}).get('backend') or 'claude')")
RUN_NAME=$("$PY" -c "import yaml; print(yaml.safe_load(open('$HERE/$CONFIG'))['trainer']['run_name'])")
case "$RUN_NAME" in *s0) TAG=${TAG:-${RUN_NAME%0}$SEED} ;; *) TAG=${TAG:-$RUN_NAME} ;; esac
SPLITS=${SPLITS:-../utils/splits/test.txt}   # the stream's questions (several files: their union)
STREAM=${STREAM:-stream_s$SEED.txt}
LIBRARY_FROM=${LIBRARY_FROM:-}               # a finished run whose library this run starts from: a continuation
case "$EDITORS" in
  claude)
    EDITOR_MODEL=${EDITOR_MODEL:-accounts/fireworks/models/glm-5p3-flash}
    WM_PORT=${WM_PORT:?set WM_PORT, the wire meter port for the editor sessions}
    : "${FIREWORKS_API_KEY:?set FIREWORKS_API_KEY}"
    command -v claude > /dev/null || { echo "REFUSING: the Claude Code CLI (claude) is not on PATH"; exit 1; } ;;
  codex)
    CODEX_BIN=${CODEX_BIN:-$(command -v codex || true)}
    [ -n "$CODEX_BIN" ] || { echo "REFUSING: no Codex (codex) on PATH and no CODEX_BIN"; exit 1; } ;;
  *) echo "REFUSING: evolver.params.backend in $CONFIG is '$EDITORS', not claude or codex"; exit 1 ;;
esac
export PYTHONPATH=$FRAMEWORK${PYTHONPATH:+:$PYTHONPATH}
cd "$HERE"
if [ -n "$LIBRARY_FROM" ]; then
  [ -d "$LIBRARY_FROM" ] || { echo "REFUSING: LIBRARY_FROM=$LIBRARY_FROM is not a directory"; exit 1; }
  LIBRARY_FROM=$(readlink -f "$LIBRARY_FROM")
  [ "$(readlink -f "runs/$TAG")" != "$LIBRARY_FROM" ] || { echo "REFUSING: TAG=$TAG is the run continued"; exit 1; }
fi
mkdir -p runs
"$PY" make_seed_codex.py
"$PY" stream.py --seed "$SEED" --split $SPLITS --out "$STREAM" ${MAX_TASKS:+--max "$MAX_TASKS"} ${SKIP_TASKS:+--skip "$SKIP_TASKS"}
# The meters this launch starts stop once the processes it names have ended (#82); a meter found already running is
# left to the launch that started it.
METERS=()
stop_meters_after() {  # pid...
  [ ${#METERS[@]} -gt 0 ] || return 0
  setsid nohup bash -c 'for p in $1; do while kill -0 "$p" 2>/dev/null; do sleep 30; done; done
    for m in $2; do kill -- "-$m" 2>/dev/null || kill "$m" 2>/dev/null || true; done' _ "$*" "${METERS[*]}" \
    > /dev/null 2>&1 < /dev/null &
}
if [ "$EDITORS" = claude ]; then
  # The meter restarts if it dies; each start keeps its own ledger (sum runs/$TAG.wire_ledger-*.json for the bill).
  if ! curl -s -m 2 "http://127.0.0.1:$WM_PORT/health" > /dev/null; then
    setsid nohup bash -c "while true; do PORT=$WM_PORT UPSTREAM=https://api.fireworks.ai \
        LEDGER=$HERE/runs/$TAG.wire_ledger-\$(date -u +%Y%m%dT%H%M%SZ).json '$PY' -m hermit.runtime.wire_meter; \
        sleep 5; done" >> "runs/$TAG.wire_meter.log" 2>&1 < /dev/null &
    METERS+=("$!"); stop_meters_after $$   # the restart loop with its meter, when the run exec'd below ends
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
else
  # Codex updates itself too: the run uses a copy of the machine's standalone package (bin/codex with its ripgrep and
  # bubblewrap), alone in its directory, the program the editor sessions run (CLAUDE_BIN: a pin to reuse).
  CODEX_BIN=$(readlink -f "${CLAUDE_BIN:-$CODEX_BIN}"); VERSION=$("$CODEX_BIN" --version | awk '{print $NF}')
  [[ "$VERSION" =~ ^[0-9]+(\.[0-9]+)+$ ]] || { echo "REFUSING: no version for $CODEX_BIN ($VERSION)"; exit 1; }
  PACKAGE=$(dirname "$(dirname "$CODEX_BIN")")
  [ "$CODEX_BIN" = "$PACKAGE/bin/codex" ] || { echo "REFUSING: $CODEX_BIN is not <package>/bin/codex of a standalone Codex"; exit 1; }
  PIN_DIR="$HERE/runs/.codex-pin/$VERSION"; PINNED="$PIN_DIR/bin/codex"
  if [ -e "$PINNED" ]; then
    cmp -s "$CODEX_BIN" "$PINNED" || { echo "REFUSING: $PINNED is not $CODEX_BIN"; exit 1; }
  else
    mkdir -p "$(dirname "$PIN_DIR")" && cp -a "$PACKAGE" "$PIN_DIR.part.$$" && mv "$PIN_DIR.part.$$" "$PIN_DIR"
  fi
fi
# The answer files and the run continued (its records carry the judge's feedback): the sessions' sandbox is checked to
# show none of them.
ABSENT=$("$PY" -c "import json, sys; d = json.load(open('../utils/data.json'));
from pathlib import Path; print(json.dumps([str(Path(d['answers']).parent), d['csv'], *sys.argv[1:]]))" \
  ${LIBRARY_FROM:+"$LIBRARY_FROM"})
if [ "$EDITORS" = claude ]; then
  exec "$PY" -m hermit.cli train --config "$CONFIG" "trainer.run_name=$TAG" "data.train=$STREAM" \
    "backends.learner={from: fireworks, base_url: 'http://127.0.0.1:$WM_PORT/$TAG/inference/v1'}" \
    "evolver.params.model=learner/$EDITOR_MODEL" "evolver.params.probe_absent=$ABSENT" \
    "evolver.params.claude_bin=$PINNED" ${LIBRARY_FROM:+"evolver.params.library_from=$LIBRARY_FROM"} "$@"
fi
exec "$PY" -m hermit.cli train --config "$CONFIG" "trainer.run_name=$TAG" "data.train=$STREAM" \
  "evolver.params.probe_absent=$ABSENT" "evolver.params.claude_bin=$PINNED" \
  ${LIBRARY_FROM:+"evolver.params.library_from=$LIBRARY_FROM"} "$@"
