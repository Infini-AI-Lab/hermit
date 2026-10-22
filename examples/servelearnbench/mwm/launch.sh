#!/bin/bash
# MWM on a ServeLearnBench scenario, host and learner on the models the environment names (default GLM 5.3 flash on
# Fireworks, reasoning effort high):
#
#   examples/servelearnbench/mwm/launch.sh retail_l3          # train on the serving stream (background)
#   examples/servelearnbench/mwm/launch.sh retail_l3 test     # the per-window tests, then `slb score`
#
# `train` starts two wire meters (the host's on WM_PORT, the learner's on WM_PORT+1), each in front of its model's
# backend, and one `hermit train` in the background, logging to <runs>/<tag>/run.log. `test` replays every
# window's held-out tests on the adapter the run had at that window (examples/servelearnbench/utils/windows.py) and
# scores the result file; it can run while training is still going (windows not trained yet are skipped) and again
# later.
#
# Settings (environment variables, all optional):
#   MW_MODEL         the host's and the learner's model, a backend reference <backend>/<model id>
#                    (`hermit backends list`; default fireworks/accounts/fireworks/models/glm-5p3-flash);
#                    MW_HOST_MODEL and MW_EVOLVER_MODEL name either alone. The learner's backend must serve the
#                    Responses API (Codex speaks nothing else; vLLM and SGLang do). For example:
#                    MW_MODEL=vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 VLLM_BASE_URL=http://127.0.0.1:8102/v1
#   the backend's key variable (FIREWORKS_API_KEY, ...), or MW_KEY_FILE holding the key (default
#                    ~/code/metaworld/.fireworks, read for the fireworks backend only)
#   MW_TAG           the run's name (default slb_<scenario>)        WM_PORT  the host meter's port (default 3451)
#   MW_RUNS          where runs go (default ~/code/metaworld-runs)  MW_TRAIN_SPLIT  another train split (a smoke)
#   MW_EFFORT        the reasoning effort of host and learner (default high)
#   MW_WIRE_METER    the wire meter script (default: the framework's hermit/runtime/wire_meter.py)
#   MW_METHOD        another method's config, examples/servelearnbench/<method>/<scenario>.yaml (default mwm);
#                    examples/servelearnbench/prompt_only/launch.sh sets it
set -eu
SCENARIO=${1:?usage: launch.sh <scenario> [train|test]}
MODE=${2:-train}
HERE=$(cd "$(dirname "$0")" && pwd)
FRAMEWORK=$(cd "$HERE/../../.." && pwd)
CODE=${MW_CODE:-$HOME/code}
RUNS=${MW_RUNS:-$CODE/metaworld-runs}
METHOD=${MW_METHOD:-mwm}
TAG=${MW_TAG:-slb_$SCENARIO}
WM_PORT=${WM_PORT:-3451}
PY=${MW_FRAMEWORK_PYTHON:-$FRAMEWORK/.venv/bin/python}
[ -z "${MW_MODEL_ID:-}" ] || { echo "REFUSING: MW_MODEL_ID is gone; set MW_MODEL (or MW_HOST_MODEL) to a <backend>/<model id> reference"; exit 1; }
MODEL=${MW_MODEL:-fireworks/accounts/fireworks/models/glm-5p3-flash}
HOST_MODEL=${MW_HOST_MODEL:-$MODEL}
EVOLVER_MODEL=${MW_EVOLVER_MODEL:-$MODEL}
EFFORT=${MW_EFFORT:-high}
METER=${MW_WIRE_METER:-$FRAMEWORK/hermit/runtime/wire_meter.py}
META_PORT=$((WM_PORT+1))
TASKDIR=$FRAMEWORK/examples/servelearnbench/utils
CONFIG=$FRAMEWORK/examples/servelearnbench/$METHOD/$SCENARIO.yaml
WD="$RUNS/$TAG/task"
export PYTHONPATH=$FRAMEWORK${PYTHONPATH:+:$PYTHONPATH}

[ -f "$CONFIG" ] || { echo "REFUSING: no config $CONFIG"; exit 1; }
[ -f "$METER" ] || { echo "REFUSING: no wire meter at $METER (MW_WIRE_METER)"; exit 1; }
"$PY" -c "import servelearnbench" 2>/dev/null \
  || { echo "REFUSING: servelearnbench is not installed: $PY -m pip install git+https://github.com/Infini-AI-Lab/ServeLearnBench"; exit 1; }
# The scenario as a task (tasks.jsonl, splits, windows.json, the reference documents), exported once.
if ! grep -q "\"scenario\": \"$SCENARIO\"" "$TASKDIR/tasks.jsonl" 2>/dev/null; then
  BLOCK=$(grep -E '^\s*step_size:' "$CONFIG" | head -1 | sed -E 's/.*step_size:\s*([0-9]+).*/\1/')
  "$PY" "$TASKDIR/prepare.py" --scenario "$SCENARIO" --block "${BLOCK:-12}"
fi

# The two models resolved on their backends (the shell form of hermit.backends: HOST_* and EVOLVER_*).
resolve() { "$PY" -m hermit.backends shell "$2" | sed "s/^HA_/$1_/"; }
# HOST_MODEL stays the reference; HOST_MODEL_ID is the model's id on its backend (same for EVOLVER_*).
eval "$(resolve HOST "$HOST_MODEL")"; [ -n "${HOST_MODEL_ID:-}" ] || { echo "REFUSING: cannot resolve $HOST_MODEL"; exit 1; }
eval "$(resolve EVOLVER "$EVOLVER_MODEL")"; [ -n "${EVOLVER_MODEL_ID:-}" ] || { echo "REFUSING: cannot resolve $EVOLVER_MODEL"; exit 1; }
need_key() {  # the backend's key variable: from the environment, else MW_KEY_FILE (the key of MW_KEY_BACKEND, default
  # fireworks); required unless the backend says not
  local var=$1 required=$2 backend=$3 file=${MW_KEY_FILE:-$CODE/metaworld/.fireworks}
  [ -n "$var" ] || return 0
  if [ -z "${!var:-}" ] && [ -f "$file" ] && [ "$backend" = "${MW_KEY_BACKEND:-fireworks}" ]; then
    export "$var=$(cat "$file")"
  fi
  [ "$required" = 1 ] && [ -z "${!var:-}" ] && { echo "REFUSING: $var is not set (the key of backend $backend)"; exit 1; }
  return 0
}
need_key "$HOST_API_KEY_ENV" "$HOST_KEY_REQUIRED" "$HOST_BACKEND"
need_key "$EVOLVER_API_KEY_ENV" "$EVOLVER_KEY_REQUIRED" "$EVOLVER_BACKEND"
# The host is the benchmark's own agent loop: it reads its model, endpoint and key from SLB_* (a placeholder key
# for a server that needs none).
HOST_KEY=""; [ -n "$HOST_API_KEY_ENV" ] && HOST_KEY=${!HOST_API_KEY_ENV:-}
export SLB_MODEL=$HOST_MODEL_ID SLB_API_KEY=${HOST_KEY:-local}
export SLB_REASONING_EFFORT=$EFFORT SLB_METHOD=$METHOD SLB_WORKDIR=$WD

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

if [ "$MODE" = test ]; then
  [ -d "$RUNS/$TAG/$TAG" ] || { echo "REFUSING: no training run at $RUNS/$TAG/$TAG"; exit 1; }
  export SLB_BASE_URL="http://127.0.0.1:$WM_PORT/${TAG}_test$HOST_PATH"
  "$PY" "$TASKDIR/windows.py" --config "$CONFIG" --run "$TAG" --method "$METHOD" ${MW_SMOKE:+--smoke} \
      "trainer.output_dir=$RUNS/$TAG"
  "$FRAMEWORK/.venv/bin/slb" score "$WD/results/${SCENARIO}_${METHOD}.jsonl" 2>/dev/null \
    || "$PY" -m servelearnbench.cli score "$WD/results/${SCENARIO}_${METHOD}.jsonl"
  exit 0
fi
[ "$MODE" = train ] || { echo "usage: launch.sh <scenario> [train|test]"; exit 1; }

command -v codex >/dev/null || { echo "REFUSING: codex CLI not on PATH"; exit 1; }
command -v bwrap >/dev/null || { echo "REFUSING: bubblewrap (bwrap) not on PATH"; exit 1; }
case " $EVOLVER_APIS " in *" responses "*) ;; *) echo "REFUSING: backend $EVOLVER_BACKEND does not serve the Responses API, which codex needs"; exit 1;; esac
# A relative split path is relative to the shell, not to the config file (where the Trainer resolves it).
[ -n "${MW_TRAIN_SPLIT:-}" ] && { [ -f "$MW_TRAIN_SPLIT" ] || { echo "REFUSING: no split $MW_TRAIN_SPLIT"; exit 1; }
                                 MW_TRAIN_SPLIT=$(readlink -f "$MW_TRAIN_SPLIT"); }
[ -e "$RUNS/$TAG/$TAG" ] && { echo "REFUSING: $RUNS/$TAG/$TAG exists (choose another MW_TAG)"; exit 1; }
[ -e "$WD" ] && { echo "REFUSING: $WD exists"; exit 1; }
start_meter $META_PORT "$EVOLVER_ORIGIN" "$RUNS/$TAG/meta_wire_ledger.json" "$RUNS/$TAG/meta_wire_meter.log" \
  REWRITE_DEVELOPER_ROLE=$EVOLVER_REWRITE_DEVELOPER_ROLE REWRITE_STRIP_SERVER_TOOLS=1 REWRITE_REPAIR_TOOL_ARGS=1 \
  DUMP_REJECTED="$RUNS/$TAG/rejected"

stamp() { echo "$(git -C "$1" rev-parse --short HEAD)$([ -n "$(git -C "$1" status --porcelain)" ] && echo -dirty || true)"; }
HEAD=$(stamp "$FRAMEWORK")
SLB_VERSION=$("$PY" -c "import servelearnbench; print(servelearnbench.__version__)")
mkdir -p "$WD/evidence"
export SLB_BASE_URL="http://127.0.0.1:$WM_PORT/${TAG}$HOST_PATH"
# The learner's model through its meter: the backend copied to the metered URL, the Evolver bound to it (the
# Trainer derives codex's provider settings from the binding and passes the key variable into the sandbox).
LEARNER="{from: '$EVOLVER_BACKEND', base_url: 'http://127.0.0.1:$META_PORT/${TAG}_meta$EVOLVER_PATH'}"
LOG="$RUNS/$TAG/run.log"
EVOLVER=$(sed -n '/^evolver:/,/^[a-z]/p' "$CONFIG" | grep -E '^\s+name:' | head -1 | sed -E 's/.*name:\s*([^ #]+).*/\1/')
[ "$METHOD" = mwm ] && MWM_ARGS=("evolver.params.evidence=$WD/evidence" "evolver_access.environment=[$WD/evidence]") || MWM_ARGS=()
echo "=== setting stamp: scenario=$SCENARIO method=$METHOD framework=$HEAD servelearnbench=$SLB_VERSION" \
     "host_model=$HOST_MODEL host_effort=$EFFORT evolver=$EVOLVER evolver_model=$EVOLVER_MODEL evolver_effort=$EFFORT" \
     "meter=$WM_PORT/$META_PORT shuffle=false train=${MW_TRAIN_SPLIT:-splits/serving.txt} ===" > "$LOG"
setsid nohup "$PY" -m hermit.cli train --config "$CONFIG" \
    "trainer.run_name=$TAG" "trainer.output_dir=$RUNS/$TAG" \
    ${MW_TRAIN_SPLIT:+"data.train=$MW_TRAIN_SPLIT"} \
    "backends.learner=$LEARNER" "evolver.params.model=learner/$EVOLVER_MODEL_ID" \
    "evolver.params.args=[-c, model_reasoning_effort=\"$EFFORT\"]" "${MWM_ARGS[@]}" \
    >> "$LOG" 2>&1 < /dev/null &
TRAIN_PID=$!
echo "train -> pid $TRAIN_PID log $LOG"
# The tests follow the training on their own: this waiter runs `test` when the Trainer exits.
setsid nohup bash -c "while kill -0 $TRAIN_PID 2>/dev/null; do sleep 60; done; \
  MW_TAG=$TAG WM_PORT=$WM_PORT MW_RUNS=$RUNS MW_METHOD=$METHOD MW_MODEL=$MODEL MW_HOST_MODEL=$HOST_MODEL MW_EVOLVER_MODEL=$EVOLVER_MODEL \
  MW_SMOKE=${MW_TRAIN_SPLIT:+1} exec \"$0\" \"$SCENARIO\" test" > "$RUNS/$TAG/test.log" 2>&1 < /dev/null &
echo "tests -> after training, log $RUNS/$TAG/test.log"
echo "run directory: $RUNS/$TAG   (progress: $RUNS/$TAG/$TAG/steps.jsonl; scores: $RUNS/$TAG/test.log)"
