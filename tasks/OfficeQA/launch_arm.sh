#!/bin/bash
# OfficeQA on this machine, one arm per invocation (stateless | prompt-only |
# mwm): native codex actor through the branch's per-question broker, the
# framework's codex learner through a metering relay. Everything task-specific
# lives under tasks/OfficeQA; the framework repository receives no run data.
#
#   MW_ARM=mwm MW_TAG=oqa_mwm_flash WM_PORT=3411 MW_PREPARED=... ./launch_arm.sh
#
# MW_ACTOR / MW_EVOLVER (flash | sol, default flash): sol = gpt-5.6-sol on the ChatGPT sign-in.
# MW_QUESTIONS (default 50; 90 for v2), MW_FEEDBACK (gold | verdict | none, default gold),
# MW_DEPLOYMENT (officeqa | officeqa_v2, default officeqa; must match MW_PREPARED).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
FRAMEWORK=$(cd "$HERE/../.." && pwd)
CODE=$(dirname "$FRAMEWORK")
METAWORLD=${MW_METAWORLD:-$CODE/metaworld}
RUNS=${MW_RUNS:-$CODE/metaworld-runs}
ARM=${MW_ARM:?set MW_ARM (stateless | prompt-only | mwm)}
TAG=${MW_TAG:?set MW_TAG}
WM_PORT=${WM_PORT:?set WM_PORT}
PREPARED=${MW_PREPARED:?set MW_PREPARED (the directory holding prepared/)}
DEPLOYMENT=${MW_DEPLOYMENT:-officeqa}   # deployments/<name>: officeqa (Pro v1 sample) | officeqa_v2 (Pro v2)
QUESTIONS=${MW_QUESTIONS:-50}
FEEDBACK=${MW_FEEDBACK:-gold}
CONCURRENCY=${MW_CONCURRENCY:-1}
ACTOR=${MW_ACTOR:-flash}       # flash: GLM 5.3 Flash via Fireworks broker | sol: gpt-5.6-sol via the ChatGPT sign-in (no broker)
EVOLVER=${MW_EVOLVER:-flash}   # flash: GLM 5.3 Flash via the metering relay | sol: gpt-5.6-sol via the ChatGPT sign-in (no relay)
case "$ACTOR$EVOLVER" in flashflash|flashsol|solflash|solsol) ;; *) echo "REFUSING: MW_ACTOR/MW_EVOLVER must be flash or sol"; exit 1;; esac
if [ "$EVOLVER" = sol ]; then EVOLVER_MODEL=${MW_EVOLVER_MODEL:-gpt-5.6-sol}; else EVOLVER_MODEL=${MW_EVOLVER_MODEL:-accounts/fireworks/models/glm-5p3-flash}; fi
if [ "$ACTOR" = sol ]; then ACTOR_MODEL=gpt-5.6-sol; else ACTOR_MODEL=accounts/fireworks/models/glm-5p3-flash; fi
EVOLVER_TIMEOUT=${MW_EVOLVER_TIMEOUT:-1800}
MAX_REQUESTS=${MW_MAX_REQUESTS:-100}       # model requests per question (0 = unlimited); the cap ends the question as request_cap
ACTOR_TIMEOUT=${MW_ACTOR_TIMEOUT:-1800}   # infrastructure guard per question (30 min, both arms; normal questions take 1-12 min); no output-token budget
WS="$RUNS/officeqa_${TAG}"
[ -e "$WS" ] && [ -z "${MW_RESUME:-}" ] && { echo "REFUSING: $WS exists (set MW_RESUME=1 to continue it)"; exit 1; }
[ -x "$FRAMEWORK/.venv/bin/harnessadapter" ] || { echo "REFUSING: framework CLI not installed"; exit 1; }
command -v codex >/dev/null || { echo "REFUSING: codex CLI not on PATH"; exit 1; }
command -v bwrap >/dev/null || { echo "REFUSING: bubblewrap not installed"; exit 1; }
[ -f "$METAWORLD/.fireworks" ] || { echo "REFUSING: key file missing"; exit 1; }
KEY=$(cat "$METAWORLD/.fireworks")
export HA_FW_KEY=$KEY
if [ "$EVOLVER" = flash ]; then export HA_PROVIDER_NAME=fireworks HA_PROVIDER_ENV_KEY=HA_FW_KEY HA_PROVIDER_WIRE_API=responses; fi
if [ "$ACTOR$EVOLVER" != flashflash ]; then codex login status 2>&1 | grep -q "Logged in" || { echo "REFUSING: codex is not signed in"; exit 1; }; fi
export OFFICEQA_CODEX=$(readlink -f "$(command -v codex)") OFFICEQA_ACTOR=$ACTOR
[ -d "$HERE/deployments/$DEPLOYMENT" ] || { echo "REFUSING: deployment $DEPLOYMENT missing"; exit 1; }
export OFFICEQA_DEPLOYMENT=$DEPLOYMENT
if [ "$ARM" != stateless ] && [ "$EVOLVER" = flash ] && ! curl -s -m 2 "http://127.0.0.1:$WM_PORT/health" >/dev/null 2>&1; then
  REWRITE_DEVELOPER_ROLE=1 REWRITE_STRIP_SERVER_TOOLS=1 REWRITE_REPAIR_TOOL_ARGS=1 DUMP_REJECTED="$RUNS/officeqa_${TAG}_rejected" \
  PORT=$WM_PORT LEDGER="$RUNS/officeqa_${TAG}_meta_wire_ledger.json" UPSTREAM=https://api.fireworks.ai \
    setsid nohup python3 "$METAWORLD/mwm/wire_meter.py" > "$RUNS/officeqa_${TAG}_meta_wire_meter.log" 2>&1 < /dev/null &
  echo "learner meter pid $! port $WM_PORT"; sleep 2
fi
if [ "$EVOLVER" = flash ]; then export HA_PROVIDER_BASE_URL="http://127.0.0.1:$WM_PORT/${TAG}_meta/inference/v1"; PROVIDER="fireworks-via-meter:$WM_PORT"; else PROVIDER=openai-chatgpt-login; fi
HEAD=$(git -C "$FRAMEWORK" rev-parse --short HEAD)$([ -n "$(git -C "$FRAMEWORK" status --porcelain)" ] && echo -dirty || true)
mkdir -p "$RUNS"
echo "=== setting stamp: venue=officeqa arm=$ARM framework=$HEAD codex=$(codex --version 2>/dev/null | head -1) actor=$ACTOR model=$ACTOR_MODEL host_effort=high questions=$QUESTIONS learn_every=5 concurrency=$CONCURRENCY feedback=$FEEDBACK evolver=codex evolver_model=$EVOLVER_MODEL evolver_effort=high provider=$PROVIDER evolver_network=off evolver_timeout=$EVOLVER_TIMEOUT actor_timeout=$ACTOR_TIMEOUT max_requests=$MAX_REQUESTS deployment=deployments/$DEPLOYMENT prepared=$PREPARED resume=${MW_RESUME:-0} ===" >> "$RUNS/officeqa_${TAG}.launch.log"
setsid nohup "$FRAMEWORK/.venv/bin/python" -B "$HERE/host/arm.py" --arm "$ARM" --run-dir "$WS" --prepared "$PREPARED" \
  --questions "$QUESTIONS" --learn-every 5 --concurrency "$CONCURRENCY" --feedback "$FEEDBACK" --evolver-model "$EVOLVER_MODEL" \
  --evolver-timeout "$EVOLVER_TIMEOUT" --actor-timeout "$ACTOR_TIMEOUT" --max-requests "$MAX_REQUESTS" --key-file "$METAWORLD/.fireworks" ${MW_RESUME:+--resume} \
  >> "$RUNS/officeqa_${TAG}.launch.log" 2>&1 < /dev/null &
echo "arm $ARM -> pid $! ws $WS (log $RUNS/officeqa_${TAG}.launch.log)"
