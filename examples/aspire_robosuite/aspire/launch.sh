#!/usr/bin/env bash
# Train, or evaluate, the ASPIRE method on Robosuite. Run it on the host that serves ASPIRE's providers.
#
#   ASPIRE_ROOT=/path/to/ASPIRE ./launch.sh                      # train run.yaml
#   ASPIRE_CONFIG=smoke.yaml ./launch.sh                          # train the smoke (its seeds: ASPIRE_SEEDS=smoke)
#   ./launch.sh eval --adapter final                              # the reruns with the final library (test split)
#   ./launch.sh eval --adapter root                               # the same with the empty library
#   ASPIRE_SIM_ENV=/path/to/sim_env.yaml ./launch.sh              # the simulator's render environment (a YAML map)
#   ASPIRE_SIM_GPU=8 ./launch.sh                                  # its GPUs, the N of /dev/nvidia<N> (none: software)
#   ASPIRE_OUT=/big/disk/aspire ./launch.sh                       # the runs, the sessions, Stage 2 (default ../runs)
#   ASPIRE_EVAL_SEEDS=1-50 ./launch.sh                            # held-out seeds other than ASPIRE's 1-100
#
# The launcher writes the seeds into the task rows (prepare.py; ASPIRE_SEEDS=smoke for the smoke's), starts the
# simulator service the actors reach through bin/sim (utils/agent/sim_service.py, on ASPIRE_SIM_PORT, default 8120;
# stopped when the run ends), and runs hermit. ASPIRE_SIM_ENV names a YAML mapping of environment variables for the
# simulator, e.g. {MUJOCO_GL: egl, PYOPENGL_PLATFORM: egl, EGL_PLATFORM: device, MUJOCO_EGL_DEVICE_ID: "0"}
# (the default, with CUDA_VISIBLE_DEVICES="": the simulator needs no CUDA, and Robosuite asserts that the EGL
# device appears in CUDA_VISIBLE_DEVICES when that is set). The model key (FIREWORKS_API_KEY) comes from the
# environment; the sandboxes need bubblewrap (`bwrap`) on PATH.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
: "${ASPIRE_ROOT:?set ASPIRE_ROOT to an ASPIRE checkout with .venv-robosuite}"
# Absolute, from where the launcher was called: the service, the judge and the sandboxed workers each start elsewhere
ASPIRE_ROOT="$(cd "$ASPIRE_ROOT" && pwd)"
[[ -z "${ASPIRE_OUT:-}" ]] || { mkdir -p "$ASPIRE_OUT" && ASPIRE_OUT="$(cd "$ASPIRE_OUT" && pwd)"; }
: "${FIREWORKS_API_KEY:?set FIREWORKS_API_KEY}"
CONFIG="${ASPIRE_CONFIG:-run.yaml}"
HERMIT="${HERMIT_PYTHON:-python}"
[[ "$HERMIT" != */* || "$HERMIT" == /* ]] || HERMIT="$PWD/$HERMIT"          # a relative path: from here, as given
SIM_ENV="${ASPIRE_SIM_ENV:-}"
if [[ -n "$SIM_ENV" ]]; then
  SIM_ENV_JSON="$("$HERMIT" -c 'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))' "$SIM_ENV")"
else
  SIM_ENV_JSON='{"MUJOCO_GL": "egl", "PYOPENGL_PLATFORM": "egl", "EGL_PLATFORM": "device", "MUJOCO_EGL_DEVICE_ID": "0", "CUDA_VISIBLE_DEVICES": ""}'
fi
# The services the programs reach: segmentation and cuRobo where CAP_HARNESS_SAM3_URL / CAP_HARNESS_CUROBO_URL say,
# PyRoki on its fixed port.
for url in "${CAP_HARNESS_SAM3_URL:-http://127.0.0.1:8114}" "http://127.0.0.1:8116" \
           "${CAP_HARNESS_CUROBO_URL:-http://127.0.0.1:8118}"; do
  curl -sf -o /dev/null --max-time 3 "$url/openapi.json" \
    || { echo "ASPIRE provider at $url is not up (see ../README.md)" >&2; exit 1; }
done
# The ASPIRE commit this example was written against; another one is run, but named.
PINNED=fa00f0ef2bc1d91d594b9aa502c22133c9f538f7
ACTUAL="$(git -C "$ASPIRE_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
[[ "$ACTUAL" == "$PINNED" ]] || echo "note: ASPIRE_ROOT is at $ACTUAL, not the pinned $PINNED" >&2
cd "$HERE"

# The seeds, in the task rows: the simulator service and the judge read them there.
case "${ASPIRE_SEEDS:-$([[ "${CONFIG##*/}" == smoke* ]] && echo smoke || echo aspire)}" in
  smoke)  SEEDS=(--dev 101-103 --unseen 126 --eval "${ASPIRE_EVAL_SEEDS:-1-3}") ;;
  aspire) SEEDS=(--dev 101-125 --unseen 126-130 --eval "${ASPIRE_EVAL_SEEDS:-1-100}") ;;
  *) echo "ASPIRE_SEEDS is smoke or aspire" >&2; exit 2 ;;
esac
(cd ../utils && "$HERMIT" prepare.py "${SEEDS[@]}")

# Where the runs go (ASPIRE_OUT): the trainer's records, the actors' sessions (their simulator runs' records and
# traces; their images are pruned after scoring), the judge's Stage 2. Put it on a large disk.
OUT="${ASPIRE_OUT:-$HERE/../runs}"
case "$(cd "$OUT" 2>/dev/null && pwd -P || echo "$OUT")/" in          # the simulator reads the checkout, not the runs
  "$(cd "$ASPIRE_ROOT" && pwd -P)"/*) echo "ASPIRE_OUT ($OUT) lies inside ASPIRE_ROOT: keep the runs elsewhere" >&2
                                      exit 2 ;;
esac
mkdir -p "$OUT"
export ASPIRE_ROOT ASPIRE_OUT="$OUT" ASPIRE_SIM_ENV="$SIM_ENV_JSON" ASPIRE_SIM_GPU="${ASPIRE_SIM_GPU:-}"   # the judge's

# The simulator service: the actors' engine, on the host; each cap-harness it starts (and the judge's) runs in a
# sandbox of its own with the GPUs ASPIRE_SIM_GPU names (utils/agent/sim.py: confine).
[[ -n "${ASPIRE_SIM_GPU:-}" ]] || echo "note: ASPIRE_SIM_GPU names no GPU: the simulator renders in software" >&2
PORT="${ASPIRE_SIM_PORT:-8120}"
NONCE="$$-$RANDOM-$(date +%s%N)"                  # this launch's service answers /health with it, another run's not
"$HERMIT" ../utils/agent/sim_service.py --port "$PORT" --aspire-root "$ASPIRE_ROOT" --tasks ../utils/tasks.jsonl \
  --workspaces "$OUT/actor-logs" --state "$OUT/sim-service" --sim-env "$SIM_ENV_JSON" --nonce "$NONCE" \
  --gpu "${ASPIRE_SIM_GPU:-}" > "$OUT/sim-service.log" 2>&1 &
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
export ASPIRE_SIM_URL="http://127.0.0.1:$PORT"

MODE=train
if [[ "${1:-}" == train || "${1:-}" == eval ]]; then MODE="$1"; shift; fi
EXTRA=()
[[ "$CONFIG" == *_direct* ]] && EXTRA=("agent.params.sim_env=$SIM_ENV_JSON")
"$HERMIT" -m hermit.cli "$MODE" --config "$CONFIG" "agent.params.aspire_root=$ASPIRE_ROOT" \
  "agent.params.logs=$OUT/actor-logs" "trainer.output_dir=$OUT" \
  "trainer.tree_dir=$OUT/trees/$(basename "$CONFIG" .yaml)" "${EXTRA[@]}" "$@"
