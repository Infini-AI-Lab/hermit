#!/bin/bash
# The examples offline: hermit check, train and eval in a container with no route to the internet but the Fireworks
# API, reached through an egress proxy (examples/officeqa/utils/agent/docker/egress_proxy.py, the Codex harness's).
# The hermit checkout, the clbench checkout, the OfficeQA dataset and a scratch home are bind-mounted at their host
# paths, so a path means the same inside and outside (a venv's interpreter, data.json, a run's log).
#
#   docker/offline.sh build               the image hermit-offline:latest, its user this user's uid and gid
#   docker/offline.sh proxy               the internal network hermit-offline and the egress container hermit-egress
#                                         (idempotent; `docker logs hermit-egress` lists every connection tried)
#   docker/offline.sh prepare             ONLINE: the benchmark assets, written to the mounts
#   docker/offline.sh check               OFFLINE: prove the isolation, PASS or FAIL per item
#   docker/offline.sh run <command...>    OFFLINE: a command in the hermit checkout; leading VAR=value words set its
#                                         variables (as `env` takes them); returns when the command and everything it
#                                         left running have ended, with the command's status; MW_WAIT=1 makes the
#                                         examples' launchers wait for their trainings and fail when one does
#   docker/offline.sh shell               OFFLINE: an interactive shell
#
#   HERMIT_DIR          the hermit checkout (default: the one holding this script); read-write
#   CLBENCH_DIR         the clbench checkout (default $HOME/code/clbench); read-write: prepare puts its .venv,
#                       .uv-python and .uv-cache there
#   OFFICEQA_DIR        the OfficeQA dataset, officeqa_full.csv and treasury_bulletins_parsed/; read-only; required
#                       by prepare and the officeqa runs
#   RUN_HOME            a scratch directory mounted as the container's home (default
#                       ${XDG_CACHE_HOME:-$HOME/.cache}/hermit-offline/home); read-write
#   FIREWORKS_KEY_FILE  the file holding the Fireworks key, required by check, run and shell: the container gets it as
#                       FIREWORKS_API_KEY through a mode-600 env file of its own, removed once the container is
#                       created, never on a command line
#   CODEX_VERSION       build only: the Codex CLI release fetched from npm (the Dockerfile's default; empty: none)
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
HERMIT_DIR=${HERMIT_DIR:-$(cd "$HERE/.." && pwd)}
CLBENCH_DIR=${CLBENCH_DIR:-$HOME/code/clbench}
OFFICEQA_DIR=${OFFICEQA_DIR:-}
RUN_HOME=${RUN_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/hermit-offline/home}
IMAGE=hermit-offline:latest
NETWORK=hermit-offline
EGRESS=hermit-egress
PROXY_URL=http://$EGRESS:3128
USER_IDS=$(id -u):$(id -g)
STAMP=$(date +%Y%m%d-%H%M%S)-$$
# Docker's default capabilities less three the runs do not need (codex_sandbox.py's list); label=disable keeps an
# SELinux host from refusing the mounts. SANDBOX adds what hermit's bubblewrap sandboxes need: SYS_ADMIN with seccomp
# and AppArmor off to make namespaces, and Docker's masks over /proc lifted (systempaths), without which a fresh
# /proc cannot be mounted in a namespace ("Can't mount proc on /newroot/proc"); the user is unprivileged, so what the
# masks hid (/proc/kcore, /proc/sys writes) stays root's.
DROPS=(--cap-drop NET_RAW --cap-drop MKNOD --cap-drop AUDIT_WRITE --security-opt label=disable)
SANDBOX=(--cap-add SYS_ADMIN --security-opt seccomp=unconfined --security-opt apparmor=unconfined
         --security-opt systempaths=unconfined)

refuse() { echo "REFUSING: $*" >&2; exit 1; }
need_dir() { [ -n "${!1:-}" ] || refuse "$1 is not set"; [ -d "${!1}" ] || refuse "$1=${!1} is not a directory"; }
TTY=(-i); [ -t 0 ] && [ -t 1 ] && TTY=(-it)

# Inside the container around `run`: the command, then everything it left running. The examples' launchers start
# their wire meters and `hermit train` in the background and return; the container must not end with them. So wait
# for every process but the wire meters (and this shell's own helpers), then stop the meters, which write their
# ledgers on SIGTERM.
WRAP='
env MW_WAIT=1 "$@"; status=$?
told=
while :; do
  work=(); meters=()
  for p in /proc/[0-9]*; do
    p=${p#/proc/}
    [ "$p" != 1 ] && [ "$p" != "$$" ] || continue
    ppid=; state=
    while read -r k v _; do case $k in PPid:) ppid=$v;; State:) state=$v;; esac; done 2>/dev/null < "/proc/$p/status" || continue
    [ "$ppid" != "$$" ] && [ "$state" != Z ] || continue
    mapfile -d "" -t argv 2>/dev/null < "/proc/$p/cmdline" || continue
    meter=; for a in "${argv[@]}"; do case $a in *wire_meter.py|hermit.runtime.wire_meter) meter=1;; esac; done
    line=${argv[*]}
    if [ -n "$meter" ]; then meters+=("$p"); else work+=("$p: ${line:0:160}"); fi
  done
  [ ${#work[@]} -gt 0 ] || break
  [ -n "$told" ] || { told=1; echo "offline.sh: the command returned $status; waiting for what it left running:"; printf "  %s\n" "${work[@]}"; }
  sleep 20
done
if [ ${#meters[@]} -gt 0 ]; then kill -TERM "${meters[@]}" 2>/dev/null; sleep 3; fi
exit $status
'

# The isolation, seen from inside: no direct route out, the proxy refuses other hosts and passes the Fireworks API
# (status codes only; the key never leaves the environment), bubblewrap makes namespaces as hermit uses them, and
# the framework's Python has its dependencies.
CHECK='
fail=0
item() { if [ "$1" = 0 ]; then echo "PASS  $2"; else echo "FAIL  $2"; fail=1; fi; }
curl --noproxy "*" -s -m 5 -o /dev/null https://example.com && direct=0 || direct=$?
[ "$direct" != 0 ]; item $? "no direct route out (curl https://example.com without the proxy: exit $direct)"
curl --noproxy "*" -s -m 5 -o /dev/null http://1.1.1.1/ && direct=0 || direct=$?
[ "$direct" != 0 ]; item $? "no direct route out by address either (curl http://1.1.1.1/: exit $direct)"
code=$(curl -s -m 10 -o /dev/null -w "%{http_connect}" -x "$PROXY" https://example.com || true)
[ "$code" = 403 ]; item $? "the egress proxy refuses other hosts (CONNECT example.com:443: $code)"
code=$(curl -s -m 20 -o /dev/null -w "%{http_code}" -x "$PROXY" -H @- https://api.fireworks.ai/inference/v1/models <<<"Authorization: Bearer $FIREWORKS_API_KEY" || true)
[ "$code" = 200 ]; item $? "the egress proxy passes the Fireworks API (GET /inference/v1/models: $code)"
bwrap --unshare-all --ro-bind / / /bin/true 2>/dev/null; item $? "bubblewrap makes namespaces (bwrap --unshare-all)"
bwrap --die-with-parent --new-session --unshare-pid --unshare-net --dev /dev --proc /proc --tmpfs /tmp --ro-bind /usr /usr \
  --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/lib64 /lib64 /bin/true 2>/dev/null
item $? "bubblewrap mounts /proc, /dev and /tmp as hermit sandboxes do"
python -c "import mcp, jsonschema, yaml, openai, huggingface_hub, skillopt" 2>/dev/null; item $? "/opt/venv has the framework and example dependencies"
exit $fail
'

# The benchmark assets, on the mounts: clbench's interpreter and venv under its checkout (uv resolves them at the same
# path on the host), the database venue's products.db, then the examples' prepare scripts.
PREPARE='
set -eu
echo "== clbench: uv sync, the interpreter and venv on the mount"
cd "$CLBENCH_DIR"
export UV_PYTHON_INSTALL_DIR=$CLBENCH_DIR/.uv-python UV_CACHE_DIR=$CLBENCH_DIR/.uv-cache UV_PYTHON_PREFERENCE=only-managed
uv sync --extra database_exploration --extra poker
echo "== clbench: the database venue'\''s products.db"
"$CLBENCH_DIR/.venv/bin/python" -c "from src.tasks.database_exploration.task import DatabaseExploration as T; T.setup()"
cd "$HERMIT_DIR"
echo "== examples/clbench_database"
python examples/clbench_database/utils/prepare.py --clbench "$CLBENCH_DIR"
echo "== examples/clbench_poker"
python examples/clbench_poker/utils/prepare.py --clbench "$CLBENCH_DIR"
echo "== examples/officeqa"
python examples/officeqa/utils/prepare.py --csv "$OFFICEQA_DIR/officeqa_full.csv" --docs "$OFFICEQA_DIR/treasury_bulletins_parsed"
'

ENVFILE=
cleanup() { [ -z "$ENVFILE" ] || rm -f "$ENVFILE"; }
trap cleanup EXIT

key_envfile() {  # FIREWORKS_API_KEY for the container, from FIREWORKS_KEY_FILE, in a mode-600 file of its own
  [ -n "${FIREWORKS_KEY_FILE:-}" ] || refuse "FIREWORKS_KEY_FILE is not set (the file holding the Fireworks key)"
  [ -r "$FIREWORKS_KEY_FILE" ] || refuse "cannot read FIREWORKS_KEY_FILE=$FIREWORKS_KEY_FILE"
  local key
  key=$(tr -d '[:space:]' < "$FIREWORKS_KEY_FILE")
  [ -n "$key" ] || refuse "FIREWORKS_KEY_FILE=$FIREWORKS_KEY_FILE is empty"
  ENVFILE=$(umask 077 && mktemp "${TMPDIR:-/tmp}/hermit-offline-env.XXXXXX")
  printf 'FIREWORKS_API_KEY=%s\n' "$key" > "$ENVFILE"
}

common_args() {  # what every container that works on the checkouts gets: identity, mounts, the framework's variables
  need_dir HERMIT_DIR; need_dir CLBENCH_DIR
  mkdir -p "$RUN_HOME"
  ARGS=(--rm --init --user "$USER_IDS" "${DROPS[@]}"
        --mount "type=bind,src=$HERMIT_DIR,dst=$HERMIT_DIR"
        --mount "type=bind,src=$CLBENCH_DIR,dst=$CLBENCH_DIR"
        --mount "type=bind,src=$RUN_HOME,dst=$RUN_HOME"
        --tmpfs /tmp:exec,size=8g
        -w "$HERMIT_DIR"
        -e "HOME=$RUN_HOME" -e "PYTHONPATH=$HERMIT_DIR" -e MW_FRAMEWORK_PYTHON=/opt/venv/bin/python
        -e "HERMIT_DIR=$HERMIT_DIR" -e "CLBENCH_DIR=$CLBENCH_DIR" -e "MWM_CLBENCH=$CLBENCH_DIR"
        # clbench's host calls through litellm: offline it cannot fetch its model map from GitHub (the proxy denies
        # it, and every call would first wait on that), so it uses the map it ships; and that map does not know
        # GLM 5.3 Flash on Fireworks, so litellm would refuse the host's tool_choice="auto" (its default anyway)
        # unless unsupported parameters are dropped. Both are litellm's own switches.
        -e LITELLM_LOCAL_MODEL_COST_MAP=True -e LITELLM_DROP_PARAMS=True)
  if [ -n "$OFFICEQA_DIR" ]; then
    need_dir OFFICEQA_DIR
    ARGS+=(--mount "type=bind,src=$OFFICEQA_DIR,dst=$OFFICEQA_DIR,readonly" -e "OFFICEQA_DIR=$OFFICEQA_DIR")
  fi
}

offline_args() {  # the internal network, the proxy for every client (the wire meters read PROXY), the key, bubblewrap
  [ "$(docker container inspect -f '{{.State.Running}}' "$EGRESS" 2>/dev/null)" = true ] \
    || refuse "the egress proxy $EGRESS is not running; run $0 proxy first"
  key_envfile
  ARGS+=(--network "$NETWORK" --env-file "$ENVFILE" "${SANDBOX[@]}"
         -e "HTTPS_PROXY=$PROXY_URL" -e "HTTP_PROXY=$PROXY_URL" -e "https_proxy=$PROXY_URL" -e "http_proxy=$PROXY_URL"
         -e NO_PROXY=127.0.0.1,localhost -e no_proxy=127.0.0.1,localhost -e "PROXY=$PROXY_URL")
}

start() {  # create, drop the env file, then start attached: the key file exists only while the container is created
  local id
  id=$(docker create "$@")
  cleanup; ENVFILE=
  docker start -ai "$id"
}

cmd_build() {
  need_dir HERMIT_DIR
  [ -f "$HERMIT_DIR/docker/Dockerfile" ] || refuse "$HERMIT_DIR/docker/Dockerfile is missing"
  local extra=()
  [ -z "${CODEX_VERSION+x}" ] || extra=(--build-arg "CODEX_VERSION=$CODEX_VERSION")
  docker build -f "$HERMIT_DIR/docker/Dockerfile" --build-arg "UID=$(id -u)" --build-arg "GID=$(id -g)" "${extra[@]}" \
    -t "$IMAGE" "$HERMIT_DIR"
}

cmd_proxy() {
  docker network inspect "$NETWORK" >/dev/null 2>&1 \
    || { docker network create --internal "$NETWORK" >/dev/null; echo "created the internal network $NETWORK"; }
  local state
  state=$(docker container inspect -f '{{.State.Status}}' "$EGRESS" 2>/dev/null || true)
  case "$state" in
    running) echo "$EGRESS is running";;
    "") docker run -d --name "$EGRESS" --restart unless-stopped --init --read-only --cap-drop ALL --user "$USER_IDS" \
          "$IMAGE" python3 /opt/egress_proxy.py 3128 api.fireworks.ai:443 >/dev/null
        echo "started $EGRESS on the default network";;
    *) docker start "$EGRESS" >/dev/null; echo "started $EGRESS again (was $state)";;
  esac
  docker container inspect -f '{{range $k, $_ := .NetworkSettings.Networks}}{{$k}} {{end}}' "$EGRESS" | grep -qw "$NETWORK" \
    || docker network connect "$NETWORK" "$EGRESS"
  echo "egress: $PROXY_URL on $NETWORK, tunnels to api.fireworks.ai:443 and nothing else (docker logs $EGRESS)"
}

cmd_prepare() {
  need_dir OFFICEQA_DIR
  common_args
  local hf=()
  [ -z "${HF_TOKEN:-}" ] || hf=(-e HF_TOKEN)
  docker run "${TTY[@]}" --name "hermit-prepare-$STAMP" "${ARGS[@]}" "${hf[@]}" "$IMAGE" bash -c "$PREPARE"
}

cmd_check() {
  common_args; offline_args
  start "${TTY[@]}" --name "hermit-check-$STAMP" "${ARGS[@]}" "$IMAGE" bash -c "$CHECK"
}

cmd_run() {
  [ $# -gt 0 ] || refuse "run needs a command"
  common_args; offline_args
  start "${TTY[@]}" --name "hermit-run-$STAMP" "${ARGS[@]}" "$IMAGE" bash -c "$WRAP" offline.sh "$@"
}

cmd_shell() {
  common_args; offline_args
  start -it --name "hermit-shell-$STAMP" "${ARGS[@]}" "$IMAGE" bash
}

case "${1:-}" in
  build|proxy|prepare|check|run|shell) cmd=$1; shift; "cmd_$cmd" "$@";;
  *) sed -n '2,/^set -eu/{/^set -eu/d;s/^# \{0,1\}//p}' "$0"; exit 2;;
esac
