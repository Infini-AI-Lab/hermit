#!/bin/sh
# Tier-0 probe of the v10 L0 kit and sandbox: no model call, no network.
# Builds a scratch workspace exactly as acting.run_actor would (kit.build_kit via
# acting.prepare_actor_workspace, env = build_settings' shell_environment_policy.set, profile =
# acting.client_policy), then runs every probe command as
#   env -i <actor env> /usr/bin/sandbox-exec -p <v9 profile> /bin/zsh -lc|-c <command>
# from the workspace, with a pipe as stdout (as Codex's exec_command gives).
# Usage: tests/probe_kit.sh [SCRATCH_DIR] [ARM]   (ARM: plain | prompt-static | harness-static | harness-gate, the
#        test-only gate-on arm of test_v10_kit.py)
# Output: '=== NN_<probe>__<mode>' sections, each ending with '--- rc=<exit code>'; checks live in test_v10_kit.py.
set -eu
PLUMBING=$(cd "$(dirname "$0")/.." && pwd -P)
PY=${PROBE_PYTHON:-/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python}
S=${1:-$(mktemp -d "${TMPDIR:-/tmp}/probe-kit.XXXXXX")}
ARM=${2:-harness-static}
[ ! -e "$S/ws" ] || { echo "scratch $S already has a workspace" >&2; exit 2; }
"$PY" -B "$PLUMBING/tests/test_v10_kit.py" make "$S" "$ARM"
S=$(cd "$S" && pwd -P)
PROFILE=$(cat "$S/v9.sb")
ENVARGS=$(cat "$S/env.args")
cd "$S/ws"
echo "# scratch=$S arm=$ARM"
for f in "$S"/probes/*.zsh; do
  name=$(basename "$f" .zsh)
  mode=${name##*__}
  printf '=== %s\n' "$name"
  set +e
  # shellcheck disable=SC2086  # env values are validated to contain no whitespace
  out=$(env -i $ENVARGS /usr/bin/sandbox-exec -p "$PROFILE" /bin/zsh "-$mode" "$(cat "$f")" 2>&1 </dev/null)
  rc=$?
  set -e
  printf '%s\n--- rc=%s\n' "$out" "$rc"
done
printf '=== END %s\n' "$ARM"
