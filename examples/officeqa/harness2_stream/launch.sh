#!/bin/bash
# Harness² under the paper's protocol on OfficeQA: one stream of the test split through `hermit train`, as the
# CL-Bench examples run theirs.
#
#   FIREWORKS_API_KEY=... SEED=0 WM_PORT=3591 examples/officeqa/harness2_stream/launch.sh [overrides...]
#   EDITOR_MODEL=accounts/fireworks/models/glm-5p3 ...     # the editors' model (default glm-5p3-flash)
#   CONFIG=run_codex_luna.yaml examples/officeqa/harness2_stream/launch.sh [overrides...]
#                                                          # the Flash row with GPT-5.6-Luna: executor and editors on the
#                                                          # machine's ChatGPT login, no key and no meter; the editors are
#                                                          # Codex sessions of a pinned copy of the
#                                                          # machine's Codex (CODEX_BIN, default the `codex` on PATH)
#   CONFIG=run_claude_haiku.yaml examples/officeqa/harness2_stream/launch.sh [overrides...]
#                                                          # Claude Haiku 5.5 on the user's Claude subscription: executor
#                                                          # and editors on `login: claude` (CLAUDE_CODE_OAUTH_TOKEN, from
#                                                          # `claude setup-token`), no key and no meter; the editors are
#                                                          # Claude Code sessions of a pinned copy of the machine's `claude`
#   MAX_TASKS=2 TAG=h2-smoke ...                           # a smoke run on the first questions of the stream
#   SKIP_TASKS=10 ...                                      # drop the stream's first questions (continuing a run that took them)
#   LIBRARY_FROM=runs/h2-codex-s0 TAG=h2-codex-s0-rest STREAM=stream_rest_s0.txt \
#     SPLITS="../utils/splits/train.txt ../utils/splits/val.txt" CLAUDE_BIN=runs/.claude-pin/<version>/claude ...
#                                                          # that stream continued on other questions (paths: from here)
#
# Writes upstream's stream order for SEED, regenerates H0 (seed_adapter_codex) and trains. With Claude Code editors
# (run_codex.yaml) it starts a wire meter in front of Fireworks for the editor sessions (bucket TAG; ledger
# runs/TAG.wire_ledger.json) and trains with the editor model bound to the meter; with Codex editors
# (run_codex_luna.yaml, `evolver.params.backend: codex`) or Claude Code editors on the user's subscription
# (run_claude_haiku.yaml, `evolver.params.login: claude`) the config names the editors' model and the sessions reach it
# through the evolver sandbox's allowlist proxy. Needs the `harness2` and `skillopt` dependency groups (python -m pip
# install -e . --group harness2 --group skillopt), the Claude Code CLI (or Codex, for run_codex_luna.yaml), bubblewrap
# and, for an executor in a container (`environment.image`: the Codex configs, run_claude_haiku.yaml), the agent's
# container: the image, the network and the egress proxy of the config (docker/codex/ builds Codex's, docker/claude/
# Claude Code's; the agent names what is missing before its first session).
set -eu -o pipefail
if [ -n "${LIBRARY_FROM:-}" ]; then          # a continuation names what differs from the stream it continues
  for v in TAG SPLITS STREAM CLAUDE_BIN; do
    [ -n "${!v:-}" ] || { echo "REFUSING: a continuation (LIBRARY_FROM) needs $v"; exit 1; }
  done
fi
HERE=$(cd "$(dirname "$0")" && pwd)
FRAMEWORK=$(cd "$HERE/../../.." && pwd)
PY=${MW_FRAMEWORK_PYTHON:-$FRAMEWORK/.venv/bin/python}
SEED=${SEED:-0}
CONFIG=${CONFIG:-run_codex.yaml}
[ -f "$HERE/$CONFIG" ] || { echo "REFUSING: no $CONFIG beside this launcher"; exit 1; }
# The editors' program: Claude Code unless the config says `evolver.params.backend: codex`; their sign-in: the
# config's `evolver.params.login` (- for none: a model behind the wire meter). The run's name: the config's run name
# (h2-codex-s0, luna-h2-s0, haiku-h2-s0) with the seed in place of its 0.
read -r EDITORS LOGIN ON_CLAUDE_LOGIN ACTING <<< "$("$PY" -c "import yaml; c = yaml.safe_load(open('$HERE/$CONFIG'));
e = (c.get('evolver') or {}).get('params') or {}; a = c.get('agent') or {}; m = a.get('model')
logins = {e.get('login'), m.get('login') if isinstance(m, dict) else None, (a.get('params') or {}).get('login')}
print(e.get('backend') or 'claude', e.get('login') or '-', 'yes' if 'claude' in logins else 'no',
      ('claude' if a.get('base') in ('claude', 'claude-code') or a.get('name') == 'claude-code'
       or (a.get('name') == 'cli' and (a.get('params') or {}).get('backend') == 'claude')
       else a.get('base') or a.get('name') or '-')
      + ('-in-container' if (c.get('environment') or {}).get('image')
         and (c.get('sandbox') or {}).get('enabled', True) is not False else ''))")"
# A session on `login: claude` signs in with the token alone, from this environment: refuse before anything runs.
if [ "$ON_CLAUDE_LOGIN" = yes ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  echo "REFUSING: $CONFIG signs in with login: claude and CLAUDE_CODE_OAUTH_TOKEN is not set: run \`claude setup-token\`"
  echo "once and export the token (docs/guides/claude-subscription.md)"; exit 1
fi
RUN_NAME=$("$PY" -c "import yaml; print(yaml.safe_load(open('$HERE/$CONFIG'))['trainer']['run_name'])")
case "$RUN_NAME" in *s0) TAG=${TAG:-${RUN_NAME%0}$SEED} ;; *) TAG=${TAG:-$RUN_NAME} ;; esac
SPLITS=${SPLITS:-../utils/splits/test.txt}   # the stream's questions (several files: their union)
STREAM=${STREAM:-stream_s$SEED.txt}
LIBRARY_FROM=${LIBRARY_FROM:-}               # a finished run whose library this run starts from: a continuation
case "$EDITORS/$LOGIN" in
  claude/claude)       # the user's Claude subscription: the config names the editors' model; no key, no meter
    [ -z "${EDITOR_MODEL:-}${WM_PORT:-}" ] || { echo "REFUSING: $CONFIG names the editors' model on login: claude;"\
      "unset EDITOR_MODEL and WM_PORT"; exit 1; }
    command -v claude > /dev/null || { echo "REFUSING: the Claude Code CLI (claude) is not on PATH"; exit 1; } ;;
  claude/-)            # a model behind a wire meter in front of Fireworks
    EDITOR_MODEL=${EDITOR_MODEL:-accounts/fireworks/models/glm-5p3-flash}
    WM_PORT=${WM_PORT:?set WM_PORT, the wire meter port for the editor sessions}
    : "${FIREWORKS_API_KEY:?set FIREWORKS_API_KEY}"
    command -v claude > /dev/null || { echo "REFUSING: the Claude Code CLI (claude) is not on PATH"; exit 1; } ;;
  codex/*)
    CODEX_BIN=${CODEX_BIN:-$(command -v codex || true)}
    [ -n "$CODEX_BIN" ] || { echo "REFUSING: no Codex (codex) on PATH and no CODEX_BIN"; exit 1; } ;;
  *) echo "REFUSING: evolver.params backend/login in $CONFIG is '$EDITORS/$LOGIN', not claude/claude, claude (no"\
       "login), or codex"; exit 1 ;;
esac
METER=no; [ "$EDITORS/$LOGIN" = claude/- ] && METER=yes     # the editors' model is bound to the wire meter
export PYTHONPATH=$FRAMEWORK${PYTHONPATH:+:$PYTHONPATH}
cd "$HERE"
if [ -n "$LIBRARY_FROM" ]; then
  [ -d "$LIBRARY_FROM" ] || { echo "REFUSING: LIBRARY_FROM=$LIBRARY_FROM is not a directory"; exit 1; }
  LIBRARY_FROM=$(readlink -f "$LIBRARY_FROM")
  [ "$(readlink -f "runs/$TAG")" != "$LIBRARY_FROM" ] || { echo "REFUSING: TAG=$TAG is the run continued"; exit 1; }
fi
mkdir -p runs
# A run resumed keeps the program it started with (its config's pin), unless CLAUDE_BIN names one: an update of the
# machine's CLI in between does not change it halfway.
if [ -z "${CLAUDE_BIN:-}" ] && [ -f "runs/$TAG/config.yaml" ]; then
  CLAUDE_BIN=$("$PY" -c "import sys, yaml; c = yaml.safe_load(open(sys.argv[1])) or {}
print(((c.get('evolver') or {}).get('params') or {}).get('claude_bin') or '')" "runs/$TAG/config.yaml")
  [ -z "$CLAUDE_BIN" ] || [ -x "$CLAUDE_BIN" ] || { echo "REFUSING: runs/$TAG ran $CLAUDE_BIN, which is gone"; exit 1; }
fi
"$PY" make_seed_codex.py
"$PY" stream.py --seed "$SEED" --split $SPLITS --out "$STREAM" ${MAX_TASKS:+--max "$MAX_TASKS"} ${SKIP_TASKS:+--skip "$SKIP_TASKS"}
# The meters this launch starts stop within a second or two of the processes it names ending (#82). A meter found
# running belongs to the launch that started it: a launch reuses one only if it is still up on a second look.
METERS=()
stop_meters_after() {  # pid...
  [ ${#METERS[@]} -gt 0 ] || return 0
  setsid nohup bash -c 'for p in $1; do while kill -0 "$p" 2>/dev/null; do sleep 1; done; done
    for m in $2; do kill -- "-$m" 2>/dev/null || kill "$m" 2>/dev/null || true; done' _ "$*" "${METERS[*]}" \
    > /dev/null 2>&1 < /dev/null &
}
meter_health() {  # port: the /health of a meter still up three seconds on (one being stopped is gone by then)
  curl -s -m 2 "http://127.0.0.1:$1/health" > /dev/null 2>&1 && sleep 3 \
    && curl -s -m 2 "http://127.0.0.1:$1/health" 2>/dev/null
}
if [ "$EDITORS" = claude ]; then
  # The meter restarts if it dies; each start keeps its own ledger (sum runs/$TAG.wire_ledger-*.json for the bill).
  if [ "$METER" = yes ] && ! meter_health $WM_PORT > /dev/null; then
    setsid nohup bash -c "while true; do PORT=$WM_PORT UPSTREAM=https://api.fireworks.ai \
        LEDGER=$HERE/runs/$TAG.wire_ledger-\$(date -u +%Y%m%dT%H%M%SZ).json '$PY' -m hermit.runtime.wire_meter; \
        sleep 5; done" >> "runs/$TAG.wire_meter.log" 2>&1 < /dev/null &
    METERS+=("$!"); stop_meters_after $$   # the restart loop with its meter, when the run exec'd below ends
    sleep 3
  fi
  # Claude Code updates itself: the run uses a copy of today's binary (or of CLAUDE_BIN, e.g. the stream it continues
  # pinned), alone in its directory (the sessions' sandbox shows the directory a program lives in).
  if [ -n "${CLAUDE_BIN:-}" ]; then
    CLAUDE_BIN=$(readlink -f "$CLAUDE_BIN"); VERSION=$("$CLAUDE_BIN" --version | awk '{print $1}')
  else
    CLAUDE_BIN=$(readlink -f "$(command -v claude)"); VERSION=$(basename "$CLAUDE_BIN")
  fi
  [[ "$VERSION" =~ ^[0-9]+(\.[0-9]+)+$ ]] || { echo "REFUSING: no version for $CLAUDE_BIN ($VERSION)"; exit 1; }
  PINNED="$HERE/runs/.claude-pin/$VERSION/claude"
  if [ -e "$PINNED" ]; then
    cmp -s "$CLAUDE_BIN" "$PINNED" || { echo "REFUSING: $PINNED is not $CLAUDE_BIN"; exit 1; }
  else
    mkdir -p "$(dirname "$PINNED")" && cp "$CLAUDE_BIN" "$PINNED.part.$$" && mv "$PINNED.part.$$" "$PINNED"
  fi
  # An acting agent on Claude Code (agent.base: claude) without a container (no environment.image) runs the `claude` on
  # PATH: the editors' pinned copy, first. In a container it runs the image's (run_claude_haiku.yaml:
  # officeqa-claude:<version>).
  [ "$ACTING" = claude ] && export PATH="$(dirname "$PINNED"):$PATH"
else
  # Codex updates itself too: the run uses a copy of the machine's standalone package (bin/codex with its ripgrep and
  # bubblewrap), alone in its directory, the program the editor sessions run (CLAUDE_BIN: a pin to reuse).
  CODEX_BIN=$(readlink -f "${CLAUDE_BIN:-$CODEX_BIN}"); VERSION=$("$CODEX_BIN" --version | awk '{print $NF}')
  [[ "$VERSION" =~ ^[0-9]+(\.[0-9]+)+$ ]] || { echo "REFUSING: no version for $CODEX_BIN ($VERSION)"; exit 1; }
  PACKAGE=$(dirname "$(dirname "$CODEX_BIN")")
  [ "$CODEX_BIN" = "$PACKAGE/bin/codex" ] || { echo "REFUSING: $CODEX_BIN is not <package>/bin/codex of a standalone Codex"; exit 1; }
  PIN_DIR="$HERE/runs/.codex-pin/$VERSION"; PINNED="$PIN_DIR/bin/codex"
  if [ -e "$PINNED" ]; then
    cmp -s "$CODEX_BIN" "$PINNED" || { echo "REFUSING: $PINNED is not $CODEX_BIN"; exit 1; }
  else
    mkdir -p "$(dirname "$PIN_DIR")" && cp -a "$PACKAGE" "$PIN_DIR.part.$$" && mv "$PIN_DIR.part.$$" "$PIN_DIR"
  fi
fi
# The answer files and the run continued (its records carry the judge's feedback): the sessions' sandbox is checked to
# show none of them.
ABSENT=$("$PY" -c "import json, sys; d = json.load(open('../utils/data.json'));
from pathlib import Path; print(json.dumps([str(Path(d['answers']).parent), d['csv'], *sys.argv[1:]]))" \
  ${LIBRARY_FROM:+"$LIBRARY_FROM"})
if [ "$METER" = yes ]; then
  exec "$PY" -m hermit.cli train --config "$CONFIG" "trainer.run_name=$TAG" "data.train=$STREAM" \
    "backends.learner={from: fireworks, base_url: 'http://127.0.0.1:$WM_PORT/$TAG/inference/v1'}" \
    "evolver.params.model=learner/$EDITOR_MODEL" "evolver.params.probe_absent=$ABSENT" \
    "evolver.params.claude_bin=$PINNED" ${LIBRARY_FROM:+"evolver.params.library_from=$LIBRARY_FROM"} "$@"
fi
exec "$PY" -m hermit.cli train --config "$CONFIG" "trainer.run_name=$TAG" "data.train=$STREAM" \
  "evolver.params.probe_absent=$ABSENT" "evolver.params.claude_bin=$PINNED" \
  ${LIBRARY_FROM:+"evolver.params.library_from=$LIBRARY_FROM"} "$@"
