#!/bin/bash
# MWM on CL-Bench through the framework. Host = the host model (effort high, native tools) over the mwm adapter;
# learner = the mwm Evolver (codex + the learner model through a metering relay), one session per block plus the
# bootstrap session in the first step.
#
#   MW_TAG=fw6_mwm_flash WM_PORT=3401 MW_ORDERS="0" ./launch.sh
#   MW_TAG=fw6_poker_mwm_flash WM_PORT=3411 MW_ORDERS="0" ./launch.sh
#   MW_MODEL=vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 VLLM_BASE_URL=http://127.0.0.1:8102/v1 \
#     MW_TAG=mwm_poker_local WM_PORT=3431 MW_ORDERS="0" ./launch.sh               # a local vLLM server
#
# Models are backend references, <backend>/<model id> (`hermit backends list`; hermit/backends.py):
# MW_MODEL names the host's and the learner's model alike, MW_HOST_MODEL and MW_EVOLVER_MODEL either alone (default
# GLM 5.3 Flash on Fireworks). The backend's key variable (FIREWORKS_API_KEY, ...) must be set, or MW_KEY_FILE holds
# the key (default ~/code/metaworld/.fireworks, read for the fireworks backend only). The learner's backend must
# serve the Responses API (Codex speaks nothing else; vLLM and SGLang do).
#
# Per tag: a host wire meter (WM_PORT) and a learner wire meter with the rewrites codex needs (WM_PORT+1), each in
# front of its model's backend. Per order: a task directory <runs>/<tag>/order<i>/task (evidence store, host workspace,
# current_db, outcomes.jsonl) and one `hermit train` run, <runs>/<tag>/<tag>-o<i>. Run the venue's prepare.py
# first. MW_TRAIN_SPLIT names another split file (a prefix of the order's stream, for a smoke run of one order).
set -eu
VENUE=database
HERE=$(cd "$(dirname "$0")" && pwd)
FRAMEWORK=$(cd "$HERE/../../.." && pwd)
CODE=${MW_CODE:-$HOME/code}
CLBENCH=${MWM_CLBENCH:-$CODE/clbench}
METAWORLD=${MWM_METAWORLD:-$CODE/metaworld}
RUNS=${MW_RUNS:-$CODE/metaworld-runs}
TAG=${MW_TAG:?set MW_TAG}
WM_PORT=${WM_PORT:?set WM_PORT}
# Any Python with the framework's dependencies; PYTHONPATH below makes it import THIS checkout.
PY=${MW_FRAMEWORK_PYTHON:-$FRAMEWORK/.venv/bin/python}
METER=${MW_WIRE_METER:-$FRAMEWORK/hermit/runtime/wire_meter.py}
[ -z "${MW_MODEL_ID:-}" ] || { echo "REFUSING: MW_MODEL_ID is gone; set MW_MODEL (or MW_HOST_MODEL) to a <backend>/<model id> reference"; exit 1; }
MODEL=${MW_MODEL:-fireworks/accounts/fireworks/models/glm-5p3-flash}
HOST_MODEL=${MW_HOST_MODEL:-$MODEL}
EVOLVER_MODEL=${MW_EVOLVER_MODEL:-$MODEL}
EFFORT=${MW_EFFORT:-high}
META_PORT=$((WM_PORT+1))
TASKDIR=$FRAMEWORK/examples/clbench_database/utils
export PYTHONPATH=$FRAMEWORK${PYTHONPATH:+:$PYTHONPATH}

# The two models resolved on their backends (the shell form of hermit.backends: HOST_* and EVOLVER_*).
resolve() { "$PY" -m hermit.backends shell "$2" | sed "s/^HA_/$1_/"; }
# HOST_MODEL stays the reference; HOST_MODEL_ID is the model's id on its backend (same for EVOLVER_*).
eval "$(resolve HOST "$HOST_MODEL")"; [ -n "${HOST_MODEL_ID:-}" ] || { echo "REFUSING: cannot resolve $HOST_MODEL"; exit 1; }
eval "$(resolve EVOLVER "$EVOLVER_MODEL")"; [ -n "${EVOLVER_MODEL_ID:-}" ] || { echo "REFUSING: cannot resolve $EVOLVER_MODEL"; exit 1; }
need_key() {  # the backend's key variable: from the environment, else MW_KEY_FILE (the key of MW_KEY_BACKEND, default
  # fireworks); required unless the backend says not
  local var=$1 required=$2 backend=$3 file=${MW_KEY_FILE:-$METAWORLD/.fireworks}
  [ -n "$var" ] || return 0
  if [ -z "${!var:-}" ] && [ -f "$file" ] && [ "$backend" = "${MW_KEY_BACKEND:-fireworks}" ]; then
    export "$var=$(cat "$file")"
  fi
  [ "$required" = 1 ] && [ -z "${!var:-}" ] && { echo "REFUSING: $var is not set (the key of backend $backend)"; exit 1; }
  return 0
}
need_key "$HOST_API_KEY_ENV" "$HOST_KEY_REQUIRED" "$HOST_BACKEND"
need_key "$EVOLVER_API_KEY_ENV" "$EVOLVER_KEY_REQUIRED" "$EVOLVER_BACKEND"
case " $EVOLVER_APIS " in *" responses "*) ;; *) echo "REFUSING: backend $EVOLVER_BACKEND does not serve the Responses API, which codex needs"; exit 1;; esac
# The host calls through clbench's litellm: its id, its key variable (a placeholder for a keyless server), its prices.
export MWM_HOST_MODEL=$HOST_LITELLM_MODEL
HOST_KEY=""; [ -n "$HOST_API_KEY_ENV" ] && HOST_KEY=${!HOST_API_KEY_ENV:-}
[ -n "${!HOST_LITELLM_KEY_ENV:-}" ] || export "$HOST_LITELLM_KEY_ENV=${HOST_KEY:-local}"
[ -n "$HOST_PRICE_INPUT" ] && export MW_COST_IN_PER_M=$HOST_PRICE_INPUT MW_COST_OUT_PER_M=$HOST_PRICE_OUTPUT
export CLB_SKIP_BASELINE=1 CLB_REQUEST_TIMEOUT=600 CLB_REASONING_EFFORT=$EFFORT
export MWM_CLBENCH=$CLBENCH MWM_METAWORLD=$METAWORLD
if [ "$VENUE" = database ]; then
  export CLB_SCORING=accuracy CLB_QUERY_BUDGET=${CLB_QUERY_BUDGET:-0} MW_META_SQL=1
  grep -q 'SCORING_UNLIMITED' "$CLBENCH/src/tasks/database_exploration/prompts.py" \
    || { echo "REFUSING: task prompt lacks the accuracy scoring sentence"; exit 1; }
fi

command -v codex >/dev/null || { echo "REFUSING: codex CLI not on PATH"; exit 1; }
[ -f "$TASKDIR/tasks.jsonl" ] || { echo "REFUSING: run $TASKDIR/prepare.py first"; exit 1; }

start_meter() {  # port upstream ledger log [extra env...]: a meter already on the port must face the same upstream
  local port=$1 upstream=$2 ledger=$3 log=$4 health; shift 4
  if health=$(curl -s -m 2 "http://127.0.0.1:$port/health" 2>/dev/null); then
    case "$health" in *"\"upstream\": \"$upstream\""*) echo "wire meter already on port $port -> $upstream";;
      *) echo "REFUSING: port $port has a wire meter facing another or an unknown upstream ($health); choose another WM_PORT"; exit 1;; esac
  else
    env "$@" PORT=$port LEDGER="$ledger" UPSTREAM="$upstream" \
      setsid nohup "$PY" "$METER" > "$log" 2>&1 < /dev/null &
    echo "wire meter pid $! port $port -> $upstream"; sleep 2
  fi
}
mkdir -p "$RUNS/$TAG"
start_meter $WM_PORT "$HOST_ORIGIN" "$RUNS/$TAG/wire_ledger.json" "$RUNS/$TAG/wire_meter.log"
start_meter $META_PORT "$EVOLVER_ORIGIN" "$RUNS/$TAG/meta_wire_ledger.json" "$RUNS/$TAG/meta_wire_meter.log" \
  REWRITE_DEVELOPER_ROLE=$EVOLVER_REWRITE_DEVELOPER_ROLE REWRITE_STRIP_SERVER_TOOLS=1 REWRITE_REPAIR_TOOL_ARGS=1 \
  DUMP_REJECTED="$RUNS/$TAG/rejected"

stamp() { echo "$(git -C "$1" rev-parse --short HEAD)$([ -n "$(git -C "$1" status --porcelain)" ] && echo -dirty || true)"; }
HEAD=$(stamp "$FRAMEWORK"); CLB_HEAD=$(stamp "$CLBENCH"); MW_HEAD=$(stamp "$METAWORLD")

PIDS=()
for i in ${MW_ORDERS:-0}; do
  WD="$RUNS/$TAG/order$i/task"
  [ -e "$RUNS/$TAG/$TAG-o$i" ] && { echo "REFUSING: $RUNS/$TAG/$TAG-o$i exists"; exit 1; }
  [ -e "$WD" ] && { echo "REFUSING: $WD exists"; exit 1; }
  mkdir -p "$WD/evidence"
  : > "$WD/current_db"
  # The store's README exists before the bootstrap session, as natively (the wrapper wrote it before block 0).
  "$PY" -m hermit.algorithms.evolvers.mwm.evidence.readme --evidence "$WD/evidence" --from "$HERE/../utils/task-info/mwm/contract/env_evidence.md"
  if [ "$VENUE" = database ]; then   # the first question's database, registered before the bootstrap, as natively
    "$CLBENCH/.venv/bin/python" "$FRAMEWORK/examples/clbench_database/utils/agent/database_runner.py" --register-first $i \
      --workdir "$WD" --clbench "$CLBENCH" --framework "$FRAMEWORK"
  fi
  # The learner's model through its meter: the backend copied to the metered URL, the Evolver bound to it (the
  # Trainer derives codex's provider settings from the binding and passes the key variable into the sandbox).
  LEARNER="{from: '$EVOLVER_BACKEND', base_url: 'http://127.0.0.1:$META_PORT/${TAG}_o${i}_meta$EVOLVER_PATH'}"
  LOG="$RUNS/$TAG/order$i/run.log"
  echo "=== setting stamp: venue=$VENUE method=mwm framework=$HEAD clbench=$CLB_HEAD metaworld=$MW_HEAD" \
       "host_model=$HOST_MODEL host_effort=$EFFORT evolver=mwm evolver_model=$EVOLVER_MODEL evolver_effort=$EFFORT" \
       "meter=$META_PORT shuffle=false bucket=${TAG}_o$i ===" > "$LOG"
  MWM_TASK_WORKDIR="$WD" CLB_API_BASE="http://127.0.0.1:$WM_PORT/${TAG}_o$i$HOST_PATH" \
    setsid -w nohup "$PY" -m hermit.cli train --config "$HERE/run.yaml" \
      "trainer.run_name=$TAG-o$i" "trainer.output_dir=$RUNS/$TAG" \
      "data.train=${MW_TRAIN_SPLIT:-$TASKDIR/splits/order$i.txt}" \
      "backends.learner=$LEARNER" "evolver.params.model=learner/$EVOLVER_MODEL_ID" \
      "evolver.params.args=[-c, model_reasoning_effort=\"$EFFORT\"]" \
      "evolver.params.evidence=$WD/evidence" "evolver_access.environment=[$WD/evidence]" \
      >> "$LOG" 2>&1 < /dev/null &
  PIDS+=("$!")
  echo "order $i -> pid $! log $LOG"
  sleep 3
done

# MW_WAIT=1 (docker/offline.sh run sets it): wait for every training started above and exit non-zero when one failed,
# so the caller sees the trainings' outcome, not only this launcher's.
if [ "${MW_WAIT:-0}" = 1 ]; then
  failed=0
  for pid in "${PIDS[@]}"; do wait "$pid" || failed=$((failed + 1)); done
  [ "$failed" = 0 ] || { echo "$failed of ${#PIDS[@]} training run(s) failed: see each order's run.log" >&2; exit 1; }
fi
