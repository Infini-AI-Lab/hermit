#!/bin/bash
# MWM on a ServeLearnBench scenario, host and learner both GLM 5.3 flash on Fireworks (reasoning effort high):
#
#   examples/mwm/servelearnbench/launch.sh retail_l3          # train on the serving stream (background)
#   examples/mwm/servelearnbench/launch.sh retail_l3 test     # the per-window tests, then `slb score`
#
# `train` starts two wire meters (the host's on WM_PORT, the learner's on WM_PORT+1) and one `harnessadapter train`
# in the background, logging to <runs>/<tag>/run.log. `test` replays every window's held-out tests on the adapter
# the run had at that window (examples/tasks/servelearnbench/windows.py) and scores the result file; it can run
# while training is still going (windows not trained yet are skipped) and again later.
#
# Settings (environment variables, all optional):
#   MW_TAG           the run's name (default slb_<scenario>)        WM_PORT  the host meter's port (default 3451)
#   MW_RUNS          where runs go (default ~/code/metaworld-runs)  MW_TRAIN_SPLIT  another train split (a smoke)
#   FIREWORKS_API_KEY, or MW_KEY_FILE (default ~/code/metaworld/.fireworks)
#   MW_WIRE_METER    the wire meter script (default: the framework's harnessadapter/runtime/wire_meter.py)
#   MW_MODEL_ID, MW_EVOLVER_MODEL   the host and learner models (Fireworks ids)
set -eu
SCENARIO=${1:?usage: launch.sh <scenario> [train|test]}
MODE=${2:-train}
HERE=$(cd "$(dirname "$0")" && pwd)
FRAMEWORK=$(cd "$HERE/../../.." && pwd)
CODE=${MW_CODE:-$HOME/code}
RUNS=${MW_RUNS:-$CODE/metaworld-runs}
TAG=${MW_TAG:-slb_$SCENARIO}
WM_PORT=${WM_PORT:-3451}
PY=${MW_FRAMEWORK_PYTHON:-$FRAMEWORK/.venv/bin/python}
MODEL=${MW_MODEL_ID:-accounts/fireworks/models/glm-5p3-flash}
EVOLVER_MODEL=${MW_EVOLVER_MODEL:-accounts/fireworks/models/glm-5p3-flash}
METER=${MW_WIRE_METER:-$FRAMEWORK/harnessadapter/runtime/wire_meter.py}
KEY_FILE=${MW_KEY_FILE:-$CODE/metaworld/.fireworks}
META_PORT=$((WM_PORT+1))
TASKDIR=$FRAMEWORK/examples/tasks/servelearnbench
CONFIG=$HERE/$SCENARIO.yaml
WD="$RUNS/$TAG/task"

[ -f "$CONFIG" ] || { echo "REFUSING: no config $CONFIG"; exit 1; }
[ -f "$METER" ] || { echo "REFUSING: no wire meter at $METER (MW_WIRE_METER)"; exit 1; }
"$PY" -c "import servelearnbench" 2>/dev/null \
  || { echo "REFUSING: servelearnbench is not installed: $PY -m pip install git+https://github.com/Infini-AI-Lab/ServeLearnBench"; exit 1; }
# The scenario as a task (tasks.jsonl, splits, windows.json, the reference documents), exported once.
if ! grep -q "\"scenario\": \"$SCENARIO\"" "$TASKDIR/tasks.jsonl" 2>/dev/null; then
  BLOCK=$(grep -E '^\s*step_size:' "$CONFIG" | head -1 | sed -E 's/.*step_size:\s*([0-9]+).*/\1/')
  "$PY" "$TASKDIR/prepare.py" --scenario "$SCENARIO" --block "${BLOCK:-12}"
fi
if [ -n "${FIREWORKS_API_KEY:-}" ]; then KEY=$FIREWORKS_API_KEY
elif [ -f "$KEY_FILE" ]; then KEY=$(cat "$KEY_FILE")
else echo "REFUSING: set FIREWORKS_API_KEY or put the key in $KEY_FILE (MW_KEY_FILE)"; exit 1; fi
export HA_FW_KEY=$KEY SLB_API_KEY=$KEY
export SLB_MODEL=$MODEL SLB_REASONING_EFFORT=high SLB_METHOD=mwm SLB_WORKDIR=$WD
export PYTHONPATH=$FRAMEWORK${PYTHONPATH:+:$PYTHONPATH}

start_meter() {  # port ledger log [extra env...]
  local port=$1 ledger=$2 log=$3; shift 3
  if ! curl -s -m 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    env "$@" PORT=$port LEDGER="$ledger" UPSTREAM=https://api.fireworks.ai \
      setsid nohup python3 "$METER" > "$log" 2>&1 < /dev/null &
    echo "wire meter pid $! port $port"; sleep 2
  fi
}
mkdir -p "$RUNS/$TAG"
start_meter $WM_PORT "$RUNS/$TAG/wire_ledger.json" "$RUNS/$TAG/wire_meter.log"

if [ "$MODE" = test ]; then
  [ -d "$RUNS/$TAG/$TAG" ] || { echo "REFUSING: no training run at $RUNS/$TAG/$TAG"; exit 1; }
  export SLB_BASE_URL="http://127.0.0.1:$WM_PORT/${TAG}_test/inference/v1"
  "$PY" "$TASKDIR/windows.py" --config "$CONFIG" --run "$TAG" ${MW_SMOKE:+--smoke} "trainer.output_dir=$RUNS/$TAG"
  "$FRAMEWORK/.venv/bin/slb" score "$WD/results/${SCENARIO}_mwm.jsonl" 2>/dev/null \
    || "$PY" -m servelearnbench.cli score "$WD/results/${SCENARIO}_mwm.jsonl"
  exit 0
fi
[ "$MODE" = train ] || { echo "usage: launch.sh <scenario> [train|test]"; exit 1; }

command -v codex >/dev/null || { echo "REFUSING: codex CLI not on PATH"; exit 1; }
command -v bwrap >/dev/null || { echo "REFUSING: bubblewrap (bwrap) not on PATH"; exit 1; }
# A relative split path is relative to the shell, not to the config file (where the Trainer resolves it).
[ -n "${MW_TRAIN_SPLIT:-}" ] && { [ -f "$MW_TRAIN_SPLIT" ] || { echo "REFUSING: no split $MW_TRAIN_SPLIT"; exit 1; }
                                 MW_TRAIN_SPLIT=$(readlink -f "$MW_TRAIN_SPLIT"); }
[ -e "$RUNS/$TAG/$TAG" ] && { echo "REFUSING: $RUNS/$TAG/$TAG exists (choose another MW_TAG)"; exit 1; }
[ -e "$WD" ] && { echo "REFUSING: $WD exists"; exit 1; }
start_meter $META_PORT "$RUNS/$TAG/meta_wire_ledger.json" "$RUNS/$TAG/meta_wire_meter.log" \
  REWRITE_DEVELOPER_ROLE=1 REWRITE_STRIP_SERVER_TOOLS=1 REWRITE_REPAIR_TOOL_ARGS=1 DUMP_REJECTED="$RUNS/$TAG/rejected"

stamp() { echo "$(git -C "$1" rev-parse --short HEAD)$([ -n "$(git -C "$1" status --porcelain)" ] && echo -dirty || true)"; }
HEAD=$(stamp "$FRAMEWORK")
SLB_VERSION=$("$PY" -c "import servelearnbench; print(servelearnbench.__version__)")
mkdir -p "$WD/evidence"
export SLB_BASE_URL="http://127.0.0.1:$WM_PORT/${TAG}/inference/v1"
ARGS="[-c, model_provider=\"fireworks\", -c, model_providers.fireworks.name=\"fireworks\", -c, model_providers.fireworks.base_url=\"http://127.0.0.1:$META_PORT/${TAG}_meta/inference/v1\", -c, model_providers.fireworks.env_key=\"HA_FW_KEY\", -c, model_providers.fireworks.wire_api=\"responses\", -c, model_reasoning_effort=\"high\"]"
LOG="$RUNS/$TAG/run.log"
echo "=== setting stamp: scenario=$SCENARIO method=mwm framework=$HEAD servelearnbench=$SLB_VERSION" \
     "host_model=$MODEL host_effort=high evolver=mwm evolver_model=$EVOLVER_MODEL evolver_effort=high" \
     "meter=$WM_PORT/$META_PORT shuffle=false train=${MW_TRAIN_SPLIT:-splits/serving.txt} ===" > "$LOG"
setsid nohup "$PY" -m harnessadapter.cli train --config "$CONFIG" \
    "trainer.run_name=$TAG" "trainer.output_dir=$RUNS/$TAG" \
    ${MW_TRAIN_SPLIT:+"data.train=$MW_TRAIN_SPLIT"} \
    "evolver.params.model=$EVOLVER_MODEL" "evolver.params.evidence=$WD/evidence" "evolver.params.args=$ARGS" \
    "evolver_access.environment=[$WD/evidence]" \
    >> "$LOG" 2>&1 < /dev/null &
TRAIN_PID=$!
echo "train -> pid $TRAIN_PID log $LOG"
# The tests follow the training on their own: this waiter runs `test` when the Trainer exits.
setsid nohup bash -c "while kill -0 $TRAIN_PID 2>/dev/null; do sleep 60; done; \
  MW_TAG=$TAG WM_PORT=$WM_PORT MW_RUNS=$RUNS MW_SMOKE=${MW_TRAIN_SPLIT:+1} exec \"$0\" \"$SCENARIO\" test" > "$RUNS/$TAG/test.log" 2>&1 < /dev/null &
echo "tests -> after training, log $RUNS/$TAG/test.log"
echo "run directory: $RUNS/$TAG   (progress: $RUNS/$TAG/$TAG/steps.jsonl; scores: $RUNS/$TAG/test.log)"
