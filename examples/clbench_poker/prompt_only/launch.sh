#!/bin/bash
# The prompt-only baseline on CL-Bench through the framework: the host of ../mwm/launch.sh (GLM flash,
# effort high, native tools) over a prompt-only adapter (ADAPTER.md as its context, no tools); learner = the built-in
# Codex Evolver (codex + GLM flash through a metering relay), one fresh session per step, no bootstrap.
#
#   MW_TAG=po_db_flash    WM_PORT=3441 MW_ORDERS="0" ./launch.sh
#   MW_TAG=po_poker_flash WM_PORT=3451 MW_ORDERS="0" ./launch.sh
#
# Per tag: a host wire meter (WM_PORT) and a learner wire meter with the rewrites codex needs against Fireworks
# (WM_PORT+1). Per order: a task directory <runs>/<tag>/order<i>/task (host workspace, current_db, the host's
# evidence records, outcomes.jsonl) and one `harnessadapter train` run, <runs>/<tag>/<tag>-o<i>. Run the venue's
# prepare.py first. MW_TRAIN_SPLIT names another split file (a prefix of the order's stream, for a smoke run).
set -eu
VENUE=poker
HERE=$(cd "$(dirname "$0")" && pwd)
FRAMEWORK=$(cd "$HERE/../../.." && pwd)
CODE=${MW_CODE:-$HOME/code}
CLBENCH=${MWM_CLBENCH:-$CODE/clbench}
METAWORLD=${MWM_METAWORLD:-$CODE/metaworld}
RUNS=${MW_RUNS:-$CODE/metaworld-runs}
TAG=${MW_TAG:?set MW_TAG}
WM_PORT=${WM_PORT:?set WM_PORT}
# Any Python with the framework's dependencies; PYTHONPATH below makes it import THIS checkout.
PY=${MW_FRAMEWORK_PYTHON:-$CODE/metaHarnessAdapter/.venv/bin/python}
MODEL=${MW_MODEL_ID:-fireworks_ai/accounts/fireworks/models/glm-5p3-flash}
EVOLVER_MODEL=${MW_EVOLVER_MODEL:-accounts/fireworks/models/glm-5p3-flash}
META_PORT=$((WM_PORT+1))
TASKDIR=$FRAMEWORK/examples/clbench_poker/utils

KEY=$(cat "$METAWORLD/.fireworks")
export FIREWORKS_AI_API_KEY=$KEY FIREWORKS_API_KEY=$KEY HA_FW_KEY=$KEY
export CLB_SKIP_BASELINE=1 CLB_REQUEST_TIMEOUT=600 CLB_REASONING_EFFORT=high
export MWM_CLBENCH=$CLBENCH MWM_METAWORLD=$METAWORLD MWM_HOST_MODEL=$MODEL
export PYTHONPATH=$FRAMEWORK${PYTHONPATH:+:$PYTHONPATH}

command -v codex >/dev/null || { echo "REFUSING: codex CLI not on PATH"; exit 1; }
[ -f "$TASKDIR/tasks.jsonl" ] || { echo "REFUSING: run $TASKDIR/prepare.py first"; exit 1; }

start_meter() {  # port ledger log [extra env...]
  local port=$1 ledger=$2 log=$3; shift 3
  if ! curl -s -m 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    env "$@" PORT=$port LEDGER="$ledger" UPSTREAM=https://api.fireworks.ai \
      setsid nohup python3 "$METAWORLD/mwm/wire_meter.py" > "$log" 2>&1 < /dev/null &
    echo "wire meter pid $! port $port"; sleep 2
  fi
}
mkdir -p "$RUNS/$TAG"
start_meter $WM_PORT "$RUNS/$TAG/wire_ledger.json" "$RUNS/$TAG/wire_meter.log"
start_meter $META_PORT "$RUNS/$TAG/meta_wire_ledger.json" "$RUNS/$TAG/meta_wire_meter.log" \
  REWRITE_DEVELOPER_ROLE=1 REWRITE_STRIP_SERVER_TOOLS=1 REWRITE_REPAIR_TOOL_ARGS=1 DUMP_REJECTED="$RUNS/$TAG/rejected"

stamp() { echo "$(git -C "$1" rev-parse --short HEAD)$([ -n "$(git -C "$1" status --porcelain)" ] && echo -dirty || true)"; }
HEAD=$(stamp "$FRAMEWORK"); CLB_HEAD=$(stamp "$CLBENCH"); MW_HEAD=$(stamp "$METAWORLD")

for i in ${MW_ORDERS:-0}; do
  WD="$RUNS/$TAG/order$i/task"
  [ -e "$RUNS/$TAG/$TAG-o$i" ] && { echo "REFUSING: $RUNS/$TAG/$TAG-o$i exists"; exit 1; }
  [ -e "$WD" ] && { echo "REFUSING: $WD exists"; exit 1; }
  mkdir -p "$WD/evidence"
  : > "$WD/current_db"
  # The host runners keep the venue's record store under the task directory (the learner here reads the
  # framework's learning material, not this store); the database runner needs the first question's database.
  "$PY" -m harnessadapter.algorithms.evolvers.mwm.evidence.readme --evidence "$WD/evidence" --from "$HERE/../utils/task-info/mwm/contract/env_evidence.md"
  ARGS="[-c, model_provider=\"fireworks\", -c, model_providers.fireworks.name=\"fireworks\", -c, model_providers.fireworks.base_url=\"http://127.0.0.1:$META_PORT/${TAG}_o${i}_meta/inference/v1\", -c, model_providers.fireworks.env_key=\"HA_FW_KEY\", -c, model_providers.fireworks.wire_api=\"responses\", -c, model_reasoning_effort=\"high\", -c, model_context_window=1048576]"
  LOG="$RUNS/$TAG/order$i/run.log"
  echo "=== setting stamp: venue=$VENUE method=prompt-only framework=$HEAD clbench=$CLB_HEAD metaworld=$MW_HEAD" \
       "host_model=$MODEL host_effort=high evolver=codex evolver_model=$EVOLVER_MODEL evolver_effort=high" \
       "meter=$META_PORT shuffle=false bucket=${TAG}_o$i ===" > "$LOG"
  MWM_TASK_WORKDIR="$WD" CLB_API_BASE="http://127.0.0.1:$WM_PORT/${TAG}_o$i/inference/v1" \
    setsid nohup "$PY" -m harnessadapter.cli train --config "$HERE/run.yaml" \
      "trainer.run_name=$TAG-o$i" "trainer.output_dir=$RUNS/$TAG" \
      "data.train=${MW_TRAIN_SPLIT:-$TASKDIR/splits/order$i.txt}" \
      "evolver.params.model=$EVOLVER_MODEL" "evolver.params.args=$ARGS" \
      >> "$LOG" 2>&1 < /dev/null &
  echo "order $i -> pid $! log $LOG"
  sleep 3
done
