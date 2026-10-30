#!/bin/bash
# ReHarness (Xiao et al., arXiv:2610.12668) on CL-Bench through the framework: the host of ../mwm/launch.sh (the host
# model, effort high, native tools) over a reharness adapter (a bank of reflections; the host's context is ADAPTER.md
# plus the top-k reflections retrieved for the instance's first prompt, no tools for the model); learner = the
# reharness Evolver, one step per instance: its revision and reflection agents are the run's `llm` tool, bound to the
# learner model through a metering relay; the embeddings of the instances' prompts go through a relay of their own.
# No Codex: the Evolver runs in the Trainer's process, and its bootstrap (step 0) only writes the bank's index.json.
#
#   MW_TAG=rh_db_flash    WM_PORT=3481 MW_ORDERS="0" ./launch.sh
#   MW_TAG=rh_poker_flash WM_PORT=3491 MW_ORDERS="0" ./launch.sh
#   MW_MODEL=vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 VLLM_BASE_URL=http://127.0.0.1:8102/v1 \
#     MW_TAG=rh_poker_local WM_PORT=3511 MW_ORDERS="0" ./launch.sh                # a local vLLM server
#
# Models are backend references, <backend>/<model id> (`hermit backends list`; hermit/backends.py):
# MW_MODEL names the host's and the learner's model alike, MW_HOST_MODEL and MW_EVOLVER_MODEL either alone (default
# GLM 5.3 Flash on Fireworks; the paper runs every agent on one backbone). MW_EMBED_MODEL names the embedding model
# (default Qwen3-Embedding-8B on Fireworks, the paper's; its backend must serve /embeddings). Each backend's key
# variable (FIREWORKS_API_KEY, ...) must be set, or MW_KEY_FILE holds the key (default ~/code/metaworld/.fireworks,
# read for the fireworks backend only). The learner's backend must serve chat completions (the `llm` tool's dialect).
#
# Per tag: a host wire meter (WM_PORT), a learner wire meter (WM_PORT+1) and an embedding relay (WM_PORT+2), each in
# front of its model's backend. The embedding relay also signs requests that arrive without a key (UPSTREAM_KEY_ENV):
# the adapter's reader runs in a sandbox that has no keys and reaches 127.0.0.1 only, and REHARNESS_EMBED_URL points
# it at the relay; the Evolver's own embedding calls take the same relay, with the key. Per order: a task directory
# <runs>/<tag>/order<i>/task (host workspace, current_db, the host's evidence records, outcomes.jsonl) and one
# `hermit train` run, <runs>/<tag>/<tag>-o<i>. Run the venue's prepare.py first. MW_TRAIN_SPLIT names another split
# file (a prefix of the order's stream, for a smoke run).
# A relay that signs requests lets every local process that reaches its port spend the key: on a shared machine,
# run inside the offline container (docker/offline.sh) or another private network namespace.
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
PY=${MW_FRAMEWORK_PYTHON:-$FRAMEWORK/.venv/bin/python}
METER=${MW_WIRE_METER:-$FRAMEWORK/hermit/runtime/wire_meter.py}
[ -z "${MW_MODEL_ID:-}" ] || { echo "REFUSING: MW_MODEL_ID is gone; set MW_MODEL (or MW_HOST_MODEL) to a <backend>/<model id> reference"; exit 1; }
MODEL=${MW_MODEL:-fireworks/accounts/fireworks/models/glm-5p3-flash}
HOST_MODEL=${MW_HOST_MODEL:-$MODEL}
EVOLVER_MODEL=${MW_EVOLVER_MODEL:-$MODEL}
EMBED_MODEL=${MW_EMBED_MODEL:-fireworks/accounts/fireworks/models/qwen3-embedding-8b}
EFFORT=${MW_EFFORT:-high}
META_PORT=$((WM_PORT+1))
EMBED_PORT=$((WM_PORT+2))
TASKDIR=$FRAMEWORK/examples/clbench_poker/utils
export PYTHONPATH=$FRAMEWORK${PYTHONPATH:+:$PYTHONPATH}

# The three models resolved on their backends (the shell form of hermit.backends: HOST_*, EVOLVER_* and EMBED_*).
resolve() { "$PY" -m hermit.backends shell "$2" | sed "s/^HA_/$1_/"; }
# HOST_MODEL stays the reference; HOST_MODEL_ID is the model's id on its backend (same for EVOLVER_* and EMBED_*).
eval "$(resolve HOST "$HOST_MODEL")"; [ -n "${HOST_MODEL_ID:-}" ] || { echo "REFUSING: cannot resolve $HOST_MODEL"; exit 1; }
eval "$(resolve EVOLVER "$EVOLVER_MODEL")"; [ -n "${EVOLVER_MODEL_ID:-}" ] || { echo "REFUSING: cannot resolve $EVOLVER_MODEL"; exit 1; }
eval "$(resolve EMBED "$EMBED_MODEL")"; [ -n "${EMBED_MODEL_ID:-}" ] || { echo "REFUSING: cannot resolve $EMBED_MODEL"; exit 1; }
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
need_key "$EMBED_API_KEY_ENV" "$EMBED_KEY_REQUIRED" "$EMBED_BACKEND"
case " $EVOLVER_APIS " in *" chat "*) ;; *) echo "REFUSING: backend $EVOLVER_BACKEND does not serve chat completions, which the run's llm tool speaks"; exit 1;; esac
# The host calls through clbench's litellm: its id, its key variable (a placeholder for a keyless server), its prices.
export MWM_HOST_MODEL=$HOST_LITELLM_MODEL
HOST_KEY=""; [ -n "$HOST_API_KEY_ENV" ] && HOST_KEY=${!HOST_API_KEY_ENV:-}
[ -n "${!HOST_LITELLM_KEY_ENV:-}" ] || export "$HOST_LITELLM_KEY_ENV=${HOST_KEY:-local}"
[ -n "$HOST_PRICE_INPUT" ] && export MW_COST_IN_PER_M=$HOST_PRICE_INPUT MW_COST_OUT_PER_M=$HOST_PRICE_OUTPUT
export CLB_SKIP_BASELINE=1 CLB_REQUEST_TIMEOUT=600 CLB_REASONING_EFFORT=$EFFORT
# clbench's host calls litellm (1.81.6), whose bundled model map (the one it uses offline, LITELLM_LOCAL_MODEL_COST_MAP)
# does not know GLM 5.3 Flash on Fireworks: it would refuse the host's tool_choice="auto" (its default anyway) unless
# unsupported parameters are dropped. litellm reads the variable as bool(str): any non-empty value turns it on.
export LITELLM_DROP_PARAMS=${LITELLM_DROP_PARAMS:-True}
export MWM_CLBENCH=$CLBENCH MWM_METAWORLD=$METAWORLD

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
    curl -s -m 2 "http://127.0.0.1:$port/health" > /dev/null \
      || { echo "REFUSING: the wire meter on port $port did not come up (see $log)"; exit 1; }
  fi
}
mkdir -p "$RUNS/$TAG"
start_meter $WM_PORT "$HOST_ORIGIN" "$RUNS/$TAG/wire_ledger.json" "$RUNS/$TAG/wire_meter.log"
start_meter $META_PORT "$EVOLVER_ORIGIN" "$RUNS/$TAG/meta_wire_ledger.json" "$RUNS/$TAG/meta_wire_meter.log"
# The embedding relay signs the reader's keyless requests with the embedding backend's key (none for a keyless server);
# a relay already on the port is refused unless its /health says it signs (`"signs": true`).
if [ -n "$EMBED_API_KEY_ENV" ] && health=$(curl -s -m 2 "http://127.0.0.1:$EMBED_PORT/health" 2>/dev/null); then
  case "$health" in *'"signs": true'*) ;; *) echo "REFUSING: port $EMBED_PORT has a relay that does not sign requests (started without UPSTREAM_KEY_ENV); choose another WM_PORT"; exit 1;; esac
fi
start_meter $EMBED_PORT "$EMBED_ORIGIN" "$RUNS/$TAG/embed_wire_ledger.json" "$RUNS/$TAG/embed_wire_meter.log" \
  UPSTREAM_KEY_ENV=$EMBED_API_KEY_ENV

stamp() {  # a checkout's revision for the setting stamp; "unknown" for a directory that is not there (metaworld is not needed here)
  [ -d "$1" ] || { echo unknown; return; }
  echo "$(git -C "$1" rev-parse --short HEAD 2>/dev/null || echo unknown)$([ -n "$(git -C "$1" status --porcelain 2>/dev/null)" ] && echo -dirty || true)"
}
HEAD=$(stamp "$FRAMEWORK"); CLB_HEAD=$(stamp "$CLBENCH"); MW_HEAD=$(stamp "$METAWORLD")

PIDS=()
for i in ${MW_ORDERS:-0}; do
  WD="$RUNS/$TAG/order$i/task"
  [ -e "$RUNS/$TAG/$TAG-o$i" ] && { echo "REFUSING: $RUNS/$TAG/$TAG-o$i exists"; exit 1; }
  [ -e "$WD" ] && { echo "REFUSING: $WD exists"; exit 1; }
  mkdir -p "$WD/evidence"
  : > "$WD/current_db"
  # The task directory keeps MWM's layout (current_db, the evidence README), as ../mwm/launch.sh writes it,
  # because the host runner is MWM's; ReHarness's learner reads the framework's learning material, none of this.
  "$PY" -m hermit.algorithms.evolvers.mwm.evidence.readme --evidence "$WD/evidence" --from "$HERE/../utils/task-info/mwm/contract/env_evidence.md"
  # The learner model through its meter: the backend copied to the metered URL, the run's `llm` tool bound to it at
  # the host's reasoning effort (`llm` is a free-form section: a whole mapping on the command line). The embedding
  # model through its relay, once for the Evolver (evolver.params.embedding, with the key variable) and once for the
  # adapter's reader (REHARNESS_EMBED_URL, which the adapter's sandbox forwards: the reader alone is confined, the
  # host keeps the launcher's network; the relay adds the key).
  LEARNER="{from: '$EVOLVER_BACKEND', base_url: 'http://127.0.0.1:$META_PORT/${TAG}_o${i}_meta$EVOLVER_PATH'}"
  LLM="{model: 'learner/$EVOLVER_MODEL_ID', extra_body: {reasoning_effort: '$EFFORT'}}"
  EMBED_URL="http://127.0.0.1:$EMBED_PORT/${TAG}_o${i}_embed$EMBED_PATH"
  EMBEDDING="{base_url: '$EMBED_URL', model: '$EMBED_MODEL_ID', api_key_env: '$EMBED_API_KEY_ENV'}"
  LOG="$RUNS/$TAG/order$i/run.log"
  echo "=== setting stamp: venue=$VENUE method=reharness framework=$HEAD clbench=$CLB_HEAD metaworld=$MW_HEAD" \
       "host_model=$HOST_MODEL host_effort=$EFFORT evolver=reharness evolver_model=$EVOLVER_MODEL evolver_effort=$EFFORT" \
       "embed_model=$EMBED_MODEL meter=$META_PORT embed_relay=$EMBED_PORT shuffle=false bucket=${TAG}_o$i ===" > "$LOG"
  MWM_TASK_WORKDIR="$WD" CLB_API_BASE="http://127.0.0.1:$WM_PORT/${TAG}_o$i$HOST_PATH" REHARNESS_EMBED_URL="$EMBED_URL" \
    setsid -w nohup "$PY" -m hermit.cli train --config "$HERE/run.yaml" \
      "trainer.run_name=$TAG-o$i" "trainer.output_dir=$RUNS/$TAG" \
      "data.train=${MW_TRAIN_SPLIT:-$TASKDIR/splits/order$i.txt}" \
      "backends.learner=$LEARNER" "llm=$LLM" \
      "evolver.params.embedding=$EMBEDDING" \
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
