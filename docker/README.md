# Offline evaluation in Docker

`offline.sh` runs `hermit check`, `train` and `eval` for the examples in a container that has no route to the
internet except the Fireworks API, which it reaches through an egress proxy that tunnels to `api.fireworks.ai:443`
and refuses everything else. The hermit checkout, the clbench checkout, the OfficeQA dataset and a scratch home are
bind-mounted at their host paths, so a path means the same inside and outside, and every file a run writes belongs
to the host user. The checkout is not copied into the image: `PYTHONPATH` points at the mount, and the container runs
whatever the checkout holds.

## Subcommands

| Command | Network | What it does |
|---|---|---|
| `docker/offline.sh build` | online | Builds `hermit-offline:latest` (`Dockerfile`), its user this user's uid and gid. |
| `docker/offline.sh proxy` | online | Creates the internal network `hermit-offline` and starts the egress container `hermit-egress`; idempotent. |
| `docker/offline.sh prepare` | online | A one-off container on the default network that prepares the benchmark assets on the mounts (below). |
| `docker/offline.sh check` | offline | Proves the isolation from inside, one `PASS` or `FAIL` line per item; exits non-zero on any `FAIL`. |
| `docker/offline.sh run <command...>` | offline | Runs a command in the hermit checkout. Leading `VAR=value` words set its variables, as `env` takes them. |
| `docker/offline.sh shell` | offline | An interactive shell in the same container setup. |

Paths come from the environment: `HERMIT_DIR` (default: the checkout holding the script), `CLBENCH_DIR` (default
`$HOME/code/clbench`), `OFFICEQA_DIR` (required by `prepare` and the OfficeQA runs), `RUN_HOME` (the container's
home, default `${XDG_CACHE_HOME:-$HOME/.cache}/hermit-offline/home`) and `FIREWORKS_KEY_FILE`, the file holding the
Fireworks key, required by `check`, `run` and `shell`. The container gets the key as `FIREWORKS_API_KEY` through a
mode-600 env file that exists only while the container is created; the key is never on a command line and never
written under the checkout. The script has no default for the key file.

`run` returns when the command and everything it left running have ended. The CL-Bench launchers start their wire
meters and `hermit train` in the background and return at once; `run` waits for the trainings (it says what it is
waiting for) and then stops the meters, which write their ledgers on SIGTERM. Ctrl-C ends the container and all of
it. Containers are named `hermit-<subcommand>-<timestamp>-<pid>` and removed when they exit; `docker ps` lists the
live ones, `docker stop <name>` ends one. Runs in separate containers do not share ports: the same `WM_PORT` in two
containers does not collide.

## What is reachable

The offline containers sit on `hermit-offline`, a Docker network created with `--internal`: no route out and no
resolution of outside names, so a direct connection fails before it starts. Their one way out is `hermit-egress`,
a container on that network and on the default one, running `examples/officeqa/utils/agent/docker/egress_proxy.py`
(the Codex harness's): an HTTP CONNECT proxy that opens tunnels to `api.fireworks.ai:443` and answers `403` to any
other target. Every client in the container is pointed at it: `HTTPS_PROXY` and `HTTP_PROXY` (the agents' and
Evolvers' HTTP clients, clbench's litellm, curl), `PROXY` (the wire meters read it and reach their upstream through
it), with `NO_PROXY=127.0.0.1,localhost` so that the meters on the loopback are reached directly. Inside hermit's own
sandboxes the adapter's tools (a context tool that retrieves, say) and the template's checks are isolated further: they see only the
forwarded `127.0.0.1` ports of the run (the meters), whose traffic leaves through the proxy. The acting agent itself
(the rollout role) keeps the container's network, which is the internal one, unless the run sets
`profiles.rollout.network: isolated`. The TLS session is end to end between the client and Fireworks; the proxy sees
host names, not contents.

`docker logs hermit-egress` shows every connection the containers tried, one line each:
`<time> allow|deny CONNECT <host>:<port> from <address>` (`fail` when Fireworks itself could not be reached). A clean
run shows `allow CONNECT api.fireworks.ai:443` only; a `deny` line names a client that tried to go elsewhere.

One client does try to go elsewhere: clbench's litellm fetches its model-price map from `raw.githubusercontent.com`
at every call unless told otherwise, and that map does not know GLM 5.3 Flash on Fireworks, so it would also refuse
the host's `tool_choice="auto"`. The offline containers therefore carry litellm's own switches
`LITELLM_LOCAL_MODEL_COST_MAP=True` (use the shipped map, no fetch) and `LITELLM_DROP_PARAMS=True` (drop a parameter
the map says a model lacks; `auto` is litellm's default, so nothing changes for the host). The egress log then shows
`deny CONNECT raw.githubusercontent.com:443` only from a run started before this note.

`prepare` is the exception: it runs on the default network with no proxy, because it downloads from PyPI, the Python
builds uv fetches, Hugging Face (`products.db`) and `raw.githubusercontent.com` (the OfficeQA split). Run it once
before the offline runs; running it again re-syncs the clbench venv, so not while a run is using it.

## Why the capabilities

hermit runs every agent, learner and adapter tool in a bubblewrap sandbox of its own (`hermit/sandbox.py`): user,
mount, PID and network namespaces, a fresh `/proc` and `/dev`, a positive allowlist of paths. Docker allows an
unprivileged process to make namespaces only with `--cap-add SYS_ADMIN`, `--security-opt seccomp=unconfined` and
`--security-opt apparmor=unconfined`, the list of `codex_sandbox.py`, which runs Codex's bubblewrap the same way. One
more is needed for hermit's `--proc /proc`: `--security-opt systempaths=unconfined` lifts the masks Docker lays over
`/proc` (`/proc/kcore`, `/proc/keys`, ...), without which a fresh `/proc` cannot be mounted in a namespace
(`bwrap: Can't mount proc on /newroot/proc: Operation not permitted`). The container's user is unprivileged, so what
the masks hid stays root's (`/proc/kcore` is mode 0400, `/proc/sys` is read-only to it). `NET_RAW`, `MKNOD` and
`AUDIT_WRITE` are dropped as `codex_sandbox.py` drops them, and `label=disable` keeps an SELinux host from refusing
the bind mounts. The containers are not privileged, have no Docker socket and are not on the host network.
`prepare` gets none of the sandbox additions.

## Mounts at identical paths

| Mount | Access | Why the same path |
|---|---|---|
| `HERMIT_DIR` | read-write | `PYTHONPATH`; the examples' `utils/` (tasks, splits, `data.json` with absolute paths, the `documents` link) and `runs/`. |
| `CLBENCH_DIR` | read-write | `.venv/bin/python` is a link to the interpreter under `.uv-python/`, and `pyvenv.cfg` names it by absolute path; hermit's rollout sandbox grants `$MWM_CLBENCH` and binds the interpreter the venv names. `prepare` puts the interpreter, the venv and uv's cache under the checkout (`UV_PYTHON_INSTALL_DIR`, `UV_CACHE_DIR`) so they resolve on the host too. A venv made on the host with an interpreter elsewhere does not resolve inside. |
| `OFFICEQA_DIR` | read-only | `examples/officeqa/utils/documents` links to it and `data.json` names it. |
| `RUN_HOME` | read-write | `HOME` inside: `~/.cache` (uv, Hugging Face, the tests' `hermit-tests`), a CLI agent's home; the launchers' `$HOME/code/...` defaults land here unless overridden. |
| `/tmp` | tmpfs | A fresh, memory-backed `/tmp` per container (`exec`, 8 GB); hermit's sandbox bridges live there. |

The container's Python is `/opt/venv/bin/python` (Ubuntu's 3.12 with `pyproject.toml`'s pinned dependencies, its
`test` and `skillopt` extras, openai, huggingface_hub and uv; `/opt/venv/pip-freeze.txt` records the versions);
`run` sets `MW_FRAMEWORK_PYTHON` to it, so the launchers use it instead of the checkout's `.venv`. `hermit` is on
`PATH`. The Codex CLI (the learner of the `prompt_only` and `mwm` examples) is in the image, release `CODEX_VERSION`
of the Dockerfile (`CODEX_VERSION= docker/offline.sh build` leaves it out). hermit itself is not installed in the
image; `hermit_dist.py` registers the distribution's metadata so that `hermit/sandbox.py` finds the framework's
dependencies when it binds them into a sandbox.

## Where runs land

- Configs run directly (`hermit train --config examples/<example>/<method>/run.yaml`) write under the mounted
  checkout: `examples/<example>/<method>/runs/<run name>/`, with `eval/<selector>/summary.json` for `hermit eval`.
  The OfficeQA launcher also keeps its meter's ledger and log there (`runs/wire_ledger-<port>.json`).
- The CL-Bench launchers write `$MW_RUNS/<tag>/`: the meters' ledgers and logs, `order<i>/run.log`, `order<i>/task/`
  and the run `<tag>-o<i>/` (`steps.jsonl` holds the scores). Give `MW_RUNS` a path under a read-write mount; its
  default, `$HOME/code/metaworld-runs`, lands in `RUN_HOME`.

## Running the examples

Once, online:

```bash
export HERMIT_DIR=/path/to/hermit CLBENCH_DIR=/path/to/clbench OFFICEQA_DIR=/path/to/officeqa
export FIREWORKS_KEY_FILE=/path/to/fireworks.key
docker/offline.sh build
docker/offline.sh proxy
docker/offline.sh prepare
docker/offline.sh check
```

Then offline. `run` presets `FIREWORKS_API_KEY`, `MWM_CLBENCH=$CLBENCH_DIR` and `MW_FRAMEWORK_PYTHON`, so the
launchers need neither `MW_KEY_FILE` nor `MWM_CLBENCH`; the other variables are the launchers' own (their headers
list them). The commands run from the checkout root on the host, in the checkout root inside:

```bash
# CL-Bench database, MWM (the Codex learner through its meter)
docker/offline.sh run MW_TAG=mwm_db_flash WM_PORT=3401 MW_ORDERS="0 1 2 3 4" \
  MW_RUNS=$HERMIT_DIR/examples/clbench_database/runs examples/clbench_database/mwm/launch.sh

# CL-Bench database, the prompt-only baseline
docker/offline.sh run MW_TAG=po_db_flash WM_PORT=3441 MW_ORDERS="0 1 2 3 4" \
  MW_RUNS=$HERMIT_DIR/examples/clbench_database/runs examples/clbench_database/prompt_only/launch.sh
```

A smoke first: `MW_TRAIN_SPLIT=<a file with a few ids> MW_ORDERS=0` for the CL-Bench launchers. The scores are where
the examples' READMEs say: the mean score in a CL-Bench run's `steps.jsonl`.
`docker/offline.sh shell` opens a shell in the same setup for looking around; the proxy keeps logging.

To take the setup down: `docker rm -f hermit-egress && docker network rm hermit-offline`.
