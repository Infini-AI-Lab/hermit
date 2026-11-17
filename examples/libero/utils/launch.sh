#!/usr/bin/env bash
# Train, or evaluate, a method on LIBERO: started by a method folder's launch.sh (../aspire/launch.sh,
# ../prompt_only/launch.sh) with that folder's name. Run it on the host that serves ASPIRE's providers. From a
# method folder:
#
#   ASPIRE_ROOT=/path/to/ASPIRE ./launch.sh                      # train run.yaml (the four LIBERO suites)
#   ASPIRE_CONFIG=run_pro.yaml ./launch.sh                        # train LIBERO-Pro
#   ASPIRE_CONFIG=smoke_luna.yaml ./launch.sh                     # train the smoke (its seeds: ASPIRE_SEEDS=smoke)
#   ./launch.sh eval --adapter final                              # the tasks again with the final adapter (test split)
#   ./launch.sh eval --adapter root                               # the same with the empty one
#
# The engine is the Robosuite example's (../../aspire_robosuite): its launcher runs the method folder's configs, with
# the seeds of utils/prepare.py, and takes the same variables (ASPIRE_SIM_ENV, ASPIRE_SIM_GPU, ASPIRE_OUT,
# ASPIRE_EVAL_SEEDS, ASPIRE_SIM_PORT, HERMIT_PYTHON; see its header). ASPIRE_ROOT needs .venv-libero.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
METHOD="${1:?the method folder}"; shift
: "${ASPIRE_ROOT:?set ASPIRE_ROOT to an ASPIRE checkout with .venv-libero (scripts/bootstrap_libero.sh)}"
[[ -x "$ASPIRE_ROOT/.venv-libero/bin/cap-harness" ]] \
  || { echo "$ASPIRE_ROOT/.venv-libero/bin/cap-harness: run ASPIRE's scripts/bootstrap_libero.sh" >&2; exit 1; }
# Contact-GraspNet, LIBERO's grasp backend (ASPIRE's SERVICE_PORTS for libero-pro), on its fixed port; the
# Robosuite launcher checks the other services
curl -sf -o /dev/null --max-time 3 "http://127.0.0.1:8115/openapi.json" \
  || { echo "ASPIRE's Contact-GraspNet service at http://127.0.0.1:8115 is not up (see ../README.md)" >&2; exit 1; }
ASPIRE_EXAMPLE="$HERE/.." ASPIRE_METHOD="$METHOD" exec "$HERE/../../aspire_robosuite/aspire/launch.sh" "$@"
