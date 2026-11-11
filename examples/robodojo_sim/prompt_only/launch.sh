#!/usr/bin/env bash
# Train, or evaluate, on RoboDojo-Sim. Run it on the host that has RoboDojo installed (Isaac Sim, its Assets).
#
#   ROBODOJO_ROOT=/path/to/RoboDojo ./launch.sh                   # train run.yaml
#   ROBODOJO_CONFIG=smoke.yaml ./launch.sh                         # train the smoke (its layouts: ROBODOJO_PRESET=smoke)
#   ./launch.sh eval --adapter final
#   ROBODOJO_GPU=0 ./launch.sh                                     # the simulator's GPU (default 0)
#   ROBODOJO_POLICY_ENV=RoboDojo ROBODOJO_EVAL_ENV=RoboDojo ./launch.sh   # the conda environments of the policy
#                                                                  # server and of the simulator
#   ROBODOJO_OUT=/big/disk/robodojo ./launch.sh                    # the runs, the sessions, the held-out evaluations
#
# The launcher writes the layouts into the task rows (utils/prepare.py), installs the harness's XPolicyLab policy
# into the checkout (utils/policy/install.py), starts the simulator service the actors reach through bin/sim
# (utils/agent/dojo_service.py, on ROBODOJO_SIM_PORT, default 8130; stopped when the run ends) and runs hermit. The
# model key (FIREWORKS_API_KEY) comes from the environment; the sandboxes need bubblewrap (`bwrap`) on PATH.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
: "${ROBODOJO_ROOT:?set ROBODOJO_ROOT to a RoboDojo checkout (scripts/robodojo.sh, Assets, XPolicyLab)}"
ROBODOJO_ROOT="$(cd "$ROBODOJO_ROOT" && pwd)"
CONFIG="${ROBODOJO_CONFIG:-run.yaml}"
HERMIT="${HERMIT_PYTHON:-python}"
[[ "$HERMIT" != */* || "$HERMIT" == /* ]] || HERMIT="$PWD/$HERMIT"
cd "$HERE"
! grep -q "fireworks/" "$CONFIG" || : "${FIREWORKS_API_KEY:?set FIREWORKS_API_KEY}"
# The RoboDojo commit this example was written against; another one is run, but named.
PINNED=f57e1633ffed59761bc8e568cfe7530c4b33381c
ACTUAL="$(git -C "$ROBODOJO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
[[ "$ACTUAL" == "$PINNED" ]] || echo "note: ROBODOJO_ROOT is at $ACTUAL, not the pinned $PINNED" >&2

PRESET="${ROBODOJO_PRESET:-$([[ "${CONFIG##*/}" == smoke* ]] && echo smoke || echo full)}"
(cd ../utils && "$HERMIT" prepare.py --preset "$PRESET")
"$HERMIT" ../utils/policy/install.py --root "$ROBODOJO_ROOT"

OUT="${ROBODOJO_OUT:-$HERE/../runs}"
mkdir -p "$OUT" && OUT="$(cd "$OUT" && pwd)"
case "$(cd "$OUT" && pwd -P)/" in          # a program's files must lie outside the checkout (dojo.Engine)
  "$(cd "$ROBODOJO_ROOT" && pwd -P)"/*) echo "ROBODOJO_OUT ($OUT) lies inside ROBODOJO_ROOT" >&2; exit 2 ;;
esac
export ROBODOJO_ROOT ROBODOJO_OUT="$OUT" ROBODOJO_GPU="${ROBODOJO_GPU:-0}" \
  ROBODOJO_POLICY_ENV="${ROBODOJO_POLICY_ENV:-RoboDojo}" ROBODOJO_EVAL_ENV="${ROBODOJO_EVAL_ENV:-RoboDojo}" \
  ROBODOJO_ENV_CFG="${ROBODOJO_ENV_CFG:-arx_x5}" ROBODOJO_ACTION_TYPE="${ROBODOJO_ACTION_TYPE:-joint}"   # the judge's

PORT="${ROBODOJO_SIM_PORT:-8130}"
NONCE="$$-$RANDOM-$(date +%s%N)"
"$HERMIT" ../utils/agent/dojo_service.py --port "$PORT" --robodojo-root "$ROBODOJO_ROOT" --tasks ../utils/tasks.jsonl \
  --workspaces "$OUT/actor-logs" --state "$OUT/sim-service" --gpu "$ROBODOJO_GPU" --nonce "$NONCE" \
  --policy-env "$ROBODOJO_POLICY_ENV" --eval-env "$ROBODOJO_EVAL_ENV" --env-cfg "$ROBODOJO_ENV_CFG" \
  --action-type "$ROBODOJO_ACTION_TYPE" > "$OUT/sim-service.log" 2>&1 &
SERVICE=$!
trap 'kill "$SERVICE" 2>/dev/null || true' EXIT
ours() { curl -sf "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -qF "\"nonce\": \"$NONCE\""; }
for _ in $(seq 1 30); do
  ours && break
  kill -0 "$SERVICE" 2>/dev/null \
    || { echo "the simulator service failed (port $PORT taken?): $OUT/sim-service.log" >&2; exit 1; }
  sleep 1
done
ours || { echo "the simulator service did not come up on port $PORT: $OUT/sim-service.log" >&2; exit 1; }
export ROBODOJO_SIM_URL="http://127.0.0.1:$PORT"

MODE=train
if [[ "${1:-}" == train || "${1:-}" == eval ]]; then MODE="$1"; shift; fi
"$HERMIT" -m hermit.cli "$MODE" --config "$CONFIG" "agent.params.logs=$OUT/actor-logs" "trainer.output_dir=$OUT" \
  "trainer.tree_dir=$OUT/trees/$(basename "$CONFIG" .yaml)" "$@"
