#!/bin/bash
# The OfficeQA Flash row's six methods with Codex + GPT-5.6-Luna acting AND learning, on this machine's ChatGPT login
# (its Codex plan) and its own standalone Codex (a pinned copy, built into the image officeqa-codex:<version>). Every
# other setting is the Flash row's config, changed only by the overrides below: the acting model, the learner's model
# (through ../utils/agent/codex_chat.py, which answers each learner call with one Codex session; Harness²'s editors
# are Codex sessions, ../harness2_stream/codex_proposer.py), the run names and where Codex's workspaces go.
#
#   examples/officeqa/luna/launch.sh <what> [hermit overrides...]
#     relay                  start the learner relay on RELAY_PORT unless it answers
#     none | initial         evaluate no skill / SkillOpt's initial skill (skillopt/run_codex_flash.yaml, run luna-reference)
#     gepa N | skillopt N    training N (1-4) of {gepa,skillopt}/run_codex_flash.yaml (runs luna-gepa, luna-gepa-r2, ...),
#                            then the evaluation of its final adapter
#     ch                     the Continual Harness stream (continual_harness/stream_codex.yaml, run luna-ch-s0)
#     h2                     the Harness² stream (harness2_stream/run_codex.yaml, run luna-h2-s0)
#
# MODEL (gpt-5.6-luna), LUNA (~/albz/officeqa-luna: the pinned Codex, workspaces, the relay's records, plan.json and
# PAUSE, see ../utils/agent/codex_login.py), CODEX_PIN ($LUNA/codex-pin/0.164.0), RELAY_PORT (3671), RELAY_CONCURRENCY
# (32 Codex sessions at a time for all learners together),
# OFFICEQA_CODEX_IMAGE (officeqa-codex:0.164.0), OFFICEQA_CODEX_PROXY (http://oq-egress-chatgpt:3128, a proxy that lets
# through chatgpt.com only: ../utils/agent/docker/egress_proxy.py with chatgpt.com:443).
set -eu -o pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OQA=$(cd "$HERE/.." && pwd)
FRAMEWORK=$(cd "$OQA/../.." && pwd)
PY=${MW_FRAMEWORK_PYTHON:-$FRAMEWORK/.venv/bin/python}
MODEL=${MODEL:-gpt-5.6-luna}
LUNA=${LUNA:-$HOME/albz/officeqa-luna}
CODEX_PIN=${CODEX_PIN:-$LUNA/codex-pin/0.164.0}
RELAY_PORT=${RELAY_PORT:-3671}
export OFFICEQA_CODEX_IMAGE=${OFFICEQA_CODEX_IMAGE:-officeqa-codex:0.164.0}
export OFFICEQA_CODEX_PROXY=${OFFICEQA_CODEX_PROXY:-http://oq-egress-chatgpt:3128}
export OFFICEQA_CODEX_PLAN=${OFFICEQA_CODEX_PLAN:-$LUNA/plan.json}
export OFFICEQA_CODEX_PAUSE=${OFFICEQA_CODEX_PAUSE:-$LUNA/PAUSE}
export TMPDIR=$LUNA/tmp
export PYTHONPATH=$FRAMEWORK${PYTHONPATH:+:$PYTHONPATH}
export PYTHONUNBUFFERED=1                    # a run's log shows its progress as it goes
mkdir -p "$TMPDIR" "$LUNA/cache" "$LUNA/logs"
[ -x "$CODEX_PIN/bin/codex" ] || { echo "REFUSING: no Codex at $CODEX_PIN/bin/codex"; exit 1; }
"$PY" "$OQA/utils/agent/codex_login.py" > /dev/null || { echo "REFUSING: no ChatGPT login for Codex"; exit 1; }

PLAN="backends.codexplan={base_url: 'https://chatgpt.com/backend-api/codex', key_required: false, api_key_env: null, apis: [chat, responses]}"
RELAY="backends.codexrelay={base_url: 'http://127.0.0.1:$RELAY_PORT/v1', key_required: false, api_key_env: null, apis: [chat]}"
COMMON=("$PLAN" "$RELAY" "agent.params.model=codexplan/$MODEL")
LEARNER=("llm.model=codexrelay/$MODEL" "llm.timeout=3600")       # extra_body (reasoning_effort: medium) stays

relay() {
  if curl -s -m 2 "http://127.0.0.1:$RELAY_PORT/health" > /dev/null; then return; fi
  setsid nohup "$PY" "$OQA/utils/agent/codex_chat.py" --port "$RELAY_PORT" --log-dir "$LUNA/relay" \
    --concurrency "${RELAY_CONCURRENCY:-32}" >> "$LUNA/logs/relay.log" 2>&1 < /dev/null &
  for _ in $(seq 30); do curl -s -m 2 "http://127.0.0.1:$RELAY_PORT/health" > /dev/null && return; sleep 1; done
  echo "REFUSING: the learner relay did not start (see $LUNA/logs/relay.log)"; exit 1
}

hermit() { "$PY" -m hermit.cli "$@"; }
what=${1:?usage: launch.sh relay|none|initial|gepa N|skillopt N|ch|h2 [overrides...]}
shift
case "$what" in
  relay)
    relay ;;
  none|initial)
    adapter=$([ "$what" = none ] && echo none || echo ../utils/seed_adapter)
    hermit eval --config "$OQA/skillopt/run_codex_flash.yaml" --adapter "$adapter" trainer.run_name=luna-reference \
      "agent.params.codex_dir=$LUNA/cache/luna-reference-$what" "${COMMON[@]}" "${LEARNER[@]}" "$@" ;;
  gepa|skillopt)
    n=${1:?training number 1-4}
    shift
    name=luna-$what$([ "$n" = 1 ] || echo "-r$n")
    relay
    args=(trainer.run_name="$name" trainer.tree_dir="runs/trees/$name" "agent.params.codex_dir=$LUNA/cache/$name"
          "${COMMON[@]}" "${LEARNER[@]}")
    hermit train --config "$OQA/$what/run_codex_flash.yaml" "${args[@]}" "$@"
    hermit eval --config "$OQA/$what/run_codex_flash.yaml" --adapter final "${args[@]}" "$@" ;;
  ch)
    relay
    "$PY" "$OQA/continual_harness/make_seed.py"
    hermit train --config "$OQA/continual_harness/stream_codex.yaml" trainer.run_name=luna-ch-s0 \
      trainer.tree_dir=runs/trees/luna-ch-s0 "agent.params.codex_dir=$LUNA/cache/luna-ch-s0" \
      "${COMMON[@]}" "${LEARNER[@]}" "$@" ;;
  h2)
    cd "$OQA/harness2_stream"
    "$PY" make_seed_codex.py
    docs=$("$PY" -c "import json; print(json.load(open('../utils/data.json'))['docs'])")
    "$PY" ../utils/agent/codex_session.py --check "$docs" \
      || { echo "REFUSING: the Codex harness's container is not ready (see ../utils/agent/codex_session.py)"; exit 1; }
    "$PY" stream.py --seed 0 --split ../utils/splits/test.txt --out stream_s0.txt
    cmp -s stream_s0.txt ../continual_harness/stream_s0.txt \
      || { echo "REFUSING: the Harness² stream differs from the Continual Harness stream's order"; exit 1; }
    absent=$("$PY" -c "import json; from pathlib import Path; d = json.load(open('../utils/data.json'));
print(json.dumps([str(Path(d['answers']).parent), d['csv']]))")
    hermit train --config run_codex.yaml trainer.run_name=luna-h2-s0 trainer.tree_dir=runs/trees/luna-h2-s0 \
      data.train=stream_s0.txt "agent.params.codex_dir=$LUNA/cache/luna-h2-s0" "${COMMON[@]}" \
      evolver.params.backend=codex "evolver.params.model=codexplan/$MODEL" \
      evolver.params.proposer=codex_proposer.py:CodexProposer "evolver.params.claude_bin=$CODEX_PIN/bin/codex" \
      "evolver.params.probe_absent=$absent" "sandbox.allow=[chatgpt.com]" "$@" ;;
  *)
    echo "unknown: $what"; exit 1 ;;
esac
