#!/usr/bin/env bash
# Train (and then evaluate) the ASPIRE method on Robosuite. Run it on the host that serves ASPIRE's providers.
#
#   ASPIRE_ROOT=/path/to/ASPIRE ./launch.sh                      # train run.yaml
#   ASPIRE_CONFIG=smoke.yaml ./launch.sh                          # train the smoke
#   ./launch.sh eval --adapter final                              # the reruns with the final library (test split)
#   ./launch.sh eval --adapter root                               # the same with the empty library
#   ASPIRE_SIM_ENV=/path/to/sim_env.yaml ./launch.sh              # the simulator's render environment (a YAML map)
#
# ASPIRE_SIM_ENV names a YAML mapping of environment variables for the simulator process, e.g.
#   {MUJOCO_GL: egl, PYOPENGL_PLATFORM: egl, EGL_PLATFORM: device, MUJOCO_EGL_DEVICE_ID: "0"}
# (the default, with CUDA_VISIBLE_DEVICES="": the simulator needs no CUDA, and Robosuite asserts that the EGL
# device appears in CUDA_VISIBLE_DEVICES when that is set). The model key (FIREWORKS_API_KEY) comes from the environment.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
: "${ASPIRE_ROOT:?set ASPIRE_ROOT to an ASPIRE checkout with .venv-robosuite}"
: "${FIREWORKS_API_KEY:?set FIREWORKS_API_KEY}"
CONFIG="${ASPIRE_CONFIG:-run.yaml}"
HERMIT="${HERMIT_PYTHON:-python}"
SIM_ENV="${ASPIRE_SIM_ENV:-}"
if [[ -n "$SIM_ENV" ]]; then
  SIM_ENV_YAML="$("$HERMIT" -c 'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))' "$SIM_ENV")"
else
  SIM_ENV_YAML='{"MUJOCO_GL": "egl", "PYOPENGL_PLATFORM": "egl", "EGL_PLATFORM": "device", "MUJOCO_EGL_DEVICE_ID": "0", "CUDA_VISIBLE_DEVICES": ""}'
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
(cd ../utils && "$HERMIT" prepare.py)
MODE=train
if [[ "${1:-}" == train || "${1:-}" == eval ]]; then MODE="$1"; shift; fi
"$HERMIT" -m hermit.cli "$MODE" --config "$CONFIG" "agent.params.aspire_root=$ASPIRE_ROOT" \
  "agent.params.sim_env=$SIM_ENV_YAML" "$@"
