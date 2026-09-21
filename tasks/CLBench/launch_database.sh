#!/bin/bash
# CL-Bench database through the framework: host = GLM flash (effort high,
# native tools, accuracy scoring, unlimited queries), adapter = the mwm
# architecture (or prompt-only), learner = codex + GLM flash through a
# metering relay, one evolve session per question plus a bootstrap session
# before the first question. Everything task-specific lives in this
# directory; the framework repository receives no run data.
#
#   MW_ARCH=mwm MW_TAG=fw_mwm_flash WM_PORT=3301 ./launch_database.sh
#
# Services started here (per tag): a host wire meter (WM_PORT), a learner
# wire meter with the developer-role/server-tool rewrites codex needs against
# Fireworks (WM_PORT+1), and the read-only SQL endpoint the learner's `sql`
# binding uses (WM_PORT+2), fed by the run's current_db pointer.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
FRAMEWORK=$(cd "$HERE/../.." && pwd)
CODE=$(dirname "$FRAMEWORK")
CLBENCH=${MW_CLBENCH:-$CODE/clbench}
METAWORLD=${MW_METAWORLD:-$CODE/metaworld}          # wire_meter.py and the .fireworks key file
RUNS=${MW_RUNS:-$CODE/metaworld-runs}
ARCH=${MW_ARCH:?set MW_ARCH (mwm | prompt-only)}
TAG=${MW_TAG:?set MW_TAG}
WM_PORT=${WM_PORT:?set WM_PORT}
MODEL=${MW_MODEL_ID:-fireworks_ai/accounts/fireworks/models/glm-5p3-flash}
EVOLVER_MODEL=${MW_EVOLVER_MODEL:-accounts/fireworks/models/glm-5p3-flash}
EVOLVER_TIMEOUT=${MW_EVOLVER_TIMEOUT:-1800}
EFFORT=high
META_PORT=$((WM_PORT+1))   # each order gets its own SQL endpoint at WM_PORT+2+order (its pointer is per workspace)

KEY=$(cat "$METAWORLD/.fireworks")
export FIREWORKS_AI_API_KEY=$KEY FIREWORKS_API_KEY=$KEY HA_FW_KEY=$KEY
export CLB_SKIP_BASELINE=1 CLB_SCORING=accuracy CLB_QUERY_BUDGET=${CLB_QUERY_BUDGET:-0}
export CLB_REQUEST_TIMEOUT=600 CLB_REASONING_EFFORT=$EFFORT
export MW_HARNESS_ADAPTER="$FRAMEWORK" MW_META_SQL=1
export HA_PROVIDER_NAME=fireworks HA_PROVIDER_ENV_KEY=HA_FW_KEY HA_PROVIDER_WIRE_API=responses

cd "$CLBENCH"
[ -x "$FRAMEWORK/.venv/bin/harnessadapter" ] || { echo "REFUSING: framework CLI not installed"; exit 1; }
[ -f "$FRAMEWORK/tasks/CLBench/host/database_system.py" ] || { echo "REFUSING: task host module missing"; exit 1; }
[ -d "$FRAMEWORK/tasks/CLBench/deployments/clbench" ] || { echo "REFUSING: clbench deployment missing"; exit 1; }
command -v codex >/dev/null || { echo "REFUSING: codex CLI not on PATH"; exit 1; }
grep -q 'SCORING_UNLIMITED' src/tasks/database_exploration/prompts.py || { echo "REFUSING: task prompt lacks the accuracy scoring sentence"; exit 1; }
grep -q 'native_tools' src/systems/icl/system.py || { echo "REFUSING: harness lacks the native-tool channel"; exit 1; }

start_meter() {  # port ledger log [extra env...]
  local port=$1 ledger=$2 log=$3; shift 3
  if ! curl -s -m 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    env "$@" PORT=$port LEDGER="$ledger" UPSTREAM=https://api.fireworks.ai \
      setsid nohup python3 "$METAWORLD/mwm/wire_meter.py" > "$log" 2>&1 < /dev/null &
    echo "wire meter pid $! port $port"; sleep 2
  fi
}
start_meter $WM_PORT "$RUNS/clbench_${TAG}_wire_ledger.json" "$RUNS/clbench_${TAG}_wire_meter.log"
start_meter $META_PORT "$RUNS/clbench_${TAG}_meta_wire_ledger.json" "$RUNS/clbench_${TAG}_meta_wire_meter.log" \
  REWRITE_DEVELOPER_ROLE=1 REWRITE_STRIP_SERVER_TOOLS=1 REWRITE_REPAIR_TOOL_ARGS=1 DUMP_REJECTED="$RUNS/${TAG}_rejected"

HEAD=$(git -C "$FRAMEWORK" rev-parse --short HEAD)$([ -n "$(git -C "$FRAMEWORK" status --porcelain)" ] && echo -dirty || true)
CLB_HEAD=$(git rev-parse --short HEAD)$([ -n "$(git status --porcelain)" ] && echo -dirty || true)
TASK_EXTRA=""
[ -n "${CLB_NUM_INSTANCES:-}" ] && TASK_EXTRA="--task.num-instances $CLB_NUM_INSTANCES"

for i in ${MW_ORDERS:-0}; do
  WS="$RUNS/clbench_${TAG}_order$i"
  [ -e "$WS" ] && { echo "REFUSING: $WS exists"; exit 1; }
  mkdir -p "$WS"
  : > "$WS/current_db"
  SQL_PORT=$((WM_PORT+2+i))
  SQL_URL="http://127.0.0.1:$SQL_PORT"
  if ! curl -s -m 2 "$SQL_URL/health" >/dev/null 2>&1; then
    setsid nohup "$FRAMEWORK/.venv/bin/python" "$HERE/env_server.py" --port $SQL_PORT \
      --db-pointer "$WS/current_db" --log "$WS/meta_sql.jsonl" > "$WS/env_server.log" 2>&1 < /dev/null &
    echo "sql endpoint pid $! port $SQL_PORT (pointer $WS/current_db)"; sleep 1
  fi
  echo "=== setting stamp: venue=database_exploration arm=harness_adapter architecture=$ARCH" \
       "framework=$HEAD clbench=$CLB_HEAD model=$MODEL host_effort=$EFFORT" \
       "query_budget=$CLB_QUERY_BUDGET action_channel=${CLB_ACTION_CHANNEL:-tools} system_prompt=''" \
       "evolver=codex evolver_model=$EVOLVER_MODEL evolver_effort=high provider=fireworks-via-meter:$META_PORT" \
       "evolver_network=off evolver_timeout=$EVOLVER_TIMEOUT learn_every=1 bootstrap=1 sql=$SQL_URL" \
       "deployment=tasks/CLBench/deployments/clbench delegation=none bucket=${TAG}_o$i ===" > "$WS/run.log"
  HA_PROVIDER_BASE_URL="http://127.0.0.1:$META_PORT/${TAG}_o${i}_meta/inference/v1" \
  CLB_API_BASE="http://127.0.0.1:$WM_PORT/${TAG}_o$i/inference/v1" \
    setsid nohup .venv/bin/clbench run database_exploration --schedule default \
    --runs 1 --run-mode replicate --task.run-index $i $TASK_EXTRA \
    --system harness_adapter --system.learn-every 1 \
    --system.architecture "$ARCH" \
    --system.model "$MODEL" \
    --system.framework "$FRAMEWORK" \
    --system.evolver-model "$EVOLVER_MODEL" \
    --system.evolver-timeout "$EVOLVER_TIMEOUT" \
    --system.sql-url "$SQL_URL" \
    --system.workspace "$WS" \
    >> "$WS/run.log" 2>&1 < /dev/null &
  echo "order $i -> pid $! ws $WS"
  sleep 3
done
