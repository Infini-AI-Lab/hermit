#!/bin/bash
# CL-Bench exploitable poker through the framework: host = GLM flash (effort
# high), adapter = mwm architecture, learner = codex + GLM flash through a
# metering relay, one evolve session every 5 hands plus a bootstrap session
# before the first hand.
#
#   MW_TAG=fw_mwm_flash WM_PORT=3311 MW_ORDERS="0" ./launch_poker.sh
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
FRAMEWORK=$(cd "$HERE/../.." && pwd)
CODE=$(dirname "$FRAMEWORK")
CLBENCH=${MW_CLBENCH:-$CODE/clbench}
METAWORLD=${MW_METAWORLD:-$CODE/metaworld}
RUNS=${MW_RUNS:-$CODE/metaworld-runs}
ARCH=${MW_ARCH:-mwm}
TAG=${MW_TAG:?set MW_TAG}
WM_PORT=${WM_PORT:?set WM_PORT}
SCHEDULE=${MW_POKER_SCHEDULE:-default}
MODEL=${MW_MODEL_ID:-fireworks_ai/accounts/fireworks/models/glm-5p3-flash}
EVOLVER_MODEL=${MW_EVOLVER_MODEL:-accounts/fireworks/models/glm-5p3-flash}
EVOLVER_TIMEOUT=${MW_EVOLVER_TIMEOUT:-1800}
LEARN_EVERY=${MW_POKER_LEARN_EVERY:-5}
EFFORT=high
META_PORT=$((WM_PORT+1))

KEY=$(cat "$METAWORLD/.fireworks")
export FIREWORKS_AI_API_KEY=$KEY FIREWORKS_API_KEY=$KEY HA_FW_KEY=$KEY
export CLB_SKIP_BASELINE=1 CLB_REQUEST_TIMEOUT=600 CLB_REASONING_EFFORT=$EFFORT
export MW_HARNESS_ADAPTER="$FRAMEWORK"
export HA_PROVIDER_NAME=fireworks HA_PROVIDER_ENV_KEY=HA_FW_KEY HA_PROVIDER_WIRE_API=responses

cd "$CLBENCH"
[ -x "$FRAMEWORK/.venv/bin/harnessadapter" ] || { echo "REFUSING: framework CLI not installed"; exit 1; }
[ -f "$FRAMEWORK/tasks/CLBench/host/poker_system.py" ] || { echo "REFUSING: task host module missing"; exit 1; }
[ -d "$FRAMEWORK/tasks/CLBench/deployments/poker" ] || { echo "REFUSING: poker deployment missing"; exit 1; }
command -v codex >/dev/null || { echo "REFUSING: codex CLI not on PATH"; exit 1; }
grep -q 'native_tools' src/systems/icl/system.py || { echo "REFUSING: harness lacks the native-tool channel"; exit 1; }

start_meter() {
  local port=$1 ledger=$2 log=$3; shift 3
  if ! curl -s -m 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    env "$@" PORT=$port LEDGER="$ledger" UPSTREAM=https://api.fireworks.ai \
      setsid nohup python3 "$METAWORLD/mwm/wire_meter.py" > "$log" 2>&1 < /dev/null &
    echo "wire meter pid $! port $port"; sleep 2
  fi
}
start_meter $WM_PORT "$RUNS/poker_${TAG}_wire_ledger.json" "$RUNS/poker_${TAG}_wire_meter.log"
start_meter $META_PORT "$RUNS/poker_${TAG}_meta_wire_ledger.json" "$RUNS/poker_${TAG}_meta_wire_meter.log" \
  REWRITE_DEVELOPER_ROLE=1 REWRITE_STRIP_SERVER_TOOLS=1

HEAD=$(git -C "$FRAMEWORK" rev-parse --short HEAD)$([ -n "$(git -C "$FRAMEWORK" status --porcelain)" ] && echo -dirty || true)
CLB_HEAD=$(git rev-parse --short HEAD)$([ -n "$(git status --porcelain)" ] && echo -dirty || true)

for i in ${MW_ORDERS:-0}; do
  WS="$RUNS/poker_${TAG}_order$i"
  [ -e "$WS" ] && { echo "REFUSING: $WS exists"; exit 1; }
  mkdir -p "$WS"
  echo "=== setting stamp: venue=exploitable_poker schedule=$SCHEDULE arm=harness_adapter_poker architecture=$ARCH" \
       "framework=$HEAD clbench=$CLB_HEAD model=$MODEL host_effort=$EFFORT action_channel=${CLB_ACTION_CHANNEL:-tools}" \
       "evolver=codex evolver_model=$EVOLVER_MODEL evolver_effort=high provider=fireworks-via-meter:$META_PORT" \
       "evolver_network=off evolver_timeout=$EVOLVER_TIMEOUT learn_every=$LEARN_EVERY bootstrap=1" \
       "deployment=tasks/CLBench/deployments/poker delegation=none bucket=${TAG}_o$i ===" > "$WS/run.log"
  HA_PROVIDER_BASE_URL="http://127.0.0.1:$META_PORT/${TAG}_o${i}_meta/inference/v1" \
  CLB_API_BASE="http://127.0.0.1:$WM_PORT/${TAG}_o$i/inference/v1" \
    setsid nohup .venv/bin/clbench run exploitable_poker --schedule "$SCHEDULE" \
    --runs 1 --run-mode replicate --task.run-index $i \
    --system harness_adapter_poker --system.learn-every "$LEARN_EVERY" \
    --system.architecture "$ARCH" \
    --system.model "$MODEL" \
    --system.framework "$FRAMEWORK" \
    --system.evolver-model "$EVOLVER_MODEL" \
    --system.evolver-timeout "$EVOLVER_TIMEOUT" \
    --system.workspace "$WS" \
    --no-live-dashboard \
    >> "$WS/run.log" 2>&1 < /dev/null &
  echo "order $i -> pid $! ws $WS"
  sleep 3
done
