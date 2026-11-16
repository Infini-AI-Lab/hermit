"""`agent.name: harbor`: one Hermit rollout as one Harbor trial, or one step of a Harbor multi-step task.

    agent:
      name: harbor
      concurrency: 8
      params:
        benchmark: ../utils/benchmark.yaml   # optional: these same keys, shared by every method's config
        tasks_dir: ../utils/tasks            # the Harbor task directories (dataset.py exports or indexes them)
        model: openai/gpt-5.6-luna           # a `<backend>/<model>` reference (the backend's endpoint and key), or
                                             # with `login` the CLI's model name; the answer agent's may be a model
                                             # a CLI reaches ({cli: codex | claude | pi, model, login}, as `llm`'s)
        login: chatgpt                       # Codex on the user's ChatGPT sign-in; claude: Claude Code on the user's
                                             # subscription (CLAUDE_CODE_OAUTH_TOKEN; plan_dir, usage_credits,
                                             # busy_retries, busy_wait_s, login_root as a CLI session's,
                                             # hermit.agent.base.login)
        acting_agent:                        # the agent in the container
          cli: codex                         # codex (the default) | claude | pi | opencode | dsh | mini-swe-agent |
                                             # answer (one model call, no tools: its answer written in the container);
                                             # or name: <Harbor built-in> / import_path: <class> (no adapter)
          settings: {}                       # the CLI Base Agent's own settings over the table's (codex `args`, ...)
          kwargs: {}                         # options the HermitXXX class declares
          env: {}                            # variables of every agent exec in the container (no secrets)
          pythonpath: []                     # directories Harbor must import an agent class from (an example's own)
        effort: medium                       # the CLI's reasoning effort
        web_search: false                    # the CLI's web search, set either way (required; off for a task whose
                                             # agent phase has no public network)
        codex_bin: ~/.codex/packages/standalone/releases/0.164.0-x86_64-unknown-linux-musl/bin/codex
                                             # a CLI's binary on this machine (codex_bin, claude_bin, pi_bin,
                                             # opencode_bin, dsh_bin), its folder mounted read-only at
                                             # /opt/hermit-<cli> and first on PATH, used when it answers --version
                                             # (asked as root) as <cli>_version and the task's agent user may
                                             # execute it; else the container installs
        codex_version: "0.164.0"             # what that install installs (<cli>_version; default: clis.HARBOR_CLIS's)
        setup_timeout_sec: null              # Harbor's agent setup limit (an npm install under load needs more
                                             # than Harbor's 360 s)
        answer: {}                           # the answer agent's: instruction_file, attachments, answer_file,
                                             # answer_pattern, answer_format, wait_s, max_tokens
        pass_reward: 1.0                     # the reward that is a pass ("passed"); null: none is (a score scale)
        verifier_redact: []                  # regexes removed from the curated verifier output (the feedback's tail)
        trial_cache: false                   # a trial of the same row with the same launch is not run again: what a
                                             # resumed run or eval re-runs comes from <jobs_dir>/trial-cache
        agent_timeout_sec: 3600              # uniform agent budget; null: each task's own
        environment: {delete: true}          # Harbor's environment config (type, import_path, force_build, kwargs,
                                             # env, extra_docker_compose: overlays, relative to the benchmark file)
        verifier: {}                         # Harbor's verifier config (override_timeout_sec, ...)
        verifier_timeout: agent              # a verifier that ran out of its time: the agent's result, kept, as
                                             # Harbor grades it (its solution too slow for the tests, or a process it
                                             # left that hung them); infra: run again (a host whose load makes
                                             # verifiers time out)
        verifier_timeout_multiplier: 1.0     # Harbor's: each task's verifier limit times this (a slow host)
        environment_build_timeout_multiplier: 1.0   # Harbor's: each task's environment start (pull, build) limit
                                             # times this; the agent's limit is never scaled
        retry: {max_retries: 0}              # Harbor's retry of a failed trial (not of a stream's step); a failed
                                             # attempt's usage is not counted (Harbor removes its trial)
        reward_key: reward                   # the verifier's reward the judge reads
        jobs_dir: ~/.cache/hermit/harbor/jobs   # one Harbor job per rollout, outside the checkout
        tmpdir: <jobs_dir>/tmp               # TMPDIR for harbor; never /tmp
        image_sizes: ../utils/image_sizes.json   # compressed bytes per task image: the disk gate keeps room for an
                                             # image not pulled yet (2.5x on disk), and for the pulls of other trials
        harbor: ~/.cache/hermit/harbor-venv/bin/harbor   # Harbor's command: a name is found on PATH, a relative
                                                         # path in the working directory
        docker_cleanup: images_keep          # images_keep | rmi_task_image | rmi_over_gb (docker.py)
        rmi_threshold_gb: 1.0
        disk_gate_gb: 20                     # no trial starts while Docker's data root has less than this free
        disk_gate_wait_s: 7200               # ... each gate at most this long, in turn; then infra (disk)
        tmp_gate_gb: 1.0                     # ... nor while the host's /tmp has less (runc writes each exec's
                                             # process file there: full, every agent setup fails)
        tools_host: null                     # the address the adapter's tools are served on (default: the Docker
                                             # bridge's gateway, which a task container reaches)
        sandbox_enforce: required            # the adapter sandbox's enforce (its tools, its per-task context):
                                             # required | best_effort
        context_query: null                  # the row's field a per-task context retrieves for (default: the
                                             # instruction)
        python: <this interpreter>           # the one the hosts' deployment and the per-task context run with
        stream: {idle_s: 14400, start_timeout_s: 3600, turn_wait_s: null}   # a multi-step task's driver: idle stop,
                                             # environment start, how long a step waits for its turn (null: a trial's
                                             # budget for each step before it, at least one, and the start's; a turn
                                             # that cannot come ends at once)

A task row (`dataset.index`) names its directory as `harbor_task`; a step row adds `step`, `step_index`, `steps`.

A trial: the adapter is staged for the acting CLI (`staging.stage`: its host's deployment, the task's context when the
adapter's context is per task), its tools served from this machine (`tools.AdapterTools`), the CLI's launch plan
rendered by Hermit's own CLI Base Agent (`launch.render`: the CLI's home, command and environment for the container),
the job config written, `harbor run -c job.yaml` run in Harbor's own venv as a subprocess, and the trial read back: the
verdict of its `result.json` (`errors.classify`), then the CLI's own stream (`agent/agent-<backend>.jsonl`) read as
Hermit reads a session's (`outcome`, `parse`: served, refused for a plan's limit, a provider that could not serve or
that refused its requests, a lost sign-in, a session that never started or whose tools never came up; the usage,
priced at the run's price: agent.params.price, else the backend's). What the container wrote is read only from regular
files, never through a link it left there (which could name a file of this machine).
A step: the stream's driver (`stream.StreamClient`, `harborside/driver.py`) is started with the first step and keeps
the task's environment; each rollout stages its adapter, renders its plan, waits for its turn, asks for its step and
reads the step's result; the last step (or an abort) stops the driver. A step whose turn cannot come (its driver waits
for a step no rollout of this runner is running: one that ended before it reached the driver, or ran in an earlier
training step; or it has no driver, and no rollout of its first step is running to start one) ends at once, infra
(stream_order), and so does every later step of its stream, whose driver the last of them stops; a driver that answers
no stop (hung in a step) is killed, and its trial's compose projects are taken down. A new driver in a stream's
directory finds the earlier one's trial set aside (`earlier/<n>/`). A stream is the row's rollout of the task, of
the eval's repeat, of the run (`run_identity`, which hermit.isolate.agent_pipeline gives: a hermit train's record
directory, by path and the id it keeps there; any other pipeline's own identity):
`<jobs_dir>/streams/<task>__s<rollout>[__r<repeat>][__<its digest>]`. A pipeline that never resumes (`keep_streams:
false`, which agent_pipeline gives every pipeline but a hermit train's) never meets its streams again: closed (`close`,
or at exit), it stops the drivers of those that did not finish, which would otherwise hold their tasks' environments
for `stream.idle_s`; a hermit train's stay up for a resume until it finishes (Trainer._finish sets its keep_streams
false before the close, which then stops them: a finished run never resumes).

What the rollout returns: `output` {harbor_task, rollout, step, reward, rewards, status, failure_cause,
exception_type, trial, hooks_active, reply (the CLI's last message), answer (the answer agent's)}, the usage, the
curated log (`curate.py`), and `extra` with the verdict, the session's facts, the job, what was deployed and its
per-task context (`context_tool`), `judge` (pass_reward) and, for a trial that is not the agent's result (infra:
the host, the provider, a disk gate, a halt, a session that never started), `unscored` (its cause: the Trainer
keeps it out of every mean). A lost sign-in, or the usage limit of an API account (no login waits it out), writes
`<jobs_dir>/HALT`: while it exists every rollout returns at once as infra/halted, without a container; whoever
restored the login or the account removes it. The trial that halted and every rollout after it say so (`extra.halt`):
a hermit train ends the run there, unfinished (Trainer._raise_halt), and hermit eval runs no infra run again.

On a login (`login: chatgpt` or `claude`) a trial shares the plan with every Hermit session on the login: it waits
while the plan holds sessions back (its PAUSE, MAX_USED, a refusal's hold) and runs in one of its slots; what the
session says of the plan (Claude Code's stream; Codex's session logs, which the launch plan collects) is kept before
the slot is given back, and so is whether the plan refused the session: then every session is held until the window
resets and the trial runs again, so that a usage limit costs time, never a trial. A session the provider could not
serve runs again `busy_retries` times, unless it ran out of the agent's time (its result, as Hermit's own sessions keep
it). What every run of the trial used is the rollout's (each run's own in `extra.attempts` when the trial ran more than
once, or a halt ended it after a run), and a trial neither starts nor runs again once another trial halted the run while
it waited. The answer agent's model reached through a CLI on a login (`model: {cli, login}`: each request one session on
the login, which waits inside the request while the plan holds sessions back) waits for that plan before each trial; a
trial whose one request was still unanswered when the agent's time ran out, its session having waited for that plan
(as the session recorded its own waits: a hold that came while it ran is none), is infra (usage_limit), not the
agent's timeout. That wait does not cover a slot, which the request's session takes itself
(CliAgent.run): a request waiting for one (the plan's SLOTS all taken) waits inside the agent's time, and its trial is
the agent's timeout (its result) if that time runs out; so keep agent.concurrency at most SLOTS less the login's other
sessions. Codex's refreshed sign-in comes back from the container and becomes the shared copy. With `trial_cache`, a
finished trial is kept by its row, its task's files, its adapter and its launch, and a resumed run or eval takes it from
there, spending nothing again, instead of running it again. A training run's trials are its own (`trial_scope`: the
Trainer's pipeline's run_identity), which no other run meets, and each of its epochs' too (the Trainer's rows name their
epoch, `epoch`, when there are more than one), so that an epoch on an adapter the one before left as it was runs anew; a
row asking for a new run (`fresh`: an Evolver's run_agent, a task a split names twice) is neither taken from the cache
nor kept.
"""
from __future__ import annotations

import atexit
import dataclasses
from datetime import datetime
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
import weakref

import yaml

from hermit.adapter.manifest import CARD
from hermit.agent.acting.agent import Agent
from hermit.agent.base import login as subscription
from hermit.agent.base.endpoint import UNSUPPORTED
from hermit.harbor import clis
from hermit.harbor import curate as curate_mod
from hermit.harbor import dataset
from hermit.harbor import docker
from hermit.harbor import launch
from hermit.harbor import stream as stream_mod
from hermit.harbor.errors import classify, no_trial, verifier_disk_full, verifier_unreached
from hermit.harbor.staging import stage
from hermit.harbor.stream import DRIVER_LOG, StreamClient, StreamError, StreamOrderError, TurnLostError
from hermit.task import RunOutput
from hermit.trainer.rollout import FRESH, ROW_KEYS

HARBOR = "~/.cache/hermit/harbor-venv/bin/harbor"
JOBS_DIR = "~/.cache/hermit/harbor/jobs"
CACHE_DIR = "trial-cache"
CACHED = ("ok", "timeout", "agent_error")                        # what a trial ended with that a rerun would not change
HARBORSIDE_ROOT = Path(__file__).resolve().parent                # the directory holding the harborside package
SETUP_BUDGET_S = 360                                             # Harbor's agent setup: apt + nvm + npm + python3
TEARDOWN_BUDGET_S = 600                                          # image pull, container start, compose down
DEFAULT_AGENT_TIMEOUT_S = 3600
TIMEOUT_MULTIPLIERS = ("verifier_timeout_multiplier", "environment_build_timeout_multiplier")   # into Harbor's job
DISK_GATE_GB = 20.0
DISK_GATE_WAIT_S = 7200.0
TMP_GATE_GB = 1.0
HOST_TMP = Path("/tmp")
HALT_FILE = "HALT"
# a variable an acting_agent.env value names: ${NAME} and ${NAME:-default}, which Harbor fills in from this
# process's environment (harbor.utils.env.resolve_env_vars), and $NAME, which it hands on as that text
ENV_REF = re.compile(r"\$(?:\{(\w+)(?::-[^}]*)?\}|(\w+))")
NESTED = ("acting_agent", "environment", "verifier", "retry", "stream", "answer")   # params merged one level deep
BINS = ("codex", "claude", "pi", "opencode", "dsh")              # the CLIs a binary of this machine may be mounted for
# What has Harbor's own codex agent sign in with the user's ChatGPT file (~/.codex/auth.json, or the file named)
CODEX_SIGN_IN = ("CODEX_FORCE_AUTH_JSON", "CODEX_AUTH_JSON_PATH")
TOOLS_PLACEHOLDER = "http://0.0.0.0:0/" + "0" * 32 + "/mcp"      # a dry render's tools URL (the cache key's plan)
ANSWER_PLACEHOLDER = "http://127.0.0.1:0/" + "0" * 32            # ... and the answer agent's endpoint
SESSION_FACTS = ("served", "refused", "unavailable", "login_failed", "rejected", "model_refusal", "held_until",
                 "served_model", "plan_report", "started", "tools_down", "finish_reason", "calls", "statuses")
# A rejection that refused the run's configuration, which no input to the session changes, as the CLI's own notices
# (the session's `failure`) name it: its key or token (401, unauthorized, a failed authentication, an invalid key or
# token, a key not valid, not logged in, OpenCode's provider.auth), its permission (403, forbidden, no access), its
# model or endpoint (404, not found, a model that does not or may not exist, an invalid or unknown model, one that is
# not a valid model), its account's bill (402, a credit balance too low, an insufficient quota, billing; not a rate
# limit's advice, "billing details" or a link to the billing page). A status is read only where one is written: leading
# the text or a line of it (Pi's "401 {...}"), after the prefix Hermit's endpoint gives a CLI's failure in its reply
# ("...(exit 1): 401 {...}", "...(timed out): "; endpoint._no_reply), after "API Error: " (Claude Code's) or
# "Error code: " (OpenAI's SDK), in "(HTTP 401)" (OpenCode's) or in "API error (401)" (Pi's), after "status" ("status
# code", "status_code") or a JSON "code"; never another number of the text (a token count: "you requested 4402 tokens
# (402 in the messages, ...)", "input tokens (402): 403 over"). So is a request setting the model does not take (an
# unsupported parameter or value: max_tokens where it takes max_completion_tokens, an effort it has not; Azure's
# unrecognized request argument; OpenCode's provider.unsupported-operation), and what Hermit's own endpoint refuses a
# CLI session cannot take (its code, hermit.agent.base.endpoint.UNSUPPORTED: config_refused). Any other rejection (a
# prompt too long, an invalid request, a content filter) is what the adapter's or the task's input made the request:
# the agent's result. (A rate limit or an overload is `unavailable`, read before any rejection.)
CONFIG_REFUSED = re.compile(r"(?:^|\((?:exit -?\d+|timed out)\): |\bAPI error(?:: | \()|\bError code: |"
                            r"(?:\bstatus(?:[ _]code)?|\"code\")\"?:? ?\"?)40[1-4]\b|\(HTTP 40[1-4]\)|"
                            r"unauthori[sz]ed|forbidden|authenticat|\bauth\b|not logged in|"
                            r"invalid[\w -]{0,20}?(?:key|token)\b|\b(?:key|token) not valid|permission|access denied|"
                            r"not have access|not[ _]found|(?:may|does|model) not exist|(?:invalid|unknown)[ _]model|"
                            r"not a valid model|credit balance is too low|insufficient[_ ]?quota|"
                            r"(?<!/)\bbilling(?! details)|unsupported[ _](?:parameter|value)|"
                            r"unrecognized request argument|unsupported[ _-]operation", re.I | re.M)
CONFIG_STATUSES = (401, 402, 403, 404)                           # ... the answer agent's: its model endpoint's status


_read = curate_mod.regular_text        # what the container wrote: a regular file's text, never read through a link


def _copy(src: Path, dst: Path) -> bool:
    """A regular file at `src` (never through a link: curate.open_regular) copied to `dst`; whether it was."""
    fd = curate_mod.open_regular(src)
    if fd is None:
        return False
    with os.fdopen(fd, "rb") as f:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(dst, "wb") as out:
                shutil.copyfileobj(f, out)
        except OSError:
            return False
    return True


def _folder(path: Path) -> bool:
    """Whether `path` is a directory itself, not a link to one (lstat)."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _text(path: Path) -> str:
    """A regular file's text, or "" when it is missing, unreadable or no regular file (never read through a link)."""
    return _read(path) or ""


_LIVE: "weakref.WeakSet[HarborAgent]" = weakref.WeakSet()   # runners whose streams may hold a tool server or a driver


@atexit.register
def _close_all() -> None:
    """At exit, what the streams still open hold (a run stopped between a stream's steps): their tool servers, and the
    drivers of a pipeline that never resumes (HarborAgent.close)."""
    for agent in list(_LIVE):
        agent.close()


def _events(raw: str):
    """The JSON objects of a CLI's stream, one per line (a line that is none, or one nested past the JSON parser's
    depth, is skipped; U+0085, U+2028 and U+2029 end no line, hermit.agent.base.cli._objects)."""
    for line in raw.split("\n"):
        if not line.lstrip().startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):        # no JSON, or nested past the parser's depth
            continue
        if isinstance(event, dict):
            yield event


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(text))


def config_refused(facts: dict) -> bool:
    """Whether a session's rejection refused the run's configuration (CONFIG_REFUSED): the answer agent's by its model
    endpoint's last status, or by its message beside a 400 (the one status Hermit's endpoint answers a CLI session the
    provider rejected with, whatever the reason: endpoint._no_reply), or by that 400's code when Hermit's endpoint
    refused what a CLI session cannot take (UNSUPPORTED: a figure, tool calls, another model; settings the session
    cannot start with); a CLI's by its own notices."""
    statuses = facts.get("statuses") or []
    said = bool(CONFIG_REFUSED.search(str(facts.get("failure") or "")))
    if statuses:
        return statuses[-1] in CONFIG_STATUSES or (statuses[-1] == 400 and (said or facts.get("code") == UNSUPPORTED))
    return said


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if key in NESTED and isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def adapter_digest(adapter: Path | None) -> str:
    """The adapter's files, as one digest: each one's relative path, and the bytes a deployment copies
    (hermit.hosts.inputs.copy_adapter) and whether it is executable (any x bit: harborside makes the deployment its
    agent user's, `u+rwX`, so the group's and others' bits, which the umask the adapter was written under sets, change
    nothing a session does); a link's are those of the file it leads to inside the adapter; any other link (one that
    leads outside it, to a directory or to no file: refused where a deployment copies it) counts as a link by its own
    text, so such an adapter is never its deployable twin without the link; none of git's or Python's caches, nor the
    adapter's card (.hermit/CARD.md: what hermit train and hermit eval --adapter final write there, which no
    deployment copies), so that a final adapter tested again, its card holding the first test's results, meets the
    trials of the first."""
    if adapter is None:
        return "none"
    h = hashlib.sha256()
    root = Path(adapter)
    inside = root.resolve()
    for path in sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink()):
        rel = path.relative_to(root)
        # as a deployment skips them: .git at the root, what a __pycache__ folder holds, a .pyc that is no folder
        if rel.parts[0] == ".git" or "__pycache__" in rel.parts[:-1] or (path.suffix == ".pyc" and not path.is_dir()):
            continue
        if rel.as_posix() == CARD:                          # the card: what the runs wrote of it, never deployed
            continue
        source = path.resolve() if path.is_file() else None   # a link: the file it leads to, as a deployment copies it
        if source is not None and source.is_relative_to(inside):
            executable = bool(stat.S_IMODE(source.stat().st_mode) & 0o111)
            h.update(rel.as_posix().encode() + b"\0" + (b"x" if executable else b"-") + b"\0"
                     + hashlib.sha256(source.read_bytes()).digest())
        else:                                               # a link a deployment refuses: by its own text
            h.update(rel.as_posix().encode() + b"\0link\0" + hashlib.sha256(os.fsencode(os.readlink(path))).digest())
    return h.hexdigest()


def _set_aside_audit(step_dir: Path) -> None:
    """The tool-call log an earlier run of a stream's step left in its directory (its stream started over: a new
    driver, an infra rerun, a resumed run), moved to earlier-<n>-tool-calls.jsonl at the step's turn, before its tools
    serve it: the log the server appends to holds this run's calls only, and so does its curated log."""
    audit = step_dir / curate_mod.TOOL_AUDIT
    if not os.path.lexists(audit):
        return
    n = 1
    while os.path.lexists(step_dir / f"earlier-{n}-{curate_mod.TOOL_AUDIT}"):
        n += 1
    audit.rename(step_dir / f"earlier-{n}-{curate_mod.TOOL_AUDIT}")


def _file_identity(path: Path | None):
    """A file's path, size and time of change (a binary rebuilt at the same path is another one), or None."""
    if path is None:
        return None
    try:
        st = Path(path).stat()
    except OSError:
        return [str(path)]
    return [str(path), st.st_size, int(st.st_mtime)]


class HarborAgent(Agent):
    """`agent.name: harbor`."""

    def __init__(self, params: dict | None = None, base: Path | str | None = None):
        super().__init__(params, base)
        given = dict(self.params)
        if given.get("benchmark"):
            shared = yaml.safe_load(self.path(given["benchmark"]).read_text()) or {}
            if not isinstance(shared, dict):
                raise ValueError(f"agent.params.benchmark {given['benchmark']} is not a mapping")
            shared.pop("dataset", None)                      # the task set's preparation (dataset.prepare), not a param
            self.params = _merge(shared, {k: v for k, v in given.items() if k != "benchmark"})
            self.benchmark_base = self.path(given["benchmark"]).parent
        else:
            self.benchmark_base = self.base
        if not self.params.get("tasks_dir"):
            raise ValueError("agent.params.tasks_dir is required (the Harbor task directories)")
        self.docker_cleanup                                  # an unknown mode fails here, before any container
        self.verifier_timeout                                # and an unknown verifier_timeout
        self.acting_agent                                    # and an agent spec without a name
        for key in TIMEOUT_MULTIPLIERS:                      # and a multiplier below 1
            self.timeout_multiplier(key)
        self.pass_reward                                     # and a pass reward or a redaction that cannot be read
        self.redactions
        harbor = self.harbor                                 # and a harbor that is not there: no rollout could start it
        if not (os.path.isabs(harbor) and os.path.isfile(harbor) and os.access(harbor, os.X_OK)):
            raise ValueError(f"agent.params.harbor {harbor} is not an executable file"
                             f"{'' if os.path.isabs(harbor) else ', nor a command on PATH'}: the `harbor` command of "
                             f"Harbor's own venv runs every trial (default {HARBOR})")
        python = self.harbor_python                          # and a python no stream's driver could start under
        if self.multi_step() and not (os.path.isfile(python) and os.access(python, os.X_OK)):
            raise ValueError(f"{python} is not an executable file: a multi-step task's driver runs under the python of "
                             f"Harbor's venv, beside agent.params.harbor ({harbor}) or beside the file a link of that "
                             "name points to; give the venv's own `harbor`, or a link to it, not a wrapper elsewhere")
        self.pulls = docker.Pulls()                          # the room trials hold for images still to be pulled
        sizes = json.loads(self._path(self.params["image_sizes"]).read_text()) if self.params.get("image_sizes") else {}
        self.image_sizes = {k: v for k, v in sizes.items() if not str(k).startswith("_")}
        self._streams: dict[str, object] = {}               # a stream's tool server, for the stream's life
        self._drivers: dict[str, StreamClient] = {}         # the drivers of the streams it stepped, until they finish
        self._in_flight: dict[str, list[int]] = {}          # a stream's steps whose rollouts are in _run_step now
        self._given_up: dict[str, str] = {}                 # a stream whose next turn cannot come: why
        self._streams_lock = threading.Lock()
        self._digests: dict[str, str] = {}                  # each task directory's contents, for the trial cache
        self._rollout = threading.local()                    # the halting verdict this thread's rollout met (halt)
        _LIVE.add(self)                                      # its streams closed at exit (_close_all)
        self.check_agent()

    # --- params ---------------------------------------------------------------------------------------------
    def _path(self, value) -> Path:
        p = Path(os.path.expandvars(str(value))).expanduser()
        return p if p.is_absolute() else (self.benchmark_base / p).resolve()

    @property
    def harbor(self) -> str:
        """The `harbor` command, resolved once, so that the startup check and every trial's Popen (in the job's
        directory) start the same one: a name without a slash found on PATH, a path made absolute (`~` expanded,
        relative to the working directory); a name not on PATH stays as given (the startup check refuses it)."""
        if getattr(self, "_harbor", None) is None:
            value = os.path.expanduser(str(self.params.get("harbor") or HARBOR))
            if "/" not in value:
                value = shutil.which(value) or value
            self._harbor = os.path.abspath(value) if "/" in value else value
        return self._harbor

    @property
    def harbor_python(self) -> str:
        """The interpreter of Harbor's venv (a stream's driver runs under it): the one beside `harbor`, or beside the
        file a link to it names (uv tool's and pipx's ~/.local/bin/harbor are links into the tool's venv)."""
        return str(Path(os.path.realpath(self.harbor)).parent / "python")

    @property
    def tasks_dir(self) -> Path:
        """The task set's directory, its links followed once (`utils/tasks`, the link `python -m
        hermit.harbor.dataset prepare` writes): a prepare that points the link elsewhere mid-run changes no trial's
        task set, nor the key its trial is kept under."""
        if getattr(self, "_tasks_dir", None) is None:
            self._tasks_dir = Path(os.path.realpath(self._path(self.params["tasks_dir"])))
        return self._tasks_dir

    def multi_step(self) -> bool:
        """Whether the task set holds a multi-step task (its steps run as a stream, whose driver runs under
        harbor_python); a task whose task.toml cannot be read counts as none here (its own rollout says why)."""
        for task_dir in dataset.task_dirs(self.tasks_dir):
            try:
                if dataset.step_names(dataset.task_config(task_dir)):
                    return True
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return False

    def unsupported(self, task: dict) -> list[str]:
        """Why Harbor runs the task for none of Hermit's own agents (a table CLI, the answer agent), or nothing: Harbor
        refuses a task that declares MCP servers (`[[environment.mcp_servers]]`) or a skills folder
        (`environment.skills_dir`), or that targets a Windows container (`environment.os`), for an agent whose
        capabilities lack them (harbor/trial/trial.py, _validate_agent_capabilities), as harborside's agents' do, and
        its refusal leaves no stream: such a rollout is infra (`unsupported_task`), never started (run). A task.toml
        that cannot be read says nothing here (its trial says why)."""
        env = self.task_config(task).get("environment") or {}
        if not isinstance(env, dict):
            return []
        return [w for w, on in (("MCP servers", env.get("mcp_servers")), ("a skills folder", env.get("skills_dir")),
                                ("a Windows container", str(env.get("os") or "").lower() == "windows")) if on]

    @property
    def jobs_dir(self) -> Path:
        return Path(os.path.expanduser(str(self.params.get("jobs_dir") or JOBS_DIR))).resolve()

    @property
    def tmpdir(self) -> Path:
        value = self.params.get("tmpdir")
        return Path(os.path.expanduser(str(value))).resolve() if value else self.jobs_dir / "tmp"

    @property
    def python(self) -> str:
        return str(self.params.get("python") or sys.executable)

    @property
    def keep_streams(self) -> bool:
        """Whether the streams this runner leaves unfinished are kept for a resume when it is closed (`keep_streams`,
        which hermit.isolate.agent_pipeline gives: a hermit train's are until it finishes, when Trainer._finish sets it
        false, any other pipeline's are not); a runner given none keeps them (without a run's identity, another runner
        may meet its streams)."""
        return bool(self.params.get("keep_streams", True))

    @property
    def acting_agent(self) -> dict:
        spec = dict(self.params.get("acting_agent") or {})
        clis.cli_of(spec)                                    # names one agent, and a known CLI
        if not spec.get("name") and not spec.get("import_path") and not spec.get("cli"):
            spec["cli"] = clis.DEFAULT_CLI
        return spec

    @property
    def cli(self) -> str | None:
        """The table's CLI the acting agent is (hermit.harbor.clis), or None for a Harbor built-in or another class."""
        return clis.cli_of(self.acting_agent)

    @property
    def deploys_adapter(self) -> bool:
        return self.cli is not None

    @property
    def host(self) -> str | None:
        """The host the adapter is deployed for: the acting agent's CLI."""
        return self.cli

    @property
    def login(self) -> str | None:
        return str(self.params["login"]) if self.params.get("login") else None

    @property
    def login_root(self) -> Path:
        value = self.params.get("login_root")
        return Path(os.path.expanduser(str(value))).resolve() if value else subscription.ROOT

    @property
    def binding(self):
        """The model's binding (hermit.harbor.launch.binding_of), or None for a plain model name or a model a CLI
        reaches; priced at `price` when the params give one (an explicit price, which bind_harbor_params keeps beside
        the backend's in the binding's spec), so that every HermitXXX trial's usage (one of Harbor's own agents keeps
        Harbor's cost) and the answer agent's endpoint follow it."""
        if not hasattr(self, "_binding"):
            self._binding = None if self.cli_model is not None else launch.binding_of(self.params)
            if self._binding is not None and isinstance(self.params.get("price"), dict):
                self._binding.price = dict(self.params["price"])
        return self._binding

    @property
    def cli_model(self) -> dict | None:
        """`model: {cli: codex | claude | pi, model, login, ...}`: a model only a CLI reaches (the answer agent's, each
        request one session of that CLI on this machine: hermit.agent.base.endpoint.cli_endpoint), or None."""
        model = self.params.get("model")
        return dict(model) if isinstance(model, dict) and model.get("cli") else None

    def answer_model(self) -> dict:
        """The answer agent's model as its endpoint serves it: a model a CLI reaches, or the binding's spec with the
        client settings the model's spec gives beside it (`timeout`, `max_retries`, `count_error_usage`: the spec of a
        model a CLI reaches through an endpoint of this pipeline, hermit.agent.base.endpoint.endpoint_spec, gives a
        timeout that waits out a session and a login's plan, which the relay's client would otherwise cut at its own
        600 s and send again, and counts what a failed session used, which its error reply says)."""
        if self.cli_model is not None:
            return self.cli_model
        if self.binding is None:
            raise ValueError("the answer agent needs agent.params.model: a <backend>/<model> reference, or a model a "
                             "CLI reaches (model: {cli: codex | claude | pi, model: ..., login: ...})")
        spec, bound, model = self.binding.spec(), self.params.get("binding"), self.params.get("model")
        # the spec the binding was made from (launch.binding_of): the bound one, else the model's own mapping
        given = bound if isinstance(bound, dict) and bound.get("base_url") and bound.get("model") else model
        if isinstance(given, dict):
            spec.update({k: given[k] for k in ("timeout", "max_retries", "count_error_usage")
                         if given.get(k) is not None})
        return spec

    def model_plan(self) -> tuple[Path, bool] | None:
        """(the plan, whether its sessions may draw usage credits) of the login the answer agent's model runs on: a
        model a CLI reaches on a login (`model: {cli, login, plan_dir, usage_credits}`, or the `model_login`,
        `model_plan_dir` and `model_usage_credits` hermit.isolate.agent_pipeline gives when it serves that model
        itself), each request one session that waits inside the request while the plan holds sessions back
        (CliAgent.run); None for any other agent or model."""
        if self.cli != "answer":
            return None
        given = self.cli_model or {k: self.params.get(f"model_{k}") for k in ("login", "plan_dir", "usage_credits")}
        if not given.get("login"):
            return None
        return (subscription.plan_dir(given.get("plan_dir"), str(given["login"])),
                subscription.opted_in(given.get("usage_credits")))

    def bin(self, cli: str | None = None) -> Path | None:
        """`<cli>_bin`: a binary of this machine mounted for the acting CLI, or None."""
        cli = cli or self.cli
        value = self.params.get(f"{cli}_bin") if cli in BINS else None
        return Path(os.path.expanduser(str(value))).resolve() if value else None

    def bin_mount(self, cli: str | None = None) -> tuple[Path, str]:
        """(the folder mounted for `<cli>_bin`, the binary's path in the container): Codex's standalone release's
        folder (codex-package.json; its rg and code-mode host beside the binary), else the binary's own folder."""
        cli = cli or self.cli
        binary = self.bin(cli)
        root = binary.parent
        if cli == "codex" and (binary.parent.parent / "codex-package.json").is_file():
            root = binary.parent.parent
        return root, f"{clis.bin_mount(cli)}/{binary.relative_to(root).as_posix()}"

    def version(self, cli: str | None = None) -> str | None:
        """`<cli>_version`, else the table's default: what an install in the container installs."""
        cli = cli or self.cli
        value = self.params.get(f"{cli.replace('-', '_')}_version") if cli else None
        return str(value) if value else (clis.entry(cli)["version"] if cli else None)

    @property
    def agent_timeout_sec(self) -> int | None:
        value = self.params.get("agent_timeout_sec")
        return int(value) if value else None

    @property
    def setup_timeout_sec(self) -> float | None:
        value = self.params.get("setup_timeout_sec")
        return float(value) if value else None

    @property
    def docker_cleanup(self) -> str:
        mode = str(self.params.get("docker_cleanup") or "images_keep")
        if mode not in docker.CLEANUP_MODES:
            raise ValueError(f"agent.params.docker_cleanup must be one of {', '.join(docker.CLEANUP_MODES)}, not "
                             f"{mode!r}")
        return mode

    @property
    def verifier_timeout(self) -> str:
        """What a verifier that ran out of its time is: `agent` (the default) or `infra` (verifier_timeout_verdict)."""
        mode = str(self.params.get("verifier_timeout") or "agent")
        if mode not in ("agent", "infra"):
            raise ValueError(f"agent.params.verifier_timeout is agent or infra (a verifier that ran out of its time: "
                             f"the agent's result, or run again), not {mode!r}")
        return mode

    @property
    def reward_key(self) -> str:
        return str(self.params.get("reward_key") or "reward")

    @property
    def pass_reward(self) -> float | None:
        value = self.params.get("pass_reward", 1.0)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError(f"agent.params.pass_reward is a number or null (no reward is a pass), not {value!r}") \
                from None

    @property
    def redactions(self) -> list[str]:
        patterns = self.params.get("verifier_redact") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        for pattern in patterns:
            try:
                re.compile(str(pattern))
            except re.error as exc:
                raise ValueError(f"agent.params.verifier_redact: {pattern!r} is no regular expression "
                                 f"({exc})") from None
        return [str(p) for p in patterns]

    def model_fields(self) -> dict:
        """A Harbor built-in's model fields from `model`: a name, or a bound backend's {model, base_url,
        api_key_env}. Harbor's claude-code is given the backend's Messages API root (Claude Code appends /v1/messages
        to it, as Hermit's own sessions are given it: Binding.claude_env), and a model at Anthropic's own API by name
        alone (`anthropic/<model>`), as Harbor signs in to it for a model given by name: its key, ANTHROPIC_API_KEY."""
        model = self.params.get("model")
        if not isinstance(model, dict):
            return {"model_name": str(model)} if model else {}
        base = model.get("base_url")
        if base and self.acting_agent.get("name") == "claude-code":
            base = str(model.get("messages_url") or re.sub(r"/v1/?$", "", str(base))).rstrip("/")
            from urllib.parse import urlsplit
            if urlsplit(base).hostname == "api.anthropic.com":       # with its key's variable, which Harbor reads
                named = {"model_name": f"anthropic/{model.get('model')}",
                         "model_api_key_name": model.get("api_key_env")}
                return {k: v for k, v in named.items() if v}
        out = {"model_name": model.get("model"), "base_url": base, "model_api_key_name": model.get("api_key_env")}
        return {k: v for k, v in out.items() if v}

    def task_dir(self, task: dict) -> Path:
        return self.tasks_dir / str(task.get("harbor_task") or task.get("task") or task["id"])

    def task_config(self, task: dict) -> dict:
        try:
            return dataset.task_config(self.task_dir(task))
        except (OSError, ValueError):
            return {}

    def restricted(self, task: dict) -> bool:
        """Whether the task's (or the step's) agent phase has no public network (task.toml): its model's and tools'
        hosts are named to Harbor, and the CLI's web search is off (Harbor's own rule)."""
        return dataset.network_modes(self.task_config(task), task.get("step"))["agent"] != "public"

    def wait_budget(self, task: dict, *, build: bool = True) -> float:
        """How long a trial may take: setup (Harbor's limit, `setup_timeout_sec` when longer), the agent's budget, the
        verifier's (verifier.override_timeout_sec, else the task's, times verifier_timeout_multiplier), the image
        build (a Dockerfile task; with environment_build_timeout_multiplier, its scaled limit, and for a pulled image
        the start's added room), teardown; or the explicit `wait_budget_s`."""
        if self.params.get("wait_budget_s"):
            return float(self.params["wait_budget_s"])
        agent = self.agent_timeout_sec or int(task.get("agent_timeout_sec") or DEFAULT_AGENT_TIMEOUT_S)
        verifier = int(self.verifier_config().get("override_timeout_sec") or task.get("verifier_timeout_sec") or agent)
        verifier = int(verifier * self.timeout_multiplier("verifier_timeout_multiplier"))
        start = self.timeout_multiplier("environment_build_timeout_multiplier")
        build_s = int(task.get("build_timeout_sec") or 0)
        built = int(build_s * (start if build and not task.get("docker_image") else start - 1))
        setup = max(int(self.params.get("setup_budget_s") or SETUP_BUDGET_S), int(self.setup_timeout_sec or 0))
        return agent + setup + verifier + built + int(self.params.get("teardown_budget_s") or TEARDOWN_BUDGET_S)

    def timeout_multiplier(self, key: str) -> float:
        """Harbor's job-level multiplier `key` (TIMEOUT_MULTIPLIERS), 1 when unset; never below 1."""
        value = float(self.params.get(key) or 1.0)
        if value < 1.0:
            raise ValueError(f"{key} must be at least 1, not {value}")
        return value

    # --- the acting CLI: its settings, checked at startup ---------------------------------------------------------
    def settings(self, task: dict | None = None) -> dict:
        """The CLI Base Agent's settings for a trial (hermit.harbor.launch.settings_for); web search off for a task
        whose agent phase has no public network."""
        web = bool(self.params.get("web_search"))
        if task is not None and self.restricted(task):
            web = False
        extra = dict(self.acting_agent.get("settings") or {})
        return launch.settings_for(self.cli, model=self.params.get("model"), binding=self.binding,
                                   effort=self.params.get("effort"), web_search=web, login=self.login, extra=extra)

    def check_agent(self) -> None:
        """What every trial of the acting agent would refuse, refused before the first one: for a Harbor built-in its
        kwargs from the run's settings (no login, an effort it takes: builtin_kwargs) and, for its claude-code, a
        sign-in it is handed (check_builtin_sign_in); for a table CLI its sign-in (a binding with its key set, or its
        CLI's own login), web search set, a model this machine's containers can reach, the settings its Base Agent
        renders (a dry render of the launch plan), and for mini-swe-agent the package its adapter's deployment reads
        on this machine (unless the pipeline deploys no adapter: hermit eval --adapter none, whose pipeline
        hermit.isolate.agent_pipeline tells so, `deploys_adapters: false`), and, for a pipeline that deploys
        adapters, bubblewrap when the adapter sandbox that renders each (`sandbox_enforce`) is required; for the
        answer agent its model's key, and
        no attachments on a model a CLI reaches (`model: {cli: ...}`, or the CLI `model_cli` names when
        hermit.isolate.agent_pipeline serves that model itself), whose sessions take text only. A task Harbor runs
        for none of Hermit's agents is not refused here, as the run may never take it: its rollout is infra
        (unsupported). For every agent, no secret in acting_agent.env (check_env)."""
        self.check_env()
        if self.cli is None:
            self.builtin_kwargs()
            self.check_builtin_sign_in()
            return
        cli, login, binding = self.cli, self.login, self.binding
        model = self.params.get("model")
        if not isinstance(self.acting_agent.get("settings") or {}, dict):
            raise ValueError("agent.params.acting_agent.settings is a mapping of the CLI Base Agent's settings")
        if self.params.get("deploys_adapters", True) and not shutil.which("bwrap"):
            from hermit.adapter.session import ENV_ENFORCE
            if (self.params.get("sandbox_enforce") or os.environ.get(ENV_ENFORCE, "required")) == "required":
                # staging renders every adapter (its exports, its per-task context) in it: each trial would fail
                raise ValueError("a trial deploys its adapter by rendering it in the strict adapter sandbox, whose "
                                 "enforce is required (agent.params.sandbox_enforce), and bubblewrap (bwrap) is not "
                                 "installed: install it, or set agent.params.sandbox_enforce: best_effort (unconfined, "
                                 "recorded); hermit eval --adapter none deploys no adapter")
        if cli == "answer":
            if login:
                raise ValueError("the answer agent takes no login: its model is a <backend>/<model> reference, or "
                                 "one a CLI reaches (model: {cli: ..., login: ...})")
            spec = self.answer_model()
            reached = (self.cli_model or {}).get("cli") or self.params.get("model_cli")
            if reached and (self.params.get("answer") or {}).get("attachments"):
                raise ValueError(f"agent.params.answer.attachments sends the task's images with the question, and the "
                                 f"answer agent's model is one a CLI reaches (model: {{cli: {reached}}}), whose "
                                 "session takes text only: give a <backend>/<model> reference whose model takes "
                                 "images, or no attachments")
            if self.cli_model is not None:
                from hermit.agent.base.endpoint import cli_problems
                problems = cli_problems(spec)
                enforce = str(self.params.get("sandbox_enforce") or "required")
                if (spec.get("isolation") or "sandbox") == "sandbox" and enforce == "required" and \
                        not shutil.which("bwrap"):
                    problems.append(f"model: cli: {spec['cli']} runs each session in a bubblewrap sandbox of its own "
                                    "(isolation: sandbox), and bubblewrap (bwrap) is not installed")
                if problems:
                    raise ValueError("; ".join(problems))
            elif binding.key_env_if_set() and not os.environ.get(binding.key_env_if_set()):
                raise ValueError(f"{binding.key_env_if_set()} is not set: the answer agent's model endpoint on this "
                                 "machine sends each request on with the key it reads from this process's environment")
            self._dry_render(None, tools=False)
            return
        if self.cli_model is not None or self.params.get("model_cli"):    # model_cli: the pipeline serves that model
            raise ValueError(f"model: {{cli: ...}} is a model the answer agent reaches through a CLI; a {cli} trial's "
                             "model is its own: a <backend>/<model> reference, or the CLI's model name with its login")
        if login is not None and login not in subscription.LOGINS:
            raise ValueError(f"agent.params.login is {' or '.join(sorted(subscription.LOGINS))} or absent, not "
                             f"{login!r}")
        own = clis.entry(cli)["backend"]
        if login and subscription.LOGINS[login] != own:
            raise ValueError(f"login: {login} is {'Codex' if login == 'chatgpt' else 'Claude Code'}'s sign-in; the "
                             f"acting CLI {cli} " + ("takes no login: its model is a <backend>/<model> reference"
                                                     if own in subscription.NO_LOGIN else
                                                     f"takes login: {'chatgpt' if own == 'codex' else 'claude'}"))
        if login and binding is not None:
            raise ValueError(f"login: {login} and a <backend>/<model> reference are two bills: give one")
        if not login and binding is None:
            raise ValueError(f"agent.params.model {model!r} is no <backend>/<model> reference: a {cli} trial needs one"
                             + (f", or login: {'chatgpt' if own == 'codex' else 'claude'} with the CLI's model name"
                                if own in ("codex", "claude") else " (its only sign-in)"))
        if login and (not isinstance(model, str) or not model):
            raise ValueError(f"with login: {login}, agent.params.model is the CLI's model name")
        if not isinstance(self.params.get("web_search"), bool):
            raise ValueError("agent.params.web_search must be set, true or false: the CLI's web search (Codex's runs "
                             "at the provider, past any sandbox or proxy) is never left to a default")
        if binding is not None:
            from urllib.parse import urlsplit
            url = binding.base_url if own != "claude" else binding.backend.messages_root
            if (urlsplit(url).hostname or "") in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
                raise ValueError(f"the model's endpoint {url} is on this machine's loopback, which a task container "
                                 f"cannot reach: serve it on an address the container reaches (<NAME>_BASE_URL)")
        if login:
            # a mounted claude_bin is this machine's Claude Code, checked here as that CLI (problems_of runs it for its
            # version); without one a Claude session runs the container's install (contained: only the token is checked)
            mounted = self.bin("claude") if login == "claude" else None
            problems = subscription.problems_of(login, program=str(mounted) if mounted else None,
                                                contained=mounted is None, root=self.login_root)
            if problems:
                raise ValueError("; ".join(problems))
        if own == "dsh":
            from hermit.agent.base.cli import dsh_problems
            problems = dsh_problems(self.settings(), "agent")
            if problems:
                raise ValueError("; ".join(problems))
        binary = self.bin(cli)
        if binary is not None and not (binary.is_file() and os.access(binary, os.X_OK)):
            raise ValueError(f"agent.params.{cli}_bin {binary} is not an executable file")
        if cli == "mini-swe-agent" and self.params.get("deploys_adapters", True) and not self._finds("minisweagent"):
            from hermit.agent.base.mini_swe_agent import INSTALL
            raise ValueError(f"{INSTALL}, for {self.python} (agent.params.python, else this interpreter): it deploys "
                             "the adapter of each mini-swe-agent trial (hermit.hosts.mini_swe_agent), which reads the "
                             "package's own config")
        self._dry_render(None, tools=False, check=True)

    def _finds(self, package: str) -> bool:
        """Whether the interpreter the adapter's deployment runs with (`python`, staging.stage) finds `package` as a
        package, as hermit.agent.base.mini_swe_agent.render_config looks for it: in this process when that interpreter
        is this one, else in a short child of it."""
        if self.python == sys.executable:
            from importlib.util import find_spec
            spec = find_spec(package)
            return spec is not None and bool(spec.submodule_search_locations)
        from hermit.sandbox import framework_env
        probe = (f"import importlib.util, sys; s = importlib.util.find_spec({package!r}); "
                 "sys.exit(0 if s is not None and s.submodule_search_locations else 1)")
        try:
            return subprocess.run([self.python, "-c", probe], env=framework_env(), capture_output=True,
                                  timeout=60).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _dry_render(self, task: dict | None, *, tools: bool, check: bool = False) -> str:
        """The digest of the launch plan a trial of `task` renders, without its deployment, sign-in or tools' server
        (the cache key's; at startup, the settings checked), rendered in a scratch job that is removed after. With
        `check`, what harborside would refuse in every trial is refused here (check_secrets)."""
        scratch = self.jobs_dir / ".renders" / uuid.uuid4().hex[:12]
        scratch.mkdir(parents=True)
        try:
            if self.cli == "answer":
                plan = launch.render_answer(self.settings(task), endpoint=ANSWER_PLACEHOLDER, job=scratch,
                                            options=self.params.get("answer") or {})
            else:
                plan = launch.render(self.cli, self.settings(task), deploy=None, sign_in=False, job=scratch,
                                     tools_url=TOOLS_PLACEHOLDER if tools else None, login=self.login,
                                     tools_name="adapter" if tools else None, **self._mounted())
            if check:
                self.check_secrets(json.loads(plan.read_text(encoding="utf-8")))
            return launch.digest(plan)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def check_secrets(self, plan: dict) -> None:
        """A plan's secrets reach the session by name (harborside: `docker compose exec -e NAME`, or a descriptor):
        each must be set here, where harbor starts, and the task's environment must be Harbor's local Docker one."""
        names = sorted({*(plan.get("env_from_host") or {}).values(), *(plan.get("env_by_fd") or {}).keys()})
        if not names:
            return
        missing = [n for n in names if not os.environ.get(n)]
        if missing:
            raise ValueError(f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set: the acting "
                             f"agent's session reads {'it' if len(missing) == 1 else 'them'} from this process's "
                             "environment, by name")
        kind = (self.params.get("environment") or {}).get("type")
        if kind not in (None, "docker"):
            raise ValueError(f"agent.params.environment.type {kind!r}: the acting agent's secrets ({', '.join(names)}) "
                             "reach its session by name, which only Harbor's local docker environment does")

    def _mounted(self) -> dict:
        """The launch's program and PATH for a mounted `<cli>_bin`."""
        if self.bin() is None:
            return {}
        program = self.bin_mount()[1]
        return {"bin_path": program, "path_prepend": [str(Path(program).parent)]}

    # --- the job --------------------------------------------------------------------------------------------
    def agent_config(self, task: dict, plan: Path | None = None, deployed: dict | None = None, *,
                     tools_host: str | None = None) -> dict:
        """Harbor's agent config for this rollout: a table CLI's class with its launch plan (and its own options: the
        mounted binary, the install's version), or a Harbor built-in with its model fields, as Harbor runs it."""
        spec = self.acting_agent
        cli = self.cli
        kwargs = dict(spec.get("kwargs") or {})
        env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
        if cli is not None:
            agent: dict = {"import_path": clis.entry(cli)["import_path"]}
            if plan is not None:
                kwargs["plan"] = str(plan)
            if cli != "answer":
                if self.version(cli):
                    kwargs.setdefault("version", self.version(cli))
                if self.bin(cli) is not None:
                    kwargs.setdefault(f"{cli}_bin", self.bin_mount(cli)[1])
                hosts = self.allowed_hosts(task, tools_host)
                if hosts:
                    agent["extra_allowed_hosts"] = hosts
        else:
            if deployed is not None:
                raise ValueError(f"the acting agent {spec.get('import_path') or spec.get('name')} takes no adapter; "
                                 f"use acting_agent.cli ({', '.join(k for k in clis.HARBOR_CLIS)}), or run with "
                                 "--adapter none")
            agent = {("import_path" if spec.get("import_path") else "name"): spec.get("import_path") or spec["name"]}
            agent.update(self.model_fields())
            if agent.get("base_url"):
                agent["skip_model_parsing"] = True          # a bound endpoint serves the id as given (accounts/...)
            kwargs = {**self.builtin_kwargs(), **kwargs}
        if self.agent_timeout_sec:
            agent["override_timeout_sec"] = self.agent_timeout_sec
        if self.setup_timeout_sec:
            agent["override_setup_timeout_sec"] = self.setup_timeout_sec
        agent["kwargs"] = kwargs
        if env:
            agent["env"] = env
        return agent

    def check_env(self) -> None:
        """`acting_agent.env` holds no secret: its values are written into the trial's job (job.yaml, agent.json) and
        handed to each `docker compose exec` on its command line, which every user of this machine can read. A
        variable of a secret's name (launch.SECRET_VAR: OPENAI_API_KEY, GH_TOKEN) or a value naming one is refused:
        `${NAME}` and `${NAME:-default}`, which Harbor fills in from this process's environment, and `$NAME`, which it
        hands on as that text (never the secret the config meant). A table CLI's secret is passed by name (the CLI's
        settings.env, NAME: $NAME), and Harbor's own agents read theirs from this process's environment."""
        for name, value in (self.acting_agent.get("env") or {}).items():
            named = [a or b for a, b in ENV_REF.findall(str(value))]
            secret = str(name) if launch.SECRET_VAR.search(str(name)) else \
                next((n for n in named if launch.SECRET_VAR.search(n)), None)
            if secret:
                raise ValueError(f"agent.params.acting_agent.env {name} holds a secret ({secret}): its values are "
                                 "written into each trial's job and onto `docker compose exec`'s command line (Harbor "
                                 "fills ${NAME} and ${NAME:-default} in, and hands $NAME on as that text); pass a "
                                 "table CLI's by name in its settings.env (NAME: $NAME), and export one of Harbor's "
                                 "own agents in this process's environment")

    def builtin_kwargs(self) -> dict:
        """A Harbor built-in's kwargs from the run's settings: Codex's and Claude Code's effort and version, Claude
        Code's web search off on every task with `web_search: false` (true, or unset: Harbor's own rule, off where the
        task's agent network is not public, which any `disable_web_search` Harbor is given would override) and the
        tools that outlive the session denied. Its sign-in is Harbor's own, never a login: Harbor's Claude Code would
        put the token on a command line."""
        name = self.acting_agent.get("name")
        out: dict = {}
        if name not in ("codex", "claude-code"):
            if self.login:
                raise ValueError(f"login: {self.login} runs with acting_agent.cli, whose launch passes the sign-in "
                                 f"safely; {name or self.acting_agent.get('import_path')} would put it on a command "
                                 "line")
            return out
        if self.login:
            raise ValueError(f"login: {self.login} runs with acting_agent.cli: "
                             f"{'codex' if name == 'codex' else 'claude'}, which passes the sign-in safely; Harbor's "
                             f"own {name} would put it on a command line")
        if self.params.get("effort"):
            if name == "claude-code":
                from hermit.agent.base import cli as cli_mod
                if str(self.params["effort"]) not in cli_mod.Claude.efforts:
                    raise ValueError(f"Claude Code takes effort {', '.join(cli_mod.Claude.efforts)}, not "
                                     f"{self.params['effort']!r}")
            out["reasoning_effort"] = str(self.params["effort"])
        version = self.params.get("codex_version" if name == "codex" else "claude_version")
        if version:
            out["version"] = str(version)
        if name == "claude-code":
            from hermit.agent.base import cli as cli_mod
            web = self.params.get("web_search")
            if web is not None and not isinstance(web, bool):
                raise ValueError(f"agent.params.web_search is true or false, not {web!r}")
            if web is False:
                out["disable_web_search"] = True
            out["disallowed_tools"] = ",".join(cli_mod.CLAUDE_BEYOND_SESSION)
        return out

    def check_builtin_sign_in(self) -> None:
        """Harbor's own claude-code bills the key this process gives it (harbor_env), never the subscription, whose
        token it would put on a command line: refused at startup, CLAUDE_FORCE_OAUTH (Harbor's switch to the
        subscription: each trial would bill the key instead, unsaid), and the subscription's token as the only
        sign-in of a model at Anthropic's API (each trial would run signed out and halt the run). CLAUDE_FORCE_OAUTH,
        and a bill beside the token (Claude Code's billing variables, the model's own key variable), count in this
        process's environment and in `acting_agent.env`, which Harbor's agent reads too."""
        if self.acting_agent.get("name") != "claude-code":
            return
        from hermit.agent.base import cli as cli_mod
        route = ("run acting_agent.cli: claude with login: claude, which hands Claude Code the subscription's token "
                 "safely")
        given = {**os.environ, **{str(k): str(v) for k, v in (self.acting_agent.get("env") or {}).items()}}
        if (given.get("CLAUDE_FORCE_OAUTH") or "").strip().lower() not in ("", "false", "0", "no"):
            raise ValueError("CLAUDE_FORCE_OAUTH asks Harbor's own claude-code for the subscription's sign-in, whose "
                             "token Hermit never hands it (it would put it on a command line), so each trial would "
                             f"bill ANTHROPIC_API_KEY instead: {route}, or unset CLAUDE_FORCE_OAUTH to bill the key")
        model = self.model_fields()
        at_anthropic = not model.get("base_url") and "/" not in str(model.get("model_name") or "").removeprefix(
            "anthropic/")
        bills = (*cli_mod.CLAUDE_BILLING, *([model["model_api_key_name"]] if model.get("model_api_key_name") else []))
        if (os.environ.get(subscription.TOKEN_ENV) or "").strip() and at_anthropic and \
                not any(given.get(n) for n in bills):
            raise ValueError(f"{subscription.TOKEN_ENV} is the only sign-in here, and Harbor's own claude-code is "
                             "never handed it (it would put it on a command line), so each trial would run signed "
                             f"out: {route}, or export the key it is to bill (ANTHROPIC_API_KEY)")

    def allowed_hosts(self, task: dict, tools_host: str | None = None) -> list[str]:
        """The hosts a task whose agent phase has no public network must let the CLI reach (Harbor's
        extra_allowed_hosts, which then replaces Harbor's own pick of the model's host): the model's (the binding's
        endpoint, a login's hosts) and the address of the adapter's tools. None for a public one."""
        if not self.restricted(task) or self.cli in (None, "answer"):
            return []
        from urllib.parse import urlsplit
        hosts = []
        if self.binding is not None:
            for url in (self.binding.base_url, self.binding.backend.messages_root):
                host = urlsplit(url).hostname
                if host and host not in hosts:
                    hosts.append(host)
        hosts += [h for h in subscription.hosts(self.login) if h not in hosts]
        if tools_host and tools_host not in hosts:
            hosts.append(tools_host)
        return hosts

    def environment_config(self) -> dict:
        env = {"delete": True, **(self.params.get("environment") or {})}
        if env.get("extra_docker_compose"):                  # Harbor's compose overlays, found where the benchmark
            env["extra_docker_compose"] = [str(self._path(p)) for p in env["extra_docker_compose"]]   # file is
        cli = self.cli if self.cli in BINS else None
        if cli is not None and self.bin(cli) is not None:
            if not os.access(self.bin(cli), os.X_OK):
                raise ValueError(f"agent.params.{cli}_bin {self.bin(cli)} is not an executable file")
            mounts = list(env.get("mounts") or [])
            mounts.append({"type": "bind", "source": str(self.bin_mount(cli)[0]), "target": clis.bin_mount(cli),
                           "read_only": True})
            env["mounts"] = mounts
        return {k: v for k, v in env.items() if v is not None}

    def verifier_config(self) -> dict:
        return {k: v for k, v in (self.params.get("verifier") or {}).items() if v is not None}

    def job_config(self, task: dict, job_name: str, agent: dict) -> dict:
        """Harbor's job YAML for this one trial (a config file: `--agent` on the CLI discards the agents block)."""
        config = {"job_name": job_name, "jobs_dir": str(self.jobs_dir), "n_attempts": 1, "n_concurrent_trials": 1,
                  "tasks": [{"path": str(self.task_dir(task))}], "agents": [agent],
                  "environment": self.environment_config()}
        if self.verifier_config():
            config["verifier"] = self.verifier_config()
        for key in TIMEOUT_MULTIPLIERS:
            if self.params.get(key) is not None:
                config[key] = self.timeout_multiplier(key)
        if self.params.get("retry"):
            config["retry"] = dict(self.params["retry"])
        return config

    def harbor_env(self, keep=()) -> dict:
        """The environment of Harbor's processes: the harborside package (and the example's agents) importable,
        a TMPDIR outside /tmp, the variables a plan passes by name (`keep`) among this process's. For HermitClaude
        nothing of another bill or of a parent Claude Code session; for Harbor's own Claude Code, which signs in with
        this process's key (ANTHROPIC_API_KEY, ...), nothing of a parent Claude Code session or another model's
        settings; the subscription's token only on its login; and never the switches that have Harbor's own codex sign
        in with the user's ChatGPT file (CODEX_SIGN_IN), so that a ChatGPT plan is used only on `login: chatgpt`, in
        the plan's slots and under its MAX_USED."""
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        paths = [str(HARBORSIDE_ROOT), *(str(self._path(p)) for p in (self.acting_agent.get("pythonpath") or []))]
        if os.environ.get("PYTHONPATH"):
            paths.append(os.environ["PYTHONPATH"])
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths), "TMPDIR": str(self.tmpdir)}
        for name in CODEX_SIGN_IN:
            if name not in keep:
                env.pop(name, None)
        if self.cli == "claude" or self.acting_agent.get("name") == "claude-code":
            from hermit.agent.base import cli
            drop = (*cli.CLAUDE_BILLING, *cli.CLAUDE_INHERITED, "CLAUDE_FORCE_OAUTH")
            if self.cli != "claude":                         # Harbor's own: its bill is the one it is given here
                drop = tuple(n for n in drop if n not in cli.CLAUDE_BILLING)
            for name in drop:
                if name not in keep:
                    env.pop(name, None)
        if self.login != "claude":                           # the subscription's token: only a Claude login's trial
            env.pop(subscription.TOKEN_ENV, None)
        elif not env.get(subscription.TOKEN_ENV):
            raise ValueError(subscription.NO_TOKEN)
        return env

    def harbor_run(self, job: Path, config: Path, task: dict, keep=()) -> tuple[int | None, bool]:
        """`harbor run` on the job config in its own process group; (exit code, expired). On expiry the group is
        killed and the trial's compose projects taken down, best effort: Harbor would otherwise leave the containers
        running after its client died."""
        with open(job / "harbor.log", "w") as log:
            proc = subprocess.Popen([self.harbor, "run", "-c", str(config), "-y", "-q"], env=self.harbor_env(keep),
                                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True, cwd=str(job))
            try:
                return proc.wait(timeout=self.wait_budget(task)), False
            except subprocess.TimeoutExpired:
                pass
            for sig, grace in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
                try:
                    os.killpg(os.getpgid(proc.pid), sig)
                except ProcessLookupError:
                    break
                try:
                    proc.wait(timeout=grace)
                    break
                except subprocess.TimeoutExpired:
                    continue
        for trial in self.trial_dirs(job, task):
            docker.compose_down(trial.name)
        return proc.returncode, True

    @staticmethod
    def trial_dirs(job: Path, task: dict) -> list[Path]:
        """Harbor's trial directories of the job: `<task>__<7-char id>` (one, for one trial), with the task's name
        cut as Harbor cuts it (harbor/models/trial/config.py generate_trial_name: the last path part, its first 32
        characters, trailing '_' and '-' removed): a longer name is never found under its full spelling."""
        name = str(task.get("harbor_task") or task.get("task") or task["id"]).split("/")[-1][:32].rstrip("_-")
        return sorted(p for p in job.glob(f"{glob.escape(name)}__*") if p.is_dir())

    # --- the halt sentinel and the disk gate ----------------------------------------------------------------
    def halt_file(self) -> Path:
        return self.jobs_dir / HALT_FILE

    def halted(self) -> str | None:
        try:
            return self.halt_file().read_text(encoding="utf-8")
        except OSError:
            return None

    def halt(self, verdict: dict, job: Path) -> None:
        """Record a halting verdict (a lost sign-in, an account's limit): every later rollout returns at once, and
        the rollout's own output reports it whatever follows (run: an OSError of this machine after it)."""
        self._rollout.halted = verdict
        # the record's cause first (login_failed, usage_limit, ...), then the exception Harbor kept for the trial
        cause = verdict["failure_cause"] or verdict["exception_type"]
        named = f"{cause} ({verdict['exception_type']})" if verdict["exception_type"] not in (None, cause) else cause
        try:
            self.jobs_dir.mkdir(parents=True, exist_ok=True)
            self.halt_file().write_text(f"{named}: {verdict['exception_message'][:2000]}\n"
                                        f"job: {job}\nwritten: {datetime.now().isoformat(timespec='seconds')}\n"
                                        f"Every run on this jobs directory is halted while this file exists. Remove it "
                                        f"once the login or the usage window is restored.\n", encoding="utf-8")
        except OSError as exc:              # a full disk: the rollout's own output still reports the halt
            print(f"WARNING: {self.halt_file()} could not be written ({exc}): this run ends on the halt its rollout "
                  "reports, but a later run on this jobs directory is not halted", file=sys.stderr, flush=True)
        print(f"WARNING: {named} in {job.name}: nothing later in the run can succeed; every rollout on {self.jobs_dir} "
              f"is halted while {self.halt_file()} exists", file=sys.stderr, flush=True)

    def wait_for_disk(self, task: dict | None = None, admitted: list | None = None) -> bool:
        """The disk gate; for a task whose prebuilt image is not here yet and whose size `image_sizes` knows (a JSON
        file: compressed bytes per image reference), room for the image too, with the room other trials of this
        process are still to pull counted as taken (`admitted` gets the key of the room this trial holds)."""
        gate = self.params.get("disk_gate_gb")
        gate = float(DISK_GATE_GB if gate is None else gate)
        tmp_gate = self.params.get("tmp_gate_gb")
        tmp_gate = float(TMP_GATE_GB if tmp_gate is None else tmp_gate)
        wait_s = float(self.params.get("disk_gate_wait_s") or DISK_GATE_WAIT_S)
        if not docker.wait_for_disk(HOST_TMP, tmp_gate, wait_s):          # the host's /tmp, before any room is held
            return False
        image = docker.task_image(self.task_dir(task)) if task is not None else None
        return docker.wait_for_disk(self._docker_root(), gate, wait_s,
                                    image=image, image_gb=self.image_gb(image) if image else 0.0, pulls=self.pulls,
                                    admitted=admitted)

    def image_gb(self, image: str) -> float:
        """An image's estimated size on disk (GB), from `image_sizes`; 0 when unknown."""
        compressed = self.image_sizes.get(image)
        return float(compressed) * docker.ON_DISK / docker.GB if compressed else 0.0

    def _docker_root(self) -> Path:
        if getattr(self, "_root", None) is None:
            self._root = docker.data_root()
        return self._root

    def cleanup_images(self, task: dict) -> None:
        docker.cleanup_images(self.docker_cleanup, self.task_dir(task), self.jobs_dir / ".locks",
                              threshold_gb=float(self.params.get("rmi_threshold_gb") or 1.0))

    # --- the rollout ------------------------------------------------------------------------------------------
    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        started = time.time()
        self._rollout.halted = None
        try:
            self.jobs_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:                               # this machine's, before anything: no session ran
            return self._output(task, no_trial("host_failed", f"{type(exc).__name__}: {exc}"[-2000:]), None, None,
                                started)
        stop = self._halted_output(task, started)            # a lost sign-in: no container
        if stop is not None:
            return stop
        if not self.task_dir(task).is_dir():
            raise ValueError(f"no Harbor task directory {self.task_dir(task)} for {task['id']}")
        why = self.unsupported(task) if self.cli is not None else []
        if why:                                              # Harbor would refuse it for this agent: no container
            return self._output(task, no_trial(
                "unsupported_task", f"Harbor runs {self.task_dir(task).name} for none of Hermit's agents "
                f"(acting_agent.cli: {self.cli}): it declares {' and '.join(why)}; leave it out of the split, or run "
                "it with one of Harbor's own agents (acting_agent.name)"), None, None, started)
        if adapter is not None and self.cli is None:
            self.agent_config(task, deployed={})             # refuses, before anything is staged
        try:
            if task.get("step") is not None:
                return self._run_step(task, adapter, started)
            return self._run_trial(task, adapter, started)
        except OSError as exc:
            # this machine's, past the guarded steps (no process left to start Harbor, a file it could not write or
            # read): the host's failure, not the agent's result; a session may have run, so its cost is unknown. A
            # halt the rollout met before it (halt: a lost sign-in, an account's limit, whose HALT file a full disk
            # may not have let it write) stays its verdict: nothing later in the run can succeed
            failed = f"{type(exc).__name__}: {exc}"
            halted = self._rollout.halted
            verdict = {**no_trial("host_failed", failed[-2000:]), "ran": None} if halted is None else \
                {**halted, "ran": None, "exception_message": f"{halted['exception_message'][:1500]} (then this "
                                                             f"machine's {failed[-400:]})"}
            return self._output(task, verdict, None, None, started)

    def _halted_output(self, task: dict, started: float) -> RunOutput | None:
        """The rollout's output while <jobs_dir>/HALT exists (a lost sign-in, an account's limit: no container), else
        None."""
        halted = self.halted()
        if halted is None:
            return None
        return self._output(task, no_trial("halted", f"halted by {self.halt_file()}: "
                                                     f"{halted.splitlines()[0] if halted else ''}", halt=True),
                            None, None, started)

    def _run_trial(self, task: dict, adapter: Path | None, started: float) -> RunOutput:
        # a row asking for a new run (`fresh`: an Evolver's run_agent) is neither taken from the cache nor kept there;
        # nor is a trial whose key this machine could not make (its launch's dry render could not be written)
        try:
            key = self.cache_key(task, adapter) if self.params.get("trial_cache") and not task.get(FRESH) else None
        except OSError as exc:
            print(f"WARNING: no trial cache for {task['id']} ({exc}): it runs", file=sys.stderr, flush=True)
            key = None
        cached = self.cached(key)
        if cached is not None:
            return cached
        if self.login and self.cli is not None:
            out = self._login_trial(task, adapter, started)
        else:
            out = self._model_plan_wait(task, started) or self._trial_once(task, adapter, started)
        self.keep(key, task, out)
        return out

    def _model_plan_wait(self, task: dict, started: float) -> RunOutput | None:
        """Before a trial of the answer agent whose model runs on a login (model_plan): the wait while that plan holds
        sessions back, which the trial's one request would otherwise wait out inside the agent's time; then the halted
        output when another trial halted the run meanwhile, else None (the trial runs). A slot is not waited for here:
        the request's session takes one itself (CliAgent.run), so a request waiting for one (the plan's SLOTS all taken)
        waits inside the agent's time, and its trial is the agent's timeout (its result) if that time runs out; keep
        agent.concurrency at most SLOTS less the login's other sessions."""
        plan = self.model_plan()
        if plan is None:
            return None
        say = lambda m: print(f"hermit harbor {task['id']}: {m}", file=sys.stderr, flush=True)
        subscription.wait(plan[0], None, say, None, plan[1])
        return self._halted_output(task, started)

    @staticmethod
    def _attempts(runs: list[RunOutput], end: RunOutput | None = None) -> RunOutput:
        """A login trial's output (_login_trial): its last run's, or `end` (the halt met before a run). When the trial
        ran more than once, or a halt ended it after a run, it carries what every run used (one the plan refused or the
        provider could not serve may have spent tokens before it ended): the tokens the runs reported (unknown only
        when none did), their model calls (a run that reported none adds 0) and their cost (unknown when any run's
        is), and each run's own in `extra.attempts` (its usage alone: a run's verdict and job are the trial's own,
        shown to the learner as the trial is); otherwise (one run alone, or a halt before any) the output is returned
        as it is, with no `extra.attempts`."""
        last = end if end is not None else runs[-1]
        if not runs or (end is None and len(runs) == 1):
            return last
        tokens = [r.tokens for r in runs if r.tokens is not None]
        costs = [r.cost_usd for r in runs]
        calls = sum(int((r.api_speed or {}).get("model_calls") or 0) for r in runs)
        each = [{"tokens": r.tokens, "cost_usd": r.cost_usd, "model_calls": (r.api_speed or {}).get("model_calls")}
                for r in runs]
        return dataclasses.replace(last, tokens=sum(tokens) if tokens else None,
                                   cost_usd=None if None in costs else sum(costs),
                                   api_speed={**(last.api_speed or {}), "model_calls": calls},
                                   extra={**last.extra, "attempts": each})

    def _login_trial(self, task: dict, adapter: Path | None, started: float) -> RunOutput:
        """A trial on a login (ChatGPT's, Claude's): after the plan's holds, in one of its slots; again after the window
        when the plan refused the session, again after a pause when the provider could not serve it (not when it ran
        out of the agent's time: that is its result; never a lost sign-in, which a provider's notice beside it does not
        make busy); never once another trial halted the run while it waited. What every run used is the trial's
        (_attempts)."""
        plan = subscription.plan_dir(self.params.get("plan_dir"), self.login)
        credits = subscription.opted_in(self.params.get("usage_credits"))
        retries, pause = self.params.get("busy_retries"), self.params.get("busy_wait_s")
        retries, pause = int(3 if retries is None else retries), float(120 if pause is None else pause)
        say = lambda m: print(f"hermit harbor {task['id']}: {m}", file=sys.stderr, flush=True)
        busy, runs = 0, []
        while True:
            subscription.wait(plan, None, say, None, credits)          # as long as the plan holds sessions back
            with subscription.slot(plan, None, say) as got:
                if got is not True or subscription.held(plan, credits):
                    continue
                stop = self._halted_output(task, started)  # written by another trial while this one waited or slept
                if stop is not None:
                    return self._attempts(runs, stop)
                out = self._trial_once(task, adapter, started)
                runs.append(out)
                # What the session says of the plan is written before its slot is given back, as CliAgent.run writes
                # it: a trial waiting for the slot reads the plan the moment it has it, so with SLOTS: 1 the one after
                # a session that drew credits (or was refused) is held, never started in between.
                facts = out.extra.get("session") or {}
                report = facts.get("plan_report")
                if report is not None:
                    subscription.note_usage(plan, None, report)
                    if report.get("using_credits") and not credits:
                        say("a session drew the account's usage credits, billed apart from the plan: sessions wait "
                            "for the plan's window (usage_credits: true allows them)")
                        subscription.hold(plan, report.get("credits_resets_at") or report.get("resets_at"),
                                          "usage_credits", max_s=0.0)
                # a lost sign-in is returned (it halts), never held or run again, as CliAgent.run returns it
                refused = bool(facts.get("refused")) and not facts.get("served") and not facts.get("login_failed")
                if refused:
                    subscription.hold(plan, until=facts.get("held_until") or None)
            if refused:
                say("the plan refused the session (its usage limit): the trial runs again once the plan allows")
                continue
            ran_out = out.extra.get("exception_type") == "AgentTimeoutError"    # the agent's own time: its result
            if facts.get("unavailable") and not facts.get("served") and not ran_out and not facts.get("login_failed") \
                    and busy < retries:
                busy += 1
                say(f"the provider could not serve the session; again in {pause * busy:.0f} s ({busy}/{retries})")
                time.sleep(pause * busy)
                continue
            return self._attempts(runs)

    def _trial_once(self, task: dict, adapter: Path | None, started: float) -> RunOutput:
        admitted: list = []
        try:
            return self._trial_admitted(task, adapter, started, admitted)
        finally:
            for key in admitted:                             # the room it held for its image's pull
                self.pulls.done(key)

    def _stage(self, task: dict, adapter: Path | None, where: Path) -> dict | None:
        """The adapter staged for the acting CLI under `where`, with the task's context when it is per task (computed
        from the task's own row: the keys the framework names a trial by, ROW_KEYS, are no field of the task). It runs
        on this machine before any container: an OSError there (a full disk, a folder it cannot write) is the host's,
        and the trial or the step that met it is infra (staging_failed). A RuntimeError is the adapter's: its
        deployment or its per-task context failed in their child process, the agent's result (scored 0), as on main;
        what such a child prints cannot tell the host's failure from the adapter's, which could print one."""
        if self.cli is None:
            return None
        from hermit.harbor.launch import context_limit
        return stage(adapter, where, self.python, self.params.get("sandbox_enforce"), self.cli,
                     task={k: v for k, v in task.items() if k not in ROW_KEYS},
                     context_query=self.params.get("context_query"),
                     context_limit=context_limit(self.cli, self.settings(task)) if self.cli == "codex" else None)

    def _tools_host(self) -> str:
        from hermit.harbor.tools import docker_gateway
        return str(self.params.get("tools_host") or docker_gateway())

    def _launch(self, task: dict, job: Path, deployed: dict | None, served) -> Path | None:
        """The trial's launch plan under `job` (none for an agent outside the table)."""
        if self.cli is None:
            return None
        settings = self.settings(task)
        meta = {"adapter_id": (deployed or {}).get("adapter_id"),
                "tools": served.names if served is not None and hasattr(served, "names") else []}
        if self.cli == "answer":
            system = ""
            if deployed and deployed.get("context_file"):
                system = Path(deployed["context_file"]).read_text(encoding="utf-8")
            return launch.render_answer(settings, endpoint=served.url, job=job, system=system,
                                        options=self.params.get("answer") or {}, metadata=meta)
        config = self.task_config(task)
        tools_url = served.mcp_url if served is not None else None
        return launch.render(self.cli, settings, deploy=deployed, tools_url=tools_url, job=job, login=self.login,
                             tools_name=str(deployed["adapter_id"]).replace("-", "_") if tools_url else None,
                             workdir=(config.get("environment") or {}).get("workdir"), metadata=meta,
                             login_root=self.login_root, **self._mounted())

    def _serve(self, deployed: dict | None, job: Path, adapter: Path | None):
        """What the trial's agent reaches on this machine: the answer agent's model endpoint, or the adapter's tools
        (none without agent tools); started."""
        if self.cli == "answer":
            from hermit.harbor.tools import ModelEndpoint
            return ModelEndpoint(self.answer_model(), records=job, enforce=self.params.get("sandbox_enforce"),
                                 plan_notes=self.model_plan() is not None).start()
        if self.cli is None or not deployed or not deployed.get("tools"):
            return None
        from hermit.harbor.tools import AdapterTools
        return AdapterTools(adapter, audit_log=job / curate_mod.TOOL_AUDIT, host=self._tools_host(),
                            enforce=self.params.get("sandbox_enforce")).start()

    def _trial_admitted(self, task: dict, adapter: Path | None, started: float, admitted: list) -> RunOutput:
        if not self.wait_for_disk(task, admitted):
            return self._output(task, no_trial("disk", f"the Docker data root {self._docker_root()} stayed under "
                                                       f"{self.params.get('disk_gate_gb', DISK_GATE_GB)} GB free: no "
                                                       "container started"), None, None, started)
        job_name = f"{_safe(task['id'])}-{uuid.uuid4().hex[:8]}"
        job = self.jobs_dir / job_name
        try:
            job.mkdir()
            deployed = self._stage(task, adapter, job)
        except OSError as exc:                                # this machine's, before any container: no trial
            return self._output(task, no_trial("staging_failed", str(exc)[-2000:]), None, None, started,
                                extra={"harbor_job": str(job)})
        served = plan = None
        try:
            try:
                served = self._serve(deployed, job, adapter)
            except (RuntimeError, OSError) as exc:           # no trial without its tools (or its model)
                return self._output(task, no_trial("tools_failed", str(exc)[-2000:]), None, None, started,
                                    extra={"harbor_job": str(job), "deployed": deployed})
            try:
                plan = self._launch(task, job, deployed, served)
                agent = self.agent_config(task, plan, deployed, tools_host=getattr(served, "host", None))
                (job / "job.yaml").write_text(yaml.safe_dump(self.job_config(task, job_name, agent),
                                                             sort_keys=False))
                keep = set(json.loads(plan.read_text()).get("env_from_host", {}).values()) if plan else set()
            except OSError as exc:              # this machine's, writing the trial's launch and job: no container yet
                return self._output(task, no_trial("staging_failed", str(exc)[-2000:]), None, None, started,
                                    extra={"harbor_job": str(job), "deployed": deployed})
            code, expired = self.harbor_run(job, job / "job.yaml", task, keep)
        finally:
            if served is not None:
                served.close()
            self.sign_in_back(job)
        trials = [t for t in self.trial_dirs(job, task) if (t / "result.json").is_file()]
        trial = trials[-1] if trials else None
        result = None
        if trial is not None:
            try:
                result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
            except ValueError:
                result = None
        verdict = classify(result, expired=expired, harbor_exit=code, reward_key=self.reward_key)
        verdict = self.verifier_timeout_verdict(verdict)
        facts, usage, said = self.session(trial, plan, served, job, task)
        if facts is not None:
            verdict = self.session_verdict(verdict, facts, self.login)
        verdict = self.verifier_verdict(verdict, trial, self.redactions)
        if verdict["halt"]:
            self.halt(verdict, job)
        log = curate_mod.curate(trial, job, verdict, redact=self.redactions, audit=job / curate_mod.TOOL_AUDIT)
        self.cleanup_images(task)
        extra = {"harbor_job": str(job), "harbor_exit": code, "deployed": deployed}
        return self._output(task, verdict, result, log, started, trial=trial.name if trial else None, extra=extra,
                            trajectory=trial / "agent" / "trajectory.json" if trial else None, facts=facts,
                            usage=usage, said=said, deployed=deployed)

    def sign_in_back(self, job: Path) -> None:
        """After the trial (or the step) under `job`, however it ended: the ChatGPT sign-in Codex refreshed in the
        container becomes the shared copy (when it is a later refresh of it, hermit.agent.base.login.take_back,
        stamped at most SKEW ahead of this machine's clock in Harbor's local Docker environment, whose containers share
        that clock, and OTHER_CLOCK in another, whose clock may differ), and no copy of a sign-in is left in the job
        (the home's, the one that came back)."""
        where = Path(job) / launch.LAUNCH_DIR
        returned = where / launch.RETURN_DIR
        try:
            if self.login == "chatgpt" and (returned / subscription.FILE).is_file():
                local = (self.params.get("environment") or {}).get("type") in (None, "docker")
                subscription.keep_refreshed(self.login_root, returned,
                                            skew=subscription.SKEW if local else subscription.OTHER_CLOCK)
        except OSError as exc:      # this machine's (the shared copy's folder full): the trial stands, its refresh lost
            print(f"WARNING: the sign-in Codex refreshed in {Path(job).name} could not be taken back ({exc}); a "
                  "later session refreshes again", file=sys.stderr, flush=True)
        for folder in (returned, where / launch.HOME_DIR):
            if folder.is_dir() and not folder.is_symlink():
                subscription.remove(folder / subscription.FILE, folder)

    def verifier_timeout_verdict(self, verdict: dict) -> dict:
        """A verifier that ran out of its time (VerifierTimeoutError) is the agent's result with `verifier_timeout:
        agent`, the default, as Harbor grades it: a timeout on the agent's solution (solution_timed_out), scored its
        reward or 0 and not run again (a solution too slow for its tests, as AlgoTune's verifier times it, or a process
        it left that hung them). With `verifier_timeout: infra`, for a host whose load makes verifiers time out, it is
        infra: run again."""
        if verdict.get("exception_type") != "VerifierTimeoutError":
            return verdict
        if self.verifier_timeout == "infra":
            return {**verdict, "status": "infra", "failure_cause": "verifier_failed"}
        return {**verdict, "status": "timeout", "failure_cause": "solution_timed_out", "timed_out": True}

    # --- the acting CLI's session, from its stream ------------------------------------------------------------------
    def session(self, outputs: Path | None, plan: Path | None, served, job: Path, task: dict | None = None):
        """(the session's facts, its usage, what its stream said) of a trial or a step whose outputs are under
        `outputs` (agent/), read as Hermit reads a session of the CLI (its Base Agent's `outcome`, `parse`,
        `adjust_usage`, priced at the binding's price): served, refused for the plan's limit (and until when), the
        provider unavailable, the sign-in lost, rejected, a model's refusal, the plan's report (Claude Code's stream's;
        on a ChatGPT login Codex's session logs', collected), started, the tools down, the tokens used; (None, None, {})
        for an agent outside the table or a session that never ran (no stream). The container could write all of it:
        each file is read only as a regular file, never through a link (a link in the stream's place is no stream, a
        link among the collected files is not read), and the stream is read once, then from this process's copy."""
        if plan is None or self.cli is None:
            return None, None, {}
        data = json.loads(plan.read_text(encoding="utf-8"))
        agent_dir = outputs / "agent" if outputs is not None and _folder(outputs / "agent") else None
        if self.cli == "answer":
            said = {}
            if agent_dir is not None:
                try:
                    said = json.loads(_read(agent_dir / "answer.json") or "{}")
                except (ValueError, RecursionError):    # no JSON, or nested past the parser's depth
                    said = {}
                said = said if isinstance(said, dict) else {}    # JSON, but no object: no answer either
            facts = served.facts() if served is not None else {"started": False}
            if facts.get("plan_waited") and facts.get("calls") == 1 and \
                    not any(s is not None for s in facts.get("statuses") or []):
                # its one request never answered, and its session waited inside it for its login's plan (CliAgent.run,
                # as the session recorded it: the endpoint's plan_notes): why the plan held it back
                facts["plan_held"] = facts["plan_waited"]
            usage = served.usage if served is not None and served.usage.calls else None
            return facts, usage, {"reply": said.get("reply"), "answer": said.get("answer")}
        raw = _read(agent_dir / data["stream"]) if agent_dir is not None else None
        if raw is None:
            return None, None, {}
        # its lines end as in Hermit's own run, which reads a stream with Path.read_text: "\r\n" and "\r" are "\n" (an
        # event's strings hold a "\r" escaped, so no event is split by it)
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        from hermit.agent.base.cli import DSH_TOOLS_DOWN
        from hermit.agent.base.interface import make
        from hermit.agent.base.records import price_usage
        settings = self.settings_of(data, task)
        agent = make(clis.entry(self.cli)["backend"], settings)
        read = job / launch.LAUNCH_DIR / "read"         # what is read of the session, as this process took it
        shutil.rmtree(read, ignore_errors=True)
        read.mkdir(parents=True)
        stream = read / data["stream"]
        stream.write_text(raw, encoding="utf-8")
        collected = agent_dir / curate_mod.COLLECT_DIR
        collected = collected if _folder(collected) else None
        said = agent.outcome(raw, stream, collected / launch.LAST_MESSAGE if collected is not None else None)
        started = said.pop("started", None)
        if started is None:
            started = any(line.lstrip().startswith("{") for line in raw.split("\n"))
        model = (settings.get("binding") or {}).get("ref") or settings.get("model") or agent.backend
        handle, usage = agent.parse(stream, model)
        stream.unlink()                                 # read: the trial's own copy stays in its outputs
        for src, dst in launch.COLLECT.get(agent.backend, []) if collected is not None else []:
            if dst != launch.LAST_MESSAGE:          # what adjust_usage reads of the home, back where the home had it
                _copy(collected / dst, read / src)
        db = read / "data" / "opencode" / "opencode.db"
        if db.is_file() and db.with_name(db.name + "-wal").is_file():
            # OpenCode keeps its database in WAL mode: a session cut off leaves its last rows in the -wal file, which
            # the reader (opened immutable) never opens; this copy is the runner's own, so they are merged into it
            import contextlib
            import sqlite3
            try:
                with contextlib.closing(sqlite3.connect(db)) as c:
                    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
        usage = agent.adjust_usage(read, handle, usage, resumed=False)
        price_usage(usage, settings)
        down = DSH_TOOLS_DOWN.findall(agent.notices(raw)) if agent.backend == "dsh" else []
        if agent.backend == "claude" and settings.get("tools_name"):
            name = settings["tools_name"]
            for event in _events(raw):                       # Claude Code's init event names each MCP server's status
                if event.get("type") == "system" and event.get("subtype") == "init":
                    status = {s.get("name"): s.get("status") for s in event.get("mcp_servers") or []
                              if isinstance(s, dict)}
                    if status.get(name) not in (None, "connected"):
                        down.append((name, str(status.get(name))))
                    break
        facts = {k: said.get(k) for k in SESSION_FACTS if k in said}
        facts.update(started=bool(started), tools_down=bool(down), session=handle,
                     failure=str(said.get("failure") or "")[-600:],
                     tokens=usage[0].input_tokens + usage[0].output_tokens if usage else 0)
        if down:
            facts["failure"] = f"the adapter's tools did not come up ({down[0][0]}: {down[0][1]})"[-600:]
        if self.login == "chatgpt" and collected is not None:
            try:                                             # Codex's session logs, collected: the plan's rate limits
                report = subscription.usage_of(collected)
            except Exception:                                # noqa: BLE001 - the container wrote them: one that
                report = None                                # cannot be read is no report (kept as defence: usage_of
                                                             # skips a line that is no object itself)
            if report is not None:
                facts["plan_report"] = report
        return facts, (usage[0] if usage else None), {"reply": said.get("reply")}

    def settings_of(self, plan: dict, task: dict | None = None) -> dict:
        """The Base Agent's settings a plan was rendered with, for reading its stream (its tools' name, the model)."""
        settings = self.settings(task)
        if plan.get("metadata", {}).get("tools"):
            settings["tools_name"] = str(plan["metadata"].get("adapter_id") or "hermit_tools").replace("-", "_")
        return settings

    @staticmethod
    def session_verdict(verdict: dict, facts: dict, login: str | None = None) -> dict:
        """The trial's verdict with what the session's stream adds. A lost sign-in halts. A plan's refusal or a provider
        that could not serve is not the agent's doing (infra: the trial is run again), unless the session ran out of
        the agent's time (its result, kept: a timed-out session has no completed turn, and an earlier notice it
        recovered from is no failure, as Hermit's own sessions keep it); nor is a provider that refused the run's
        configuration before it served any request (a key it does not take, a permission it does not give, a model it
        does not have, an account it does not bill, a request setting it does not take: model_rejected, config_refused;
        no halt), nor a session that never started or whose tools never came up (session_failed). A first request
        refused for what the adapter's or the task's input made it (a prompt too long, an invalid request, a content
        filter) is the agent's result, as Harbor scores it, and so is the model's own refusal (agent_refused_task,
        kept). A usage limit on a `login` is the login's to wait for (the plan's window resets): no halt; without one
        (an API account's), nothing later can be served: a halt. The plan of the login the answer agent's model runs
        on is that login's to wait for too (`plan_held`: its one request unanswered, its session having waited inside
        it for that plan, as the session recorded it; a hold that came while the session ran is no wait of its own): a
        trial that ran out of the agent's time with it is infra (usage_limit, no halt), not the agent's timeout."""
        def message():
            return verdict.get("exception_message") or facts.get("failure") or ""
        if facts.get("login_failed"):
            return {**verdict, "status": "infra", "failure_cause": "login_failed", "halt": True,
                    "exception_type": verdict.get("exception_type") or "AgentAuthenticationError",
                    "exception_message": message()}
        ran_out = verdict.get("exception_type") == "AgentTimeoutError"
        if ran_out and facts.get("plan_held") and not facts.get("served"):
            return {**verdict, "status": "infra", "failure_cause": "usage_limit", "halt": False,
                    "exception_message": f"the model's one request waited for its login's plan, which held its "
                                         f"session back ({facts['plan_held']}), and had no answer when the agent's "
                                         "time ran out"}
        if (facts.get("refused") or facts.get("unavailable")) and not facts.get("served") and not ran_out:
            cause = "usage_limit" if facts.get("refused") else "model_provider_failed"
            return {**verdict, "status": "infra", "failure_cause": cause,
                    "halt": bool(facts.get("refused")) and not login, "exception_message": message()}
        if facts.get("rejected") and not facts.get("served") and not facts.get("model_refusal") and \
                not facts.get("tokens") and config_refused(facts):
            return {**verdict, "status": "infra", "failure_cause": "model_rejected", "halt": False,
                    "exception_message": message()}
        if verdict["status"] != "infra" and (not facts.get("started") or facts.get("tools_down")):
            why = facts.get("failure") if facts.get("tools_down") else \
                f"the session never started (no event in its stream): {message()}"
            return {**verdict, "status": "infra", "failure_cause": "session_failed", "halt": False,
                    "exception_message": str(why)[-2000:]}
        if facts.get("model_refusal") and verdict["status"] in ("infra", "agent_error"):
            # The model's safeguards stopped the session (a refusal stop reason): the agent's own end, as Harbor's
            # AgentSafetyRefusalError is (agent_refused_task), kept as its result, not run again
            return {**verdict, "status": "agent_error", "failure_cause": "agent_refused_task", "halt": False,
                    "exception_message": message()}
        waits = bool(login) and verdict.get("exception_type") == "ApiUsageLimitError"   # the login loop's to hold
        return {**verdict, "halt": verdict["halt"] and not waits}

    @staticmethod
    def verifier_verdict(verdict: dict, trial: Path | None, redact=()) -> dict:
        """A trial whose verifier ran no test is infra: run again, not kept as the agent's result, whether the agent
        finished, ran out of its time or exited with an error (Harbor verifies all three and grades the reward), and
        whether the verifier wrote 0, left no reward or ran out of its time (the verifier's own failure, which is
        otherwise the agent's result, as Harbor grades it). Either the verifier's tools never arrived, their download
        failed by a server (a 5xx, GitHub's release download) so its test runner is missing and it wrote no report
        (hermit.harbor.errors.verifier_unreached), or the host's disk was full while it ran, before its tests wrote a
        report (verifier_disk_full: with a report, the words are a test's output). A trial the verifier passed ran its
        tests; a trial whose verifier never ran (no output of it) stays as it was.
        The verifier's output the verdict quotes has `redact`'s patterns removed (verifier_redact), as the curated
        output has."""
        if trial is None or verdict["status"] == "infra" or float(verdict.get("reward") or 0) >= 1:
            return verdict
        out = trial / "verifier"
        stdout = _text(out / "test-stdout.txt")
        if verifier_unreached(stdout, (out / "ctrf.json").exists()):
            why = "the verifier's tools never arrived (their download failed), so its tests never ran"
        elif verifier_disk_full(stdout, (out / "ctrf.json").exists()):
            why = "the host's disk was full while the verifier ran (ENOSPC)"
        else:
            return verdict
        return {**verdict, "status": "infra", "failure_cause": "verifier_failed",
                "exception_message": f"{why}: {curate_mod.redact_verifier(stdout, redact).strip()[-300:]}"}

    # --- the trial cache ------------------------------------------------------------------------------------------
    def task_digest(self, task: dict) -> str:
        """The contents of the row's task directory as one digest (hermit.harbor.dataset.digest), read once per
        directory for the runner's life."""
        where = str(self.task_dir(task))
        if where not in self._digests:
            self._digests[where] = dataset.digest(self.task_dir(task))
        return self._digests[where]

    def cache_key(self, task: dict, adapter: Path | None) -> str:
        """A trial's identity: the training run it is of (`trial_scope`, which hermit.isolate.agent_pipeline gives the
        Trainer's pipeline alone: a run's trials are its own; any other pipeline's are any eval's), the whole row (each
        of its keys: an eval's repeat, a training epoch too) and its task directory's contents (task_digest: a task
        edited in place is another trial), the acting agent and its model (the answer agent's with its client's timeout
        and retries), the launch its CLI would get (the plan's digest: its home files and command, its settings), the
        adapter's files (their digest) and the row's field its per-task context retrieves for (context_query), the
        environment (its overlays' contents), the verifier's config, the multipliers, a mounted binary, web search,
        Harbor's retries of a trial (`retry`), the adapter sandbox's enforce and its tools' address, and what the judge
        reads (pass_reward, verifier_redact, reward_key). A model a CLI reaches through the pipeline's own endpoint is
        named by that CLI, its login and its identity (`model_cli_identity`: its settings and its binary), never by
        the endpoint's URL."""
        tools = False
        if adapter is not None and self.cli not in (None, "answer"):
            from hermit.adapter.loader import agent_tools, load_manifest
            tools = bool(agent_tools(load_manifest(Path(adapter))))
        environment = self.environment_config()
        overlays = [hashlib.sha256(Path(p).read_bytes()).hexdigest() if Path(p).is_file() else p
                    for p in environment.get("extra_docker_compose") or []]
        # the answer agent's model as its endpoint serves it: with its client's timeout and retries (answer_model)
        model = self.answer_model() if self.cli == "answer" else \
            self.binding.spec() if self.binding is not None else self.params.get("model")
        if isinstance(model, dict) and self.params.get("model_cli"):
            # a model a CLI reaches through this pipeline's own endpoint (hermit.isolate.agent_pipeline): its URL, a
            # port and a token of this pipeline's, names no trial (a resumed run or another eval has another), nor does
            # a field the spec derives from it (the binding's messages_url); the CLI, its login and what else its
            # sessions answer with do (its settings, its binary: hermit.agent.base.endpoint.cli_identity)
            url = str((self.params.get("model") or {}).get("base_url") or "") or None
            model = {**{k: v for k, v in model.items()
                        if k != "base_url" and not (url and isinstance(v, str) and url in v)},
                     "cli": self.params["model_cli"], "cli_login": self.params.get("model_login"),
                     "cli_identity": self.params.get("model_cli_identity")}
        same = {"row": task, "task": self.task_digest(task), "agent": self.acting_agent, "model": model,
                "effort": self.params.get("effort"), "login": self.login,
                "versions": {k: v for k, v in self.params.items() if k.endswith("_version")},
                "timeout": self.agent_timeout_sec, "adapter": adapter_digest(adapter),
                "context_query": self.params.get("context_query") or None,      # None: the default query's fields
                "plan": self._dry_render(task, tools=tools) if self.cli is not None else None,
                "environment": environment, "overlays": overlays, "verifier": self.verifier_config(),
                "multipliers": [self.timeout_multiplier(k) for k in TIMEOUT_MULTIPLIERS],
                "bins": {name: _file_identity(self.bin(name)) for name in BINS if self.bin(name) is not None},
                "web_search": self.params.get("web_search"), "pass_reward": self.pass_reward,
                "verifier_redact": self.redactions, "reward_key": self.reward_key,
                "verifier_timeout": self.params.get("verifier_timeout"), "retry": self.params.get("retry"),
                "sandbox_enforce": self.params.get("sandbox_enforce"), "tools_host": self.params.get("tools_host")}
        if self.params.get("trial_scope"):                   # a training run's: its trials its own
            same["scope"] = str(self.params["trial_scope"])
        return hashlib.sha256(json.dumps(same, sort_keys=True, default=str).encode()).hexdigest()[:40]

    def cached(self, key: str | None) -> RunOutput | None:
        path = self.jobs_dir / CACHE_DIR / f"{key}.json" if key else None
        if path is None or not path.is_file():
            return None
        try:
            kept = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):                        # unreadable: no trial kept (it runs again)
            return None
        if not isinstance(kept, dict) or not isinstance(kept.get("output"), dict):
            return None
        log = Path(kept["log"]) if kept.get("log") else None
        if log is not None and not log.exists():
            return None                                      # its log is gone: run it again
        # Taken from the cache, the trial spends nothing again (the run's budget adds what a rollout returns): what it
        # spent when it ran stays in `extra`, cached_cost_usd and cached_tokens
        return RunOutput(output=kept["output"], log=log, cost_usd=0.0, wall_s=kept.get("wall_s", 0.0), tokens=0,
                         api_speed=kept.get("api_speed"),
                         extra={**(kept.get("extra") or {}), "from_trial_cache": key,
                                "cached_cost_usd": kept.get("cost_usd"), "cached_tokens": kept.get("tokens")})

    def keep(self, key: str | None, task: dict, out: RunOutput) -> None:
        """A finished trial into the cache: one the agent ended (ok, timeout, agent_error), never one the
        infrastructure, the plan or a halt ended."""
        if not key or (out.output or {}).get("status") not in CACHED or out.extra.get("halt") or \
                out.extra.get("unscored"):
            return
        root = self.jobs_dir / CACHE_DIR
        kept = {"row": task["id"], "output": out.output, "log": str(out.log) if out.log else None,
                "cost_usd": out.cost_usd, "wall_s": out.wall_s, "tokens": out.tokens, "api_speed": out.api_speed,
                "extra": out.extra, "kept": datetime.now().isoformat(timespec="seconds")}
        tmp = root / f".{key}.{uuid.uuid4().hex[:6]}"
        try:
            root.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(kept, default=str) + "\n", encoding="utf-8")
            os.replace(tmp, root / f"{key}.json")
        except OSError as exc:               # this machine's (a full disk): the trial stands, only uncached
            if os.path.lexists(tmp):
                os.unlink(tmp)
            print(f"WARNING: the trial cache could not keep {task['id']}'s trial ({exc}): it stands, and a resumed "
                  "run runs it again", file=sys.stderr, flush=True)

    # --- streams --------------------------------------------------------------------------------------------------
    def _stream_tools(self, stream: str, deployed: dict | None, step_dir: Path, adapter: Path | None):
        """The stream's tool server: one for the stream's life, started by the first rollout of the stream whose
        adapter has tools (with that step's adapter and log: whichever step serves next swaps its own in); None for a
        step whose adapter has none."""
        if self.cli in (None, "answer") or not deployed or not deployed.get("tools"):
            return None
        from hermit.harbor.tools import AdapterTools
        with self._streams_lock:
            served = self._streams.get(stream)
            if served is not None:
                return served
            served = AdapterTools(adapter, audit_log=step_dir / curate_mod.TOOL_AUDIT, host=self._tools_host(),
                                  enforce=self.params.get("sandbox_enforce")).start()
            self._streams[stream] = served
            return served

    def _stream_done(self, stream: str) -> None:
        """A stream this runner is done with (its driver stopped, or gone): its tool server closed, its driver no longer
        this runner's to stop."""
        with self._streams_lock:
            served = self._streams.pop(stream, None)
            self._drivers.pop(stream, None)
        if served is not None:
            served.close()

    def _close_streams(self) -> None:
        for stream in list(getattr(self, "_streams", {})):
            try:
                self._stream_done(stream)
            except Exception:                                # noqa: BLE001 - at exit, best effort
                pass

    def close(self) -> None:
        """The streams this runner stepped that are still open, closed: unless they are kept for a resume
        (keep_streams), the drivers still running of those that did not finish are stopped (a pipeline that never
        resumes never meets them again, and each would hold its task's environment for stream.idle_s; a stream that
        finished was stopped by its last step); every stream's tool server is closed. hermit.isolate.agent_pipeline's
        close calls it, and _close_all at exit."""
        while not self.keep_streams:
            with self._streams_lock:
                if not self._drivers:
                    break
                _, client = self._drivers.popitem()
            try:
                if client.running() is not None:
                    client.stop()
            except Exception:                                # noqa: BLE001 - best effort, as at exit
                pass
        self._close_streams()

    def _run_step(self, task: dict, adapter: Path | None, started: float) -> RunOutput:
        """One step of a multi-step task, through the stream's driver. While it runs, its index is one of its stream's
        steps in flight (`_in_flight`): a step whose turn cannot come ends at once (infra, stream_order: its driver
        waits for a step no rollout of this runner is running, or it has none and no rollout of its first step is
        running to start one), never waiting for its turn until the deadline, and the last of its stream's rollouts to
        end then stops the driver (_leave)."""
        index = int(task["step_index"])
        # each of hermit eval's repeats (eval.repeats > 1 names it in the row) is a stream of its own: its driver, its
        # step directories and their curated logs are never an earlier repeat's; and so is each run's, by what
        # hermit.isolate.agent_pipeline names it for (`run_identity`): a hermit train by its record directory (path
        # and the id it keeps), any other pipeline (hermit eval, hermit check's probe) by its own. Two runs of
        # one task, at once or one after the other (a train started over at the same path, an eval run again into the
        # same output), never share a driver, its environment or step logs, and a resumed train meets its own again
        repeat = f"__r{task['repeat']}" if task.get("repeat") is not None else ""
        run = f"__{hashlib.sha256(str(self.params['run_identity']).encode()).hexdigest()[:8]}" \
            if self.params.get("run_identity") else ""
        stream = _safe(f"{task.get('harbor_task') or task['id']}__s{task.get('rollout', 1)}{repeat}{run}")
        with self._streams_lock:
            self._in_flight.setdefault(stream, []).append(index)
        try:
            return self._step(task, adapter, started, stream)
        finally:
            self._leave(stream, index)

    def _leave(self, stream: str, index: int) -> None:
        """A step's rollout ending: its index out of its stream's steps in flight. The last of a stream whose next turn
        cannot come (`_given_up`) stops its driver, whose environment none of the stream's steps can use any more, and
        is done with the stream, which its first step may then start again."""
        with self._streams_lock:
            steps = self._in_flight[stream]
            steps.remove(index)
            if steps:
                return
            del self._in_flight[stream]
            if stream not in self._given_up:
                return
        client = StreamClient(self.jobs_dir / "streams" / stream, python=self.harbor_python)
        if client.running() is not None:
            client.stop()
        self._stream_done(stream)
        with self._streams_lock:
            if stream not in self._in_flight:                # no step of it came in the meantime
                self._given_up.pop(stream, None)

    def _first_step_missing(self, stream: str, client: StreamClient) -> str | None:
        """Why a stream that has no driver gets none for a later step: no rollout of its first step is running in this
        runner (none either after TURN_GRACE_POLLS polls, in which one submitted just before this one has begun), or its
        next turn already cannot come; None once a driver or such a rollout is there."""
        for poll in range(stream_mod.TURN_GRACE_POLLS + 1):
            if poll:
                time.sleep(stream_mod.POLL_S)
            with self._streams_lock:
                if stream in self._given_up:
                    return self._given_up[stream]
                if 1 in self._in_flight.get(stream, ()):
                    return None
            if client.running() is not None:
                return None
        return ("the stream has no driver (none started in this run, or the one its earlier steps ran in was stopped "
                "or idled out) and no rollout of its first step is running to start one")

    def _step_missing(self, stream: str, nxt: int) -> str | None:
        """Why no rollout will ask the stream's driver, idle, for the step `nxt` it waits for (stream.wait_turn's
        `missing`): none of this runner's is running it (it ended before it reached the driver, or ran in an earlier
        training step), or the stream's next turn already cannot come; None while one of its rollouts runs."""
        with self._streams_lock:
            if stream in self._given_up:
                return self._given_up[stream]
            if nxt in self._in_flight.get(stream, ()):
                return None
        return (f"the driver waits for step {nxt}, which no rollout is running (it ended before it reached the driver, "
                "or ran in an earlier training step)")

    def _step(self, task: dict, adapter: Path | None, started: float, stream: str) -> RunOutput:
        """A step's rollout in its stream (_run_step)."""
        index, name = int(task["step_index"]), str(task["step"])
        total = int(task.get("steps") or 0)
        stream_dir = self.jobs_dir / "streams" / stream
        settings = {"idle_s": 4 * 3600, "start_timeout_s": 3600,    # one left empty (null) is its default
                    **{k: v for k, v in (self.params.get("stream") or {}).items() if v is not None}}
        client = StreamClient(stream_dir, python=self.harbor_python)
        step_dir = stream_dir / "steps" / f"{index:03d}-{_safe(name)}"
        step_dir.mkdir(parents=True, exist_ok=True)
        try:
            deployed = self._stage(task, adapter, step_dir)
        except OSError as exc:                                # this machine's, before the step: not run
            return self._output(task, no_trial("staging_failed", str(exc)[-2000:]), None, None, started,
                                extra={"harbor_job": str(step_dir), "stream_dir": str(stream_dir)})
        reply, result, error, cause = None, None, None, "stream_lost"
        served = endpoint = plan = None
        lost = asked = turn = False
        try:
            try:
                if self.cli == "answer":
                    from hermit.harbor.tools import ModelEndpoint
                    endpoint = ModelEndpoint(self.answer_model(), records=step_dir,
                                             enforce=self.params.get("sandbox_enforce"),
                                             plan_notes=self.model_plan() is not None).start()
                else:
                    served = self._stream_tools(stream, deployed, step_dir, adapter)
            except (RuntimeError, OSError) as exc:
                return self._output(task, no_trial("tools_failed", str(exc)[-2000:]), None, None, started,
                                    extra={"harbor_job": str(step_dir), "deployed": deployed})
            try:
                plan = self._launch(task, step_dir, deployed, endpoint or served)
                agent = self.agent_config(task, plan, deployed, tools_host=getattr(served, "host", None))
                (step_dir / "agent.json").write_text(json.dumps(agent, indent=1) + "\n")
                keep = set(json.loads(plan.read_text()).get("env_from_host", {}).values()) if plan else set()
            except OSError as exc:                            # this machine's, as the staging's: the step not run
                return self._output(task, no_trial("staging_failed", str(exc)[-2000:]), None, None, started,
                                    extra={"harbor_job": str(step_dir), "stream_dir": str(stream_dir),
                                           "deployed": deployed})
            try:
                if client.running() is None and index > 1 and (why := self._first_step_missing(stream, client)):
                    raise TurnLostError(index, why)          # no driver (an environment) started for it
                if client.running() is None and not self.wait_for_disk():
                    return self._output(task, no_trial("disk", f"the Docker data root {self._docker_root()} stayed "
                                                               f"under {self.params.get('disk_gate_gb', DISK_GATE_GB)} "
                                                               "GB free: the stream's environment was not started"),
                                        None, None, started)
                with self._streams_lock:                     # this runner's to stop until it finishes (close)
                    self._drivers.setdefault(stream, client)
                client.ensure(task_dir=self.task_dir(task), name=stream, agent=agent,
                              environment=self.environment_config(), verifier=self.verifier_config(),
                              idle_s=float(settings["idle_s"]), env=self.harbor_env(keep),
                              start_timeout_s=float(settings["start_timeout_s"]),
                              trial={k: self.timeout_multiplier(k) for k in TIMEOUT_MULTIPLIERS})
                budget = self.wait_budget(task, build=False)
                # Earlier steps of the stream may be running in other rollouts of the same batch: wait for them, each
                # for its own budget at most (or `stream.turn_wait_s`); not for one no rollout is running (rows are
                # submitted in order: an earlier step of the batch has begun by then), whose turn never comes.
                turn_wait = float(settings.get("turn_wait_s") or (max(1, index - 1) * budget
                                                                 + float(settings["start_timeout_s"])))
                client.wait_turn(index, timeout_s=turn_wait, missing=lambda nxt: self._step_missing(stream, nxt))
                _set_aside_audit(step_dir)                   # this driver never ran the step: an earlier run's calls
                turn = True
                if served is not None and (served.adapter, served.audit_log) != \
                        (Path(adapter), step_dir / curate_mod.TOOL_AUDIT):
                    # this step's turn: its adapter behind the same URL, its calls in its own log (the server may have
                    # been started by a later step's rollout, or served the step before)
                    served.swap(Path(adapter), step_dir / curate_mod.TOOL_AUDIT)
                asked = True                                 # from here the driver may run the step's session
                reply = client.step(name=name, index=index, plan=str(plan) if plan else None,
                                    agent_kwargs=agent["kwargs"], timeout_s=budget)
            except StreamOrderError as exc:
                error, cause = str(exc), "stream_order"
                if isinstance(exc, TurnLostError):           # nor can a later step's: the stream is given up
                    with self._streams_lock:
                        self._given_up.setdefault(stream, exc.why)
            except (StreamError, RuntimeError) as exc:      # the driver's, or a tool server another step closed
                # a driver that is gone (it closed the connection without an answer: killed while it answered) is lost
                # as one whose socket refuses is
                error, lost = str(exc), isinstance(exc, StreamError) and client.gone()
            except OSError as exc:              # the driver gone (killed), or silent past a request's time (busy)
                error, lost = f"the stream's driver could not be reached: {exc}", not isinstance(exc, TimeoutError)
        finally:
            if endpoint is not None:
                endpoint.close()
            self.sign_in_back(step_dir)
        if reply and reply.get("ok"):
            result = reply.get("step") or {}
            verdict = self.verifier_timeout_verdict(classify(result, reward_key=self.reward_key))
        elif reply and reply.get("expired"):
            verdict = classify(None, expired=True)
        else:
            if (reply or {}).get("out_of_order"):
                cause = "stream_order"
            verdict = no_trial(cause, error or (reply or {}).get("error") or json.dumps(reply)[:500])
            if asked and cause != "stream_order":           # its session may have run, its usage unread: unknown
                verdict["ran"] = None
        outputs = Path(reply["paths"]["step_dir"]) if reply and reply.get("ok") and reply.get("paths") else None
        facts = usage = None
        said: dict = {}
        if outputs is not None:
            facts, usage, said = self.session(outputs, plan, endpoint or served, step_dir, task)
            if facts is not None:
                verdict = self.session_verdict(verdict, facts, self.login)
            verdict = self.verifier_verdict(verdict, outputs, self.redactions)
        if verdict["halt"]:
            self.halt(verdict, step_dir)
        ran = bool(reply and reply.get("ok"))
        aborted = ran and bool(reply.get("aborted"))
        finished = ran and ((total and index >= total) or bool(reply.get("last")) or aborted)
        if finished or (reply or {}).get("expired") or verdict["halt"]:
            client.stop()                                    # the environment has no further use
            self._stream_done(stream)
            if finished:
                self.cleanup_images(task)
        elif lost:
            # no step after it reaches this driver: its tools closed (not for one only slow to answer, which may still
            # be running the step before, on these tools)
            self._stream_done(stream)
        # the step's tool calls once its turn came; a step whose turn never came made none (the log there is an
        # earlier run's of the step)
        log = curate_mod.curate(outputs, step_dir, verdict, result=result,
                                extra_files={DRIVER_LOG: stream_dir / DRIVER_LOG}, redact=self.redactions,
                                audit=step_dir / curate_mod.TOOL_AUDIT if turn else None)
        return self._output(task, verdict, result, log, started, trial=stream, step=name,
                            extra={"harbor_job": str(step_dir), "stream_dir": str(stream_dir), "deployed": deployed,
                                   "aborted": aborted},
                            trajectory=outputs / "agent" / "trajectory.json" if outputs else None, facts=facts,
                            usage=usage, said=said, deployed=deployed)

    # --- the rollout's output -------------------------------------------------------------------------------------
    def _output(self, task: dict, verdict: dict, result: dict | None, log: Path | None, started: float, *,
                trial: str | None = None, step: str | None = None, extra: dict | None = None,
                trajectory: Path | None = None, facts: dict | None = None, usage=None, said: dict | None = None,
                deployed: dict | None = None) -> RunOutput:
        agent_result = (result or {}).get("agent_result") or {}
        metadata = agent_result.get("metadata") or {}
        hooks_active = metadata.get("hooks_active")          # None: nothing was deployed
        status = verdict["status"]
        ok = status == "ok"
        if self.cli is not None:
            # Hermit's reading of the session: its tokens and calls, priced at the backend's price (Harbor counts
            # nothing for an agent whose model it does not run)
            tokens = usage.input_tokens + usage.output_tokens if usage is not None else None
            cost = usage.cost_usd if usage is not None else None
            calls = usage.calls if usage is not None else 0
            cache_tokens = usage.cached_input_tokens if usage is not None else None
        else:
            n_in, n_out = agent_result.get("n_input_tokens"), agent_result.get("n_output_tokens")
            tokens = int(n_in) + int(n_out) if ok and n_in is not None and n_out is not None else None
            cost = agent_result.get("cost_usd")
            calls = curate_mod.model_calls(trajectory) if trajectory else 0
            cache_tokens = agent_result.get("n_cache_tokens")
        wall = (curate_mod.seconds((result or {}).get("agent_execution"))
                or curate_mod.seconds({"started_at": (result or {}).get("started_at"),
                                       "finished_at": (result or {}).get("finished_at")})
                or round(time.time() - started, 1))
        never = result is not None and not ((result.get("agent_execution") or {}).get("started_at"))
        if (verdict.get("ran") is False or never) and usage is None:
            # no container, no session (no_trial), or no agent started in it (Harbor records none: its environment
            # or the agent's setup failed first): nothing spent
            tokens, cost, calls = 0, 0.0, 0
        rewards = ((result or {}).get("verifier_result") or {}).get("rewards") if result else None
        output = {"harbor_task": task.get("harbor_task") or task.get("task") or task["id"],
                  "rollout": task.get("rollout"), "step": step, "reward": verdict["reward"], "rewards": rewards,
                  "status": status, "failure_cause": verdict["failure_cause"],
                  "exception_type": verdict["exception_type"], "trial": trial, "hooks_active": hooks_active}
        said = said or {}
        if said.get("reply") is not None:
            output["reply"] = said["reply"]
        if self.cli == "answer":
            output["answer"] = said.get("answer")
        more = {"status": status, "timed_out": verdict["timed_out"], "exception_type": verdict["exception_type"],
                "exception_message": verdict["exception_message"][:2000], "failure_cause": verdict["failure_cause"],
                "halt": verdict["halt"], "n_cache_tokens": cache_tokens, "verifier_reward_raw": verdict["reward"],
                "hooks_active": hooks_active, "agent_metadata": metadata, "judge": {"pass_reward": self.pass_reward}}
        if self.cli is not None:
            more["cli"] = self.cli
        if facts is not None:
            more["session"] = facts
        if deployed and deployed.get("context_tool") is not None:
            more["context_tool"] = deployed["context_tool"]
        if status == "infra":
            more["unscored"] = verdict["failure_cause"] or "infra"
        return RunOutput(output=output, log=log, cost_usd=cost, wall_s=wall, tokens=tokens,
                         api_speed={"model_calls": calls, "status": status}, extra={**more, **(extra or {})})
