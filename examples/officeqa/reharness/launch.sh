#!/bin/bash
# ReHarness on OfficeQA: one of the configs beside this file (smoke, run, stream) through `hermit train`, `eval` or
# `check`, with the embedding model behind a wire meter on the loopback. The adapter's reader runs in the rollouts'
# adapter sandbox, which has no keys and reaches only the 127.0.0.1 endpoints the environment names, so it embeds
# through the meter (REHARNESS_EMBED_URL) and the meter signs its requests with the Fireworks key (UPSTREAM_KEY_ENV).
# The Evolver's embedding calls go through the same meter (evolver.params.embedding), so the ledger holds the whole
# embedding bill under one bucket per config, officeqa_reharness_<config>.
#
#   FIREWORKS_API_KEY=... WM_PORT=3601 examples/officeqa/reharness/launch.sh <config> [train|eval|check] [hermit arguments...]
#
#   config: smoke, run, stream (direct chat: SkillOpt's rollout), stream_codex, run_codex_flash (Codex 0.162.0 in a
#   container per session, the recipe's Flash row; needs the image, the network and the egress proxy of
#   ../utils/agent/codex_session.py, OFFICEQA_CODEX_IMAGE default officeqa-codex:0.162.0)
#
#   ... launch.sh smoke check --no-probe          # hermit check on smoke.yaml (--no-probe skips its one model call)
#   ... launch.sh smoke                           # hermit train on smoke.yaml
#   ... launch.sh smoke eval                      # hermit eval --adapter final (or give --adapter none, seed_adapter, ...)
#   ... launch.sh run && ... launch.sh run eval   # the Results setting: the 50 as one stream, then the 172, three passes
#   ... launch.sh stream                          # the paper's protocol: the 172 as one stream; the number is in steps.jsonl
#   ... launch.sh stream_codex                    # the same stream on the Flash row (Codex, no verdict to the learner)
#   ... launch.sh stream_codex evolver.params.feedback=true trainer.run_name=rh-stream-s0-fb trainer.tree_dir=runs/trees/rh-stream-s0-fb
#   ... launch.sh run_codex_flash && ... launch.sh run_codex_flash eval      # the Flash row's split: 50 as a stream, then the 172 once
#   ... launch.sh run trainer.run_name=paper-reharness-2    # overrides and options go to hermit as they are
#
# The key: FIREWORKS_API_KEY in the environment, else the file MW_KEY_FILE names (as the CL-Bench launchers read it);
# it is never written here. Python: MW_FRAMEWORK_PYTHON, default this checkout's .venv/bin/python, importing this
# checkout (PYTHONPATH). A meter already on WM_PORT is reused when it faces the same upstream and signs requests
# (its /health says so; one started without UPSTREAM_KEY_ENV is refused: the reader's unsigned requests would fail). PROXY in the
# environment makes the meter reach the upstream through that egress proxy.
set -eu -o pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
FRAMEWORK=$(cd "$HERE/../../.." && pwd)
PY=${MW_FRAMEWORK_PYTHON:-$FRAMEWORK/.venv/bin/python}
CONFIG=${1:?usage: launch.sh <smoke|run|stream|stream_codex|run_codex_flash> [train|eval|check] [hermit arguments...]}
case "$CONFIG" in smoke|run|stream|stream_codex|run_codex_flash) ;;
  *) echo "REFUSING: the config is smoke, run, stream, stream_codex or run_codex_flash, not '$CONFIG'"; exit 1;; esac
shift
COMMAND=train
case "${1:-}" in train|eval|check) COMMAND=$1; shift;; esac
WM_PORT=${WM_PORT:?set WM_PORT, the port of the wire meter (3601, say)}
export PYTHONPATH=$FRAMEWORK${PYTHONPATH:+:$PYTHONPATH}
cd "$HERE"
[ -f ../utils/tasks.jsonl ] || { echo "REFUSING: run ../utils/prepare.py first"; exit 1; }
case "$CONFIG" in stream|stream_codex) [ "$COMMAND" != eval ] \
  || { echo "REFUSING: $CONFIG.yaml has no test split; its number is the mean of rollout.mean_score over the run's steps.jsonl"; exit 1; };; esac
case "$CONFIG" in *codex*)           # the Codex harness: its image, internal network and egress proxy must be there
  export OFFICEQA_CODEX_IMAGE=${OFFICEQA_CODEX_IMAGE:-officeqa-codex:0.162.0}
  "$PY" ../utils/agent/codex_session.py --check "$("$PY" -c "import json; print(json.load(open('../utils/data.json'))['docs'])")" \
    || { echo "REFUSING: the Codex harness's container is not ready (see ../utils/agent/codex_session.py)"; exit 1; };; esac
# hermit's options (--adapter, --no-probe) go before its overrides (key=value), whatever order they were given in.
OPTS=() OVERRIDES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --adapter) OPTS+=("$1" "${2:?--adapter needs a value}"); shift 2;;
    --*) OPTS+=("$1"); shift;;
    *) OVERRIDES+=("$1"); shift;;
  esac
done
if [ "$COMMAND" = eval ]; then
  case " ${OPTS[*]-} " in *" --adapter "*) ;; *) OPTS+=(--adapter final);; esac
fi
# The embedding model the config names, resolved on its backend (the shell form of hermit.backends): the meter's
# upstream (EMBED_ORIGIN), the metered URL's tail (EMBED_PATH), the model's id and its key variable.
EMBED_REF=$("$PY" -c "import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))['evolver']['params']['embedding'])" "$CONFIG.yaml")
eval "$("$PY" -m hermit.backends shell "$EMBED_REF" | sed 's/^HA_/EMBED_/')"
[ -n "${EMBED_MODEL_ID:-}" ] || { echo "REFUSING: cannot resolve $EMBED_REF"; exit 1; }
# The embedding backend's key variable (none for a keyless server): from the environment, else MW_KEY_FILE; required
# unless the backend says not, as the CL-Bench launchers' need_key has it.
KEY_ENV=${EMBED_API_KEY_ENV:-}
if [ -n "$KEY_ENV" ] && [ -z "${!KEY_ENV:-}" ] && [ -n "${MW_KEY_FILE:-}" ] && [ -f "$MW_KEY_FILE" ]; then
  export "$KEY_ENV=$(cat "$MW_KEY_FILE")"
fi
if [ "${EMBED_KEY_REQUIRED:-1}" = 1 ] && [ -n "$KEY_ENV" ] && [ -z "${!KEY_ENV:-}" ]; then
  echo "REFUSING: $KEY_ENV is not set (the key of backend $EMBED_BACKEND), and MW_KEY_FILE names no file"; exit 1
fi
# The meter: one per port, started here if none answers; a meter facing another upstream is refused.
mkdir -p runs
LEDGER=$HERE/runs/wire_ledger-$WM_PORT.json
if health=$(curl -s -m 2 "http://127.0.0.1:$WM_PORT/health" 2>/dev/null); then
  case "$health" in
    *"\"upstream\": \"$EMBED_ORIGIN\""*'"signs": true'*|*'"signs": true'*"\"upstream\": \"$EMBED_ORIGIN\""*)
      echo "wire meter already on port $WM_PORT -> $EMBED_ORIGIN (signs)";;
    *"\"upstream\": \"$EMBED_ORIGIN\""*)
      [ -z "$KEY_ENV" ] && echo "wire meter already on port $WM_PORT -> $EMBED_ORIGIN (keyless backend)" \
        || { echo "REFUSING: the wire meter on port $WM_PORT does not sign requests (started without UPSTREAM_KEY_ENV); the reader's requests would fail; choose another WM_PORT"; exit 1; };;
    *) echo "REFUSING: port $WM_PORT has a wire meter facing another or an unknown upstream ($health); choose another WM_PORT"; exit 1;;
  esac
else
  env PORT=$WM_PORT UPSTREAM=$EMBED_ORIGIN ${KEY_ENV:+UPSTREAM_KEY_ENV=$KEY_ENV} LEDGER=$LEDGER \
    setsid nohup "$PY" -m hermit.runtime.wire_meter >> "runs/wire_meter-$WM_PORT.log" 2>&1 < /dev/null &
  echo "wire meter pid $! port $WM_PORT -> $EMBED_ORIGIN (ledger $LEDGER)"; sleep 2
  curl -s -m 2 "http://127.0.0.1:$WM_PORT/health" > /dev/null \
    || { echo "REFUSING: the wire meter did not come up (runs/wire_meter-$WM_PORT.log)"; exit 1; }
fi
export REHARNESS_EMBED_URL=http://127.0.0.1:$WM_PORT/officeqa_reharness_$CONFIG$EMBED_PATH   # the reader's endpoint
"$PY" make_seed.py
KEY_YAML=${KEY_ENV:+"'$KEY_ENV'"}; KEY_YAML=${KEY_YAML:-null}      # the key variable, quoted, or null for a keyless backend
EMBEDDING="{base_url: '$REHARNESS_EMBED_URL', model: '$EMBED_MODEL_ID', api_key_env: $KEY_YAML}"
echo "=== hermit $COMMAND $CONFIG.yaml: embedding $EMBED_REF through $REHARNESS_EMBED_URL; framework" \
     "$(git -C "$FRAMEWORK" rev-parse --short HEAD 2>/dev/null || echo unknown) ==="
exec "$PY" -m hermit.cli "$COMMAND" --config "$CONFIG.yaml" ${OPTS[@]+"${OPTS[@]}"} \
  "evolver.params.embedding=$EMBEDDING" ${OVERRIDES[@]+"${OVERRIDES[@]}"}
