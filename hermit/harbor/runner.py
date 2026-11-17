"""`agent.name: harbor`: one Hermit rollout as one Harbor trial, or one step of a Harbor multi-step task.

    agent:
      name: harbor
      concurrency: 8
      params:
        benchmark: ../utils/benchmark.yaml   # optional: these same keys, shared by every method's config (its
                                             # model a name, or a model a CLI reaches: a `<backend>/<model>`
                                             # reference goes in the run config, where hermit check sees it)
        tasks_dir: ../utils/tasks            # the Harbor task directories (dataset.py exports or indexes them)
        model: openai/gpt-5.6-luna           # a `<backend>/<model>` reference (the backend's endpoint and key), or
                                             # with `login` the CLI's model name; the answer agent's model a CLI
                                             # reaches ({cli: codex | claude | pi, model, login}, as `llm`'s) goes in
                                             # the benchmark file: here it is refused (CONFIG_CLI_MODEL)
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
        infra_retries: 2                     # a trial that ended infra (not the agent's result) runs again this many
                                             # times at most (a stream's step never)
        infra_wait_s: 300                    # ... each after this long
        agent_timeout_sec: 3600              # uniform agent budget; null: each task's own
        environment: {delete: true}          # Harbor's environment config (type, import_path, force_build, kwargs,
                                             # env, extra_docker_compose: overlays, relative to the benchmark file)
        verifier: {}                         # Harbor's verifier config (override_timeout_sec, ...)
        verifier_timeout: agent              # a verifier that ran out of its time: the agent's result, kept, as
                                             # Harbor grades it (its solution too slow for the tests, or a process it
                                             # left that hung them); infra: not the agent's result (a host whose load
                                             # makes verifiers time out), run again (infra_retries)
        verifier_timeout_multiplier: 1.0     # Harbor's: each task's verifier limit times this (a slow host)
        environment_build_timeout_multiplier: 1.0   # Harbor's: each task's environment start (pull, build) limit
                                             # times this; the agent's limit is never scaled
        retry: {max_retries: 0}              # Harbor's retry of a failed trial (not of a stream's step; none on
                                             # login: chatgpt, whose every attempt would start from the sign-in copied
                                             # before the first); a failed attempt's usage is not counted (Harbor
                                             # removes its trial): infra_retries runs each attempt as a trial of its own
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
Hermit reads a session's, by the CLI's Base Agent (`outcome`, `parse`: served, refused for a plan's limit, a provider
that could not serve or that refused its requests, a lost sign-in, a session that never started or whose tools never
came up; the usage, priced at the run's price: agent.params.price, else the backend's), and for Codex whether its
sign-in or key was refused (`signin.codex_signin_lost`). What the container wrote is read only from regular files,
never through a link it left there (which could name a file of this machine): the stream and the CLI's last message
are copied into the job's own `launch/read/` first, and the Base Agent reads those copies.
A step: the stream's driver (`stream.StreamClient`, `harborside/driver.py`) is started with the first step and keeps
the task's environment; each rollout stages its adapter, renders its plan, waits for its turn, asks for its step (with
its own allowed hosts) and reads the step's result; the last step (or an abort) stops the driver. A step whose turn
cannot come (its driver waits for a step no rollout of this runner is running: one that ended before it reached the
driver, or ran in an earlier training step; or it has no driver, and no rollout of its first step is running to start
one) ends at once, infra (stream_order), and so does every later step of its stream, whose driver the last of them
stops; a driver that answers no stop (hung in a step) is killed, and its trial's compose projects are taken down. A new
driver in a stream's directory finds the earlier one's trial set aside (`earlier/<n>/`). A stream is the row's rollout
of the task in this runner: `<jobs_dir>/streams/<task>__s<rollout>__<the runner's id>` (each runner its own id, so two
runs on one jobs directory, at once or one after the other, a resumed run among them, never share a driver, its
environment or its step logs). Its steps run in their order: a training split that runs one keeps `data.shuffle:
false`, so that the Trainer hands its rows in the split's order. When the runner is freed, or at exit for one still
held then (`_close_runner_streams`), the drivers of the streams that did not finish are stopped, one after another,
each killed if it has not stopped within CLOSE_STOP_S, which would otherwise hold their tasks' environments for
`stream.idle_s`, and every stream's tool server is closed: no stream is resumed.

What the rollout returns: `output` {harbor_task, rollout, step, reward, rewards, status, failure_cause,
exception_type, trial, hooks_active, reply (the CLI's last message), answer (the answer agent's)}, the usage, the
curated log (`curate.py`), and `extra` with the verdict, the session's facts, the job, what was deployed and its
per-task context (`context_tool`), `judge` (pass_reward) and, for a trial that is not the agent's result (infra:
the host, the provider, a disk gate, a session that never started), `unscored` (its cause). Such a trial runs again,
up to `infra_retries` times, each after `infra_wait_s` (not a stream's step, whose environment went on without it,
nor a task Harbor runs for none of Hermit's agents, a step whose turn cannot come, a disk gate that already waited
its time, a provider's refusal of the run's configuration, model_rejected, or a host failure, host_failed: the jobs
directory that cannot be made, or a failure past the trial's staging, whose session may have run); what every run
used is the rollout's (`extra.attempts`). One that ends
infra all the same is no verdict:
the judge (hermit.harbor.judge) raises on it, so the run records a failed run (status judge_error), metered and with
its log, which `hermit eval` scores 0 in its mean and counts among its errors; `python -m hermit.harbor.report` gives
an eval's pass@1 over the trials that are the agent's result beside it. A lost sign-in, or the usage limit of an API
account (no login waits it out), or a provider that does not bill the account (its quota or credit spent, a 402),
ends the run: the rollout raises hermit.agent.base.interface.LoginFailed, which
hermit.trainer.rollout passes up (no further rollout starts; `hermit train` resumes after its last finished step,
`hermit eval` stops), and so does every rollout of this runner that starts, or was waiting for the plan or a slot,
after it.

On a login (`login: chatgpt` or `claude`) a trial shares the plan with every Hermit session on the login: it waits
while the plan holds sessions back (its PAUSE, MAX_USED, a refusal's hold) and runs in one of its slots; what the
session says of the plan (Claude Code's stream; Codex's session logs, which the launch plan collects) is kept before
the slot is given back, and so is whether the plan refused the session: then every session is held until the window
resets and the trial runs again, so that a usage limit costs time, never a trial. A session the provider could not
serve runs again `busy_retries` times, a count over all of the trial's runs that infra_retries adds none to, unless it
ran out of the agent's time (its result, as Hermit's own sessions keep it). The answer agent's model reached
through a CLI on a login that the runner serves itself (`model: {cli, login}` in the benchmark file: each request one session on the login, in a sandbox of its own) waits for that plan before each
trial, and its relay waits for it before each request (`tools.ModelEndpoint`): a trial whose one request was still
unanswered when the agent's time ran out, held back by that plan, is infra (usage_limit), not the agent's timeout;
its sessions run with `plan_rerun: false`, so that one the plan refused comes back at once (the relay's 429: infra,
usage_limit, run again once the plan allows, not counted in infra_retries), never waiting for the window inside the
request. That wait does not
cover a slot, which the request's session takes itself (CliAgent.run): a request waiting for one
(the plan's SLOTS all taken) waits inside the agent's time, and its trial is the agent's timeout (its result) if that
time runs out; so keep agent.concurrency at most SLOTS less the login's other sessions. Such a model given in the run
config's own agent.params.model would be served by the pipeline (hermit.isolate.agent_pipeline, as for any acting
agent), whose session waits for the plan inside the request, which the runner could not tell from the model's own
time: the runner refuses it at startup (PIPELINE_CLI_SPEC; as written, as hermit check hands it the run config's
params), and the benchmark file is where it goes. Codex's
refreshed sign-in comes back from the container and becomes the shared copy (`signin.take_back`); a stream's step is
given the shared copy at its turn, never when its rollout began.
"""
from __future__ import annotations

import dataclasses
import glob
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

from hermit.agent.acting.agent import Agent
from hermit.agent.base import login as subscription
from hermit.agent.base.interface import LoginFailed
from hermit.harbor import clis
from hermit.harbor import curate as curate_mod
from hermit.harbor import dataset
from hermit.harbor import docker
from hermit.harbor import launch
from hermit.harbor import signin
from hermit.harbor import stream as stream_mod
from hermit.harbor.errors import adapter_failed, classify, no_trial, verifier_disk_full, verifier_unreached
from hermit.harbor.staging import stage
from hermit.harbor.stream import (DRIVER_LOG, StepAlreadyRunError, StreamClient, StreamError, StreamOrderError,
                                  TurnLostError)
from hermit.harbor.tools import UNSUPPORTED
from hermit.task import RunOutput

HARBOR = "~/.cache/hermit/harbor-venv/bin/harbor"
JOBS_DIR = "~/.cache/hermit/harbor/jobs"
HARBORSIDE_ROOT = Path(__file__).resolve().parent                # the directory holding the harborside package
SETUP_BUDGET_S = 360                                             # Harbor's agent setup: apt + nvm + npm + python3
TEARDOWN_BUDGET_S = 600                                          # image pull, container start, compose down
DEFAULT_AGENT_TIMEOUT_S = 3600
TIMEOUT_MULTIPLIERS = ("verifier_timeout_multiplier", "environment_build_timeout_multiplier")   # into Harbor's job
DISK_GATE_GB = 20.0
DISK_GATE_WAIT_S = 7200.0
TMP_GATE_GB = 1.0
HOST_TMP = Path("/tmp")
INFRA_RETRIES = 2                                                # infra_retries' default
INFRA_WAIT_S = 300.0                                             # infra_wait_s'
# the causes of an infra trial that run no better again: a disk gate that waited its time out, a request the provider
# refused for the run's configuration (its key, permission, model or bill: no input to the session changes it). A task
# Harbor runs for none of Hermit's agents (unsupported_task) and a stream's step never run again: neither reaches
# _run_trial
NO_RETRY = ("disk", "model_rejected")
# a variable an acting_agent.env value names: a whole value ${NAME} or ${NAME:-default}, which Harbor fills in from
# this process's environment (harbor.utils.env.resolve_env_vars), and any other reference, $NAME or one inside a longer
# value, which it hands on as that text
ENV_REF = re.compile(r"\$(?:\{(\w+)(?::-[^}]*)?\}|(\w+))")
# a variable named as a secret, as Harbor names one (harbor.utils.env._SENSITIVE_KEY_RE): OPENAI_API_KEY, GH_TOKEN,
# HF_TOKEN_READ, AWS_SECRET_ACCESS_KEY, ...; never a count (MAX_OUTPUT_TOKENS)
HARBOR_SECRET = launch.HARBOR_SECRET
NESTED = ("acting_agent", "environment", "verifier", "retry", "stream", "answer")   # params merged one level deep
BINS = ("codex", "claude", "pi", "opencode", "dsh")              # the CLIs a binary of this machine may be mounted for
# What has Harbor's own codex agent sign in with the user's ChatGPT file (~/.codex/auth.json, or the file named)
CODEX_SIGN_IN = ("CODEX_FORCE_AUTH_JSON", "CODEX_AUTH_JSON_PATH")
TOOLS_PLACEHOLDER = "http://0.0.0.0:0/" + "0" * 32 + "/mcp"      # a dry render's tools URL (the startup check's)
ANSWER_PLACEHOLDER = "http://127.0.0.1:0/" + "0" * 32            # ... and the answer agent's endpoint
# The keys of the spec hermit.agent.base.endpoint.endpoint_spec gives a pipeline for a model only a CLI reaches, which an
# endpoint the pipeline started serves (hermit.isolate.agent_pipeline: a run config's own `agent.params.model: {cli:
# ...}`); a run config's model, once the config is loaded, is a Binding's spec, which has more (its ref, backend, apis)
PIPELINE_CLI_SPEC = frozenset({"base_url", "model", "api_key_env", "key_required", "price", "timeout"})
CONFIG_CLI_MODEL = ("the answer agent's model is one a CLI reaches that the run config gives (agent.params.model: "
                    "{cli: ...}), which the pipeline serves: its session waits for its login's plan inside the "
                    "request, which the runner cannot tell from the model's own time, so a trial whose time ran out "
                    "as it waited would be scored as the agent's. Give it in the benchmark file (agent.params."
                    "benchmark, its model: {cli: ...}), whose model the runner serves itself, waiting for the plan "
                    "apart")
SESSION_FACTS = ("served", "refused", "unavailable", "login_failed", "rejected", "model_refusal", "held_until",
                 "served_model", "plan_report", "started", "tools_down", "finish_reason", "calls", "statuses")
# A rejection that refused the run's configuration, which no input to the session changes, as the CLI's own notices
# (the session's `failure`) name it: its key or token (401, unauthorized, a failed authentication, an invalid key or
# token, a key not valid, not logged in, OpenCode's provider.auth), its permission (403, forbidden, no access), its
# model or endpoint (404, not found, a model that does not or may not exist, an invalid or unknown model, one that is
# not a valid model), its account's bill (402, a credit balance too low, an insufficient quota, Codex's "Quota exceeded.
# Check your plan and billing details.", billing; not a rate limit's advice, "billing details" or a link to the billing
# page). A status is read only where one is written: leading
# the text or a line of it (Pi's "401 {...}"), after the prefix Hermit's endpoint gives a CLI's failure in its reply
# ("...(exit 1): 401 {...}", "...(timed out): "; endpoint._no_reply), after "API Error: " (Claude Code's) or
# "Error code: " (OpenAI's SDK), in "(HTTP 401)" (OpenCode's) or in "API error (401)" (Pi's), after "status" ("status
# code", "status_code") or a JSON "code"; never another number of the text (a token count: "you requested 4402 tokens
# (402 in the messages, ...)", "input tokens (402): 403 over"). So is a request setting the model does not take (an
# unsupported parameter or value: max_tokens where it takes max_completion_tokens, an effort it has not; Azure's
# unrecognized request argument; OpenCode's provider.unsupported-operation), and what Hermit's own endpoint refuses a
# CLI session cannot take (its code, tools.UNSUPPORTED: config_refused). Any other rejection (a
# prompt too long, an invalid request, a content filter) is what the adapter's or the task's input made the request:
# the agent's result. (A rate limit or an overload is `unavailable`, read before any rejection.)
CONFIG_REFUSED = re.compile(r"(?:^|\((?:exit -?\d+|timed out)\): |\bAPI error(?:: | \()|\bError code: |"
                            r"(?:\bstatus(?:[ _]code)?|\"code\")\"?:? ?\"?)40[1-4]\b|\(HTTP 40[1-4]\)|"
                            r"unauthori[sz]ed|forbidden|authenticat|\bauth\b|not logged in|"
                            r"invalid[\w -]{0,20}?(?:key|token)\b|\b(?:key|token) not valid|permission|access denied|"
                            r"not have access|not[ _]found|(?:may|does|model) not exist|(?:invalid|unknown)[ _]model|"
                            r"not a valid model|credit balance is too low|insufficient[_ ]?quota|"
                            r"^\s*Quota exceeded\. Check your plan and billing details\b|"
                            r"(?<!/)\bbilling(?! details)|unsupported[ _](?:parameter|value)|"
                            r"unrecognized request argument|unsupported[ _-]operation", re.I | re.M)
CONFIG_STATUSES = (401, 402, 403, 404)                           # ... the answer agent's: its model endpoint's status
# ... of them, an account the provider does not bill (its quota or its credit spent, a payment it needs): nothing later
# can be served, as after an API account's usage limit, so it ends the run (usage_limit, a halt)
BILLING_REFUSED = re.compile(r"(?:^|\((?:exit -?\d+|timed out)\): |\bAPI error(?:: | \()|\bError code: |"
                             r"(?:\bstatus(?:[ _]code)?|\"code\")\"?:? ?\"?)402\b|\(HTTP 402\)|payment required|"
                             r"credit balance is too low|insufficient[_ ]?(?:quota|balance|credits?|funds)\b|"
                             r"^\s*Quota exceeded\. Check your plan and billing details\b|(?<!/)\bbilling(?! details)",
                             re.I | re.M)
CLOSE_STOP_S = 120.0                     # a driver's stop when its runner is closed or freed: then it is killed
STAGING_FREE_MB = 64                     # a staging child failed beside less free: the host's, not the adapter's


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


def _close_runner_streams(drivers: dict, streams: dict, lock) -> None:
    """What a runner's streams still hold, closed (HarborAgent.close): the driver still running of each stream that did
    not finish is stopped (no later runner meets it again: each runner names its own streams, and each would hold its
    task's environment for stream.idle_s; a stream that finished was stopped by its last step), and every stream's tool
    server is closed. It is the runner's finalizer too (weakref.finalize): it runs when the runner is freed, and at exit
    for a runner still held then, since main's hermit.isolate.agent_pipeline hands the runner over without a close of
    its own (`hermit check`'s probe, `hermit eval`, `hermit train`, a run a lost sign-in stopped). It holds the runner's
    state, never the runner."""
    while True:
        with lock:
            if not drivers:
                break
            _, client = drivers.popitem()
        try:
            if client.running() is not None:
                client.stop(timeout_s=CLOSE_STOP_S)    # one after another, at exit too: a hung one is killed sooner
        except Exception:                                    # noqa: BLE001 - best effort, as at exit
            pass
    while True:
        with lock:
            if not streams:
                break
            _, served = streams.popitem()
        try:
            served.close()
        except Exception:                                    # noqa: BLE001 - best effort, as at exit
            pass


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


def billing_refused(facts: dict) -> bool:
    """Whether a refusal of the run's configuration (config_refused) is the provider's refusal to bill the account
    (BILLING_REFUSED): the answer agent's model endpoint's last status 402, or the message."""
    statuses = facts.get("statuses") or []
    return (bool(statuses) and statuses[-1] == 402) or bool(BILLING_REFUSED.search(str(facts.get("failure") or "")))


_SANDBOX_STARTED: list[bool] = []        # a strict adapter sandbox started in this process (a runner's startup)


def sandbox_fault(enforce: str | None, *, fresh: bool = True) -> str | None:
    """Why the strict adapter sandbox that renders each adapter does not start on this machine (bubblewrap that cannot
    make its namespaces, say), seen from this process: a child of it running this interpreter (the deployment's own
    starts it as its child does, for its own interpreter); None when it starts, or when it is not required (enforce
    best_effort, or HA_ADAPTER_SANDBOX_ENFORCE). `fresh` false (a runner's startup): once it started in this process,
    it is not tried again."""
    from hermit.adapter.session import ENV_ENFORCE
    from hermit.sandbox import run_in_strict_sandbox
    if (enforce or os.environ.get(ENV_ENFORCE, "required")) != "required" or not shutil.which("bwrap"):
        return None
    if not fresh and _SANDBOX_STARTED:
        return None
    try:
        run = run_in_strict_sandbox([sys.executable, "-c", "pass"], role="render", readable=[], enforce="required",
                                    timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"the strict adapter sandbox did not start: {type(exc).__name__}: {exc}"
    if run.returncode != 0:
        return (f"the strict adapter sandbox did not start (exit {run.returncode}): "
                f"{(run.stderr or run.stdout or '').strip()[-300:]}")
    _SANDBOX_STARTED.append(True)
    return None


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


class _TurnUnready(Exception):
    """A stream step's turn came and this machine could not ready it (the sign-in's copy, the adapter's tools): `cause`
    is the step's verdict's."""

    def __init__(self, cause: str, message: str):
        self.cause = cause
        super().__init__(message)


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
            if "model" not in given:
                self.check_shared_model(shared.get("model"), given["benchmark"])
            self.params = _merge(shared, {k: v for k, v in given.items() if k != "benchmark"})
            self.benchmark_base = self.path(given["benchmark"]).parent
        else:
            self.benchmark_base = self.base
        if not self.params.get("tasks_dir"):
            raise ValueError("agent.params.tasks_dir is required (the Harbor task directories)")
        if "trial_cache" in self.params:
            raise ValueError("agent.params.trial_cache: there is no trial cache any more (no trial is taken from an "
                             "earlier run's); remove the key, from the benchmark file too: a resumed `hermit train` "
                             "runs its unfinished step's trials again, and an eval run again runs all of its own")
        self.infra_retries                                   # and an infra retry count or wait that is no number
        self.infra_wait_s
        self.docker_cleanup                                  # an unknown mode fails here, before any container
        self.verifier_timeout                                # and an unknown verifier_timeout
        self.acting_agent                                    # and an agent spec without a name
        if self.cli == "answer" and isinstance(given.get("model"), dict) and given["model"].get("cli"):
            # the run config's own model a CLI reaches, as hermit check hands it the config's params (hermit eval and
            # train hand it the endpoint the pipeline serves that model from: check_agent, PIPELINE_CLI_SPEC)
            raise ValueError(CONFIG_CLI_MODEL)
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
        self._stop = threading.Event()                       # set once a rollout met a lost sign-in (end_run)
        self._stop_why = ""                                  # ... and why
        self.run_id = uuid.uuid4().hex[:8]                   # this runner's own: its streams' names (_run_step)
        # its streams closed when it is freed, or at exit (_close_runner_streams)
        self._finalizer = weakref.finalize(self, _close_runner_streams, self._drivers, self._streams,
                                           self._streams_lock)
        self.check_agent()

    # --- params ---------------------------------------------------------------------------------------------
    @staticmethod
    def check_shared_model(model, where) -> None:
        """A model the benchmark file gives (the run config giving none) is a name (the CLI's own, with a login; one of
        Harbor's own agents') or a model a CLI reaches (`{cli: ...}`, which the runner serves itself). A
        `<backend>/<model>` reference or another model mapping is refused: it belongs in the run config's own
        agent.params.model, which the config's loading resolves and hermit check and hermit train check (its backend,
        its key), and which they never read in the benchmark file."""
        if model is None or (isinstance(model, dict) and model.get("cli")):
            return
        if isinstance(model, str) and launch.binding_of({"model": model}) is None:
            return
        raise ValueError(f"agent.params.benchmark {where} gives model {model!r}, a <backend>/<model> reference or a "
                         "model mapping: give it in the run config's own agent.params.model (where the config's "
                         "loading binds it and hermit check sees its backend and key); the benchmark file names a "
                         "model by name, or one a CLI reaches (model: {cli: ...})")

    @property
    def infra_retries(self) -> int:
        """How many times a trial that ended infra runs again (`infra_retries`, INFRA_RETRIES)."""
        value = self.params.get("infra_retries")
        try:
            value = INFRA_RETRIES if value is None else int(value)
        except (TypeError, ValueError):
            value = -1
        if value < 0:
            raise ValueError(f"agent.params.infra_retries is a count, at least 0, not {self.params.get('infra_retries')!r}")
        return value

    @property
    def infra_wait_s(self) -> float:
        """How long before each run again (`infra_wait_s`, INFRA_WAIT_S)."""
        value = self.params.get("infra_wait_s")
        try:
            value = INFRA_WAIT_S if value is None else float(value)
        except (TypeError, ValueError):
            value = -1.0
        if not value >= 0:
            raise ValueError(f"agent.params.infra_wait_s is a number of seconds, at least 0, not "
                             f"{self.params.get('infra_wait_s')!r}")
        return value

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
        reaches; priced at `price` when the params give one (an explicit price, which bind_agent_params keeps beside
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
        client settings the model's spec gives beside it (`timeout`, `max_retries`, which the relay's client would
        otherwise take as its own 600 s and retries)."""
        if self.cli_model is not None:
            return self.cli_model
        if self.binding is None:
            raise ValueError("the answer agent needs agent.params.model: a <backend>/<model> reference, or a model a "
                             "CLI reaches (model: {cli: codex | claude | pi, model: ..., login: ...})")
        spec, bound, model = self.binding.spec(), self.params.get("binding"), self.params.get("model")
        # the spec the binding was made from (launch.binding_of): the bound one, else the model's own mapping
        given = bound if isinstance(bound, dict) and bound.get("base_url") and bound.get("model") else model
        if isinstance(given, dict):
            spec.update({k: given[k] for k in ("timeout", "max_retries") if given.get(k) is not None})
        return spec

    def model_plan(self) -> tuple[Path, bool] | None:
        """(the plan, whether its sessions may draw usage credits) of the login the answer agent's model runs on when
        the runner serves that model itself (`model: {cli, login, plan_dir, usage_credits}`, the benchmark file's):
        each request one session on the login, which the relay holds back while the plan holds sessions back
        (tools.ModelEndpoint); None for any other agent or model, and for such a model the pipeline serves (the run
        config's own: hermit.isolate.agent_pipeline), whose CLI and login the runner never learns."""
        model = self.cli_model if self.cli == "answer" else None
        if not model or not model.get("login"):
            return None
        return (subscription.plan_dir(model.get("plan_dir"), str(model["login"])),
                subscription.opted_in(model.get("usage_credits")))

    def plan_login(self) -> str | None:
        """The login whose plan the trial's sessions draw on: the acting CLI's (`login`), or the answer agent's model's
        when the runner serves it (model_plan); None for none, or for a login the runner does not know (that of a
        model the pipeline serves)."""
        if self.cli == "answer":
            return str(self.cli_model["login"]) if self.model_plan() is not None else None
        return self.login

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

    def instruction(self, task: dict) -> str:
        """The task's (or the step's) instruction, as Harbor gives it to the agent: the row's (dataset.index), else its
        instruction.md (a step's under steps/<name>/)."""
        if task.get("instruction"):
            return str(task["instruction"])
        rel = Path("steps") / str(task["step"]) / "instruction.md" if task.get("step") else Path("instruction.md")
        return _text(self.task_dir(task) / rel).strip()

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
        CLI's own login: the Codex sign-in, Claude's token, a mounted claude_bin's version), web search set, a model
        this machine's containers can reach, no Harbor retry on login: chatgpt, the settings its Base Agent renders (a
        dry render of the launch plan), and for mini-swe-agent the package its adapter's deployment reads on this
        machine; bubblewrap when the adapter sandbox that renders each adapter (`sandbox_enforce`) is required; for the
        answer agent its model's key, no attachments on a model a CLI reaches that the runner serves (`model:
        {cli: ...}` in the benchmark file), whose sessions take text only, and no model a CLI reaches that the pipeline
        serves (the run config's own `model: {cli: ...}`: as the pipeline's endpoint spec, PIPELINE_CLI_SPEC, and as
        written, which __init__ refuses, as hermit check hands it the config's params). The runner never learns
        whether the run deploys an adapter, so a run that deploys none (hermit eval --adapter none) is checked for
        bubblewrap and
        mini-swe-agent's package too. A task Harbor runs for none of Hermit's agents is not refused here, as the run
        may never take it: its rollout is infra (unsupported). For every agent, no secret in acting_agent.env
        (check_env)."""
        if self.cli is None:
            self.builtin_kwargs()
            self.check_builtin_sign_in()                 # before check_env: its CLAUDE_FORCE_OAUTH says what to do
            self.check_env()
            return
        self.check_env()
        cli, login, binding = self.cli, self.login, self.binding
        model = self.params.get("model")
        if not isinstance(self.acting_agent.get("settings") or {}, dict):
            raise ValueError("agent.params.acting_agent.settings is a mapping of the CLI Base Agent's settings")
        if not shutil.which("bwrap"):
            from hermit.adapter.session import ENV_ENFORCE
            if (self.params.get("sandbox_enforce") or os.environ.get(ENV_ENFORCE, "required")) == "required":
                # staging renders every adapter (its exports, its per-task context) in it: each trial would fail
                raise ValueError("a trial deploys its adapter by rendering it in the strict adapter sandbox, whose "
                                 "enforce is required (agent.params.sandbox_enforce), and bubblewrap (bwrap) is not "
                                 "installed: install it, or set agent.params.sandbox_enforce: best_effort (unconfined, "
                                 "recorded), which a run that deploys no adapter (hermit eval --adapter none) needs "
                                 "too: the runner cannot tell it from one that does")
        elif (fault := sandbox_fault(self.params.get("sandbox_enforce"), fresh=False)) is not None:
            raise ValueError(f"a trial deploys its adapter by rendering it in the strict adapter sandbox, and {fault}: "
                             "every trial's staging would fail; let bubblewrap make its namespaces (user namespaces "
                             "on), or set agent.params.sandbox_enforce: best_effort (unconfined, recorded)")
        if cli == "answer":
            if login:
                raise ValueError("the answer agent takes no login: its model is a <backend>/<model> reference, or "
                                 "one a CLI reaches (model: {cli: ..., login: ...})")
            if isinstance(model, dict) and set(model) == PIPELINE_CLI_SPEC and model.get("api_key_env") is None \
                    and model.get("key_required") is False:
                # its session waits for its login's plan inside the request (CliAgent.run), where the runner cannot
                # tell that wait from the model's own time: a trial whose time ran out as it waited would be the
                # agent's timeout, scored 0, though it is the plan's
                raise ValueError(CONFIG_CLI_MODEL)
            spec = self.answer_model()
            reached = (self.cli_model or {}).get("cli")
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
                if spec.get("login") and not problems:
                    # its sign-in, as the acting CLI's is checked (a session on this machine: its CLI too, a session
                    # in a container of its own the image's), so that a run with none ends at startup, not with
                    # every trial unserved, run again and then a failed run
                    problems = subscription.problems_of(str(spec["login"]), program=spec.get("bin"),
                                                        contained=spec.get("isolation") == "container",
                                                        root=spec.get("login_root"))
                if problems:
                    raise ValueError("; ".join(problems))
            elif binding.key_env_if_set() and not os.environ.get(binding.key_env_if_set()):
                raise ValueError(f"{binding.key_env_if_set()} is not set: the answer agent's model endpoint on this "
                                 "machine sends each request on with the key it reads from this process's environment")
            self._dry_render(None, tools=False)
            return
        if self.cli_model is not None:
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
                                 f"cannot reach: serve it on an address the container reaches (<NAME>_BASE_URL); a "
                                 f"model a CLI reaches (model: {{cli: ...}}, which a run config's loading serves on "
                                 f"the loopback) is the answer agent's alone")
        if login:
            # a mounted claude_bin is this machine's Claude Code, checked here as that CLI (problems_of runs it for its
            # version); without one a Claude session runs the container's install (contained: only the token is checked)
            mounted = self.bin("claude") if login == "claude" else None
            problems = subscription.problems_of(login, program=str(mounted) if mounted else None,
                                                contained=mounted is None, root=self.login_root)
            if problems:
                raise ValueError("; ".join(problems))
            retry = self.params.get("retry") or {}
            if login == "chatgpt" and isinstance(retry, dict) and int(retry.get("max_retries") or 0) > 0:
                raise ValueError("agent.params.retry.max_retries > 0 with login: chatgpt: each of Harbor's attempts "
                                 "of a trial would start from the sign-in copied before the first, which Codex may "
                                 "have refreshed (and so spent) in an earlier attempt; set retry.max_retries: 0 and "
                                 "let infra_retries run a trial that is not the agent's result again, each run with "
                                 "the shared copy as it is then")
        if own == "dsh":
            from hermit.agent.base.cli import dsh_problems
            problems = dsh_problems(self.settings(), "agent")
            if problems:
                raise ValueError("; ".join(problems))
        binary = self.bin(cli)
        if binary is not None and not (binary.is_file() and os.access(binary, os.X_OK)):
            raise ValueError(f"agent.params.{cli}_bin {binary} is not an executable file")
        if cli == "mini-swe-agent" and not self._finds("minisweagent"):
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

    def _dry_render(self, task: dict | None, *, tools: bool, check: bool = False) -> None:
        """The launch plan a trial of `task` renders, without its deployment, sign-in or tools' server, rendered in a
        scratch job that is removed after: the settings the CLI's Base Agent refuses are refused here, at startup.
        With `check`, what harborside would refuse in every trial is refused here too (check_secrets)."""
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
        variable named as a secret, or a value that names one, is refused, a secret's name being one Harbor names so
        (HARBOR_SECRET, harbor.utils.env's: OPENAI_API_KEY, GH_TOKEN, HF_TOKEN_READ, GIT_AUTHOR_NAME too) or one of
        launch.SECRET_VAR's (DB_PASSWD): a value that is `${NAME}` or `${NAME:-default}` alone, which Harbor fills in
        from this process's environment, and any other reference (`$NAME`, or one inside a longer value), which it
        hands on as that text (never the secret the config meant). A table CLI's secret is passed by name (the CLI's
        settings.env, NAME: $NAME), and Harbor's own agents read theirs from this process's environment."""
        def secret(n: str) -> bool:
            return bool(HARBOR_SECRET.search(n) or launch.SECRET_VAR.search(n))
        for name, value in (self.acting_agent.get("env") or {}).items():
            named = [a or b for a, b in ENV_REF.findall(str(value))]
            found = str(name) if secret(str(name)) else next((n for n in named if secret(n)), None)
            if found:
                raise ValueError(f"agent.params.acting_agent.env {name} holds a secret ({found}, a name Harbor treats "
                                 "as one): its values are written into each trial's job and onto `docker compose "
                                 "exec`'s command line (Harbor fills a whole ${NAME} or ${NAME:-default} in, and hands "
                                 "any other reference on as that text); pass a table CLI's by name in its "
                                 "settings.env (NAME: $NAME), and export one of Harbor's own agents in this process's "
                                 "environment")

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

    # --- the run's end and the disk gate ---------------------------------------------------------------------
    def end_run(self, verdict: dict, job: Path):
        """A halting verdict (a lost sign-in, an API account's usage limit: nothing later in the run can succeed) ends
        the run: every rollout of this runner waiting for the plan, a slot, the disk or a run again stops, and none
        starts (stopped), and this one raises LoginFailed (hermit.agent.base.interface), which hermit.trainer.rollout
        passes up, as a lost sign-in of a Hermit session ends the run: `hermit train` resumes after its last finished
        step (the trials of the step it ended in run again), `hermit eval` stops. The trial's job and curated log stay
        under the jobs directory."""
        cause = verdict["failure_cause"] or verdict["exception_type"]
        named = f"{cause} ({verdict['exception_type']})" if verdict["exception_type"] not in (None, cause) else cause
        why = (f"a Harbor trial ({Path(job).name}): {named}: {str(verdict['exception_message'] or '')[:1500]}; nothing "
               "later in the run can succeed: restore the sign-in or the account's limit, then resume")
        with self._streams_lock:
            if not self._stop.is_set():
                self._stop_why = why
                self._stop.set()
        print(f"WARNING: {why} (the trial's job: {job})", file=sys.stderr, flush=True)
        raise LoginFailed(why)

    def stopped(self) -> None:
        """Raise LoginFailed when another rollout of this runner ended the run (end_run)."""
        if self._stop.is_set():
            raise LoginFailed(self._stop_why)

    def wait_for_disk(self, task: dict | None = None, admitted: list | None = None) -> bool:
        """The disk gate; for a task whose prebuilt image is not here yet and whose size `image_sizes` knows (a JSON
        file: compressed bytes per image reference), room for the image too, with the room other trials of this
        process are still to pull counted as taken (`admitted` gets the key of the room this trial holds)."""
        gate = self.params.get("disk_gate_gb")
        gate = float(DISK_GATE_GB if gate is None else gate)
        tmp_gate = self.params.get("tmp_gate_gb")
        tmp_gate = float(TMP_GATE_GB if tmp_gate is None else tmp_gate)
        wait_s = float(self.params.get("disk_gate_wait_s") or DISK_GATE_WAIT_S)
        self.stopped()
        if not docker.wait_for_disk(HOST_TMP, tmp_gate, wait_s, cancel=self._stop):   # the host's /tmp, before any room
            self.stopped()                                                           # is held
            return False
        image = docker.task_image(self.task_dir(task)) if task is not None else None
        free = docker.wait_for_disk(self._docker_root(), gate, wait_s, image=image,
                                    image_gb=self.image_gb(image) if image else 0.0, pulls=self.pulls,
                                    admitted=admitted, cancel=self._stop)
        self.stopped()                                       # another rollout ended the run while this one waited
        return free

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
        self.stopped()                                       # another rollout lost the sign-in: no container
        try:
            self.jobs_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:                               # this machine's, before anything: no session ran
            return self._output(task, no_trial("host_failed", f"{type(exc).__name__}: {exc}"[-2000:]), None, None,
                                started)
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
            # read): the host's failure, not the agent's result; a session may have run, so its cost is unknown. A run
            # another rollout ended meanwhile ends here too
            self.stopped()
            failed = f"{type(exc).__name__}: {exc}"
            return self._output(task, {**no_trial("host_failed", failed[-2000:]), "ran": None}, None, None, started)

    def _run_trial(self, task: dict, adapter: Path | None, started: float) -> RunOutput:
        """A trial, run again while it ends infra (not the agent's result: `unscored`), up to infra_retries times,
        each after infra_wait_s, unless its cause runs no better again (NO_RETRY) or has a rule of its own: on a login,
        a provider that could not serve the session runs again within busy_retries, one count over all of the trial's
        runs (_login_trial), and not again here once that is spent; the plan of the answer agent's model that refused
        its request (usage_limit) has the trial run again once that plan allows, as a login's own refusal does, not
        counted. What every run used is the trial's (_attempts). One that ends infra all the same is returned as such:
        the judge then refuses to score it (hermit.harbor.judge), so that the run records it as a failed run
        (judge_error), never as the agent's 0."""
        say = lambda m: print(f"hermit harbor {task['id']}: {m}", file=sys.stderr, flush=True)
        retries, pause, runs, again = self.infra_retries, self.infra_wait_s, [], 0
        busy: dict = {"n": 0}                                # the login's busy_retries, spent over all of the runs
        on_login = bool(self.login and self.cli is not None)
        while True:
            if on_login:
                out = self._login_trial(task, adapter, started, busy)
            else:
                self._model_plan_wait(task)
                out = self._trial_once(task, adapter, started)
            runs.append(out)
            cause = out.extra.get("unscored")
            if cause is None or cause in NO_RETRY or busy.pop("spent", False):
                return self._attempts(runs)
            plan = None if on_login else self.model_plan()
            if cause == "usage_limit" and plan is not None:
                # the session's refusal held that plan (CliAgent.run, or a wait of its own when the hold cannot be
                # written), which _model_plan_wait waits for
                say("the plan of the answer agent's model refused its request (its usage limit): the trial runs again "
                    "once that plan allows")
                continue
            again += 1
            if again > retries:
                return self._attempts(runs)
            say(f"not the agent's result ({cause}: {str(out.extra.get('exception_message') or '')[:300]}); again in "
                f"{pause:.0f} s ({again}/{retries})")
            self._pause(pause)

    def _pause(self, seconds: float) -> None:
        """The pause before a trial runs again; LoginFailed as soon as another rollout ends the run meanwhile."""
        if self._stop.wait(seconds):
            self.stopped()

    def _model_plan_wait(self, task: dict) -> None:
        """Before a trial of the answer agent whose model the runner serves on a login (model_plan): the wait while that
        plan holds sessions back, which the trial's one request would otherwise wait out inside the agent's time (its
        relay waits again before each request: tools.ModelEndpoint); LoginFailed when another rollout ended the run
        meanwhile. A slot is not waited for here: the request's session takes one itself (CliAgent.run), so a request
        waiting for one (the plan's SLOTS all taken) waits inside the agent's time, and its trial is the agent's timeout
        (its result) if that time runs out; keep agent.concurrency at most SLOTS less the login's other sessions."""
        plan = self.model_plan()
        if plan is None:
            return
        say = lambda m: print(f"hermit harbor {task['id']}: {m}", file=sys.stderr, flush=True)
        subscription.wait(plan[0], self._stop, say, None, plan[1])
        self.stopped()

    @staticmethod
    def _attempts(runs: list[RunOutput]) -> RunOutput:
        """A trial's output when it ran more than once (_run_trial, _login_trial): its last run's, carrying what every
        run used (one the plan refused, the provider could not serve or that ended infra may have spent tokens before it
        ended): the tokens the runs reported (unknown only when none did), their model calls (a run that reported none
        adds 0) and their cost (unknown when any run's is), and each run's own in `extra.attempts` (its usage alone, and
        its infra cause, `unscored`, None for a run that is the agent's result; a run that itself ran more than once
        gives each of its own: a run's verdict and job are the trial's own, shown to the learner as the trial is). One
        run alone is returned as it is, with no `extra.attempts`."""
        last = runs[-1]
        if len(runs) == 1:
            return last
        tokens = [r.tokens for r in runs if r.tokens is not None]
        costs = [r.cost_usd for r in runs]
        calls = sum(int((r.api_speed or {}).get("model_calls") or 0) for r in runs)
        each = [a for r in runs for a in (r.extra.get("attempts") or
                                          [{"tokens": r.tokens, "cost_usd": r.cost_usd,
                                            "model_calls": (r.api_speed or {}).get("model_calls"),
                                            "unscored": r.extra.get("unscored")}])]
        return dataclasses.replace(last, tokens=sum(tokens) if tokens else None,
                                   cost_usd=None if None in costs else sum(costs),
                                   api_speed={**(last.api_speed or {}), "model_calls": calls},
                                   extra={**last.extra, "attempts": each})

    def _login_trial(self, task: dict, adapter: Path | None, started: float, busy: dict | None = None) -> RunOutput:
        """A trial on a login (ChatGPT's, Claude's): after the plan's holds, in one of its slots; again after the window
        when the plan refused the session, again after a pause when the provider could not serve it, busy_retries
        times at most over the trial (`busy`: the count, shared by _run_trial's runs; "spent" set when the last run is
        one that count left as it was), not when it ran out of the agent's time (that is its result), never a lost
        sign-in, which a provider's notice beside it does not make busy; never once another rollout ended the run while
        it waited (LoginFailed). What every run used is the trial's (_attempts)."""
        plan = subscription.plan_dir(self.params.get("plan_dir"), self.login)
        credits = subscription.opted_in(self.params.get("usage_credits"))
        retries, pause = self.params.get("busy_retries"), self.params.get("busy_wait_s")
        retries, pause = int(3 if retries is None else retries), float(120 if pause is None else pause)
        say = lambda m: print(f"hermit harbor {task['id']}: {m}", file=sys.stderr, flush=True)
        busy = {"n": 0} if busy is None else busy
        runs = []
        while True:
            subscription.wait(plan, self._stop, say, None, credits)    # as long as the plan holds sessions back
            self.stopped()                                 # another rollout ended the run while this one waited
            with subscription.slot(plan, self._stop, say) as got:
                self.stopped()
                if got is not True or subscription.held(plan, credits):
                    continue
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
                    # a hold that cannot be written makes this trial wait five minutes itself (in its slot, so that
                    # no session asks again at once), which the run's end cuts short
                    subscription.hold(plan, until=facts.get("held_until") or None, cancel=self._stop)
            if refused:
                say("the plan refused the session (its usage limit): the trial runs again once the plan allows")
                continue
            ran_out = out.extra.get("exception_type") == "AgentTimeoutError"    # the agent's own time: its result
            if facts.get("unavailable") and not facts.get("served") and not ran_out and not facts.get("login_failed"):
                if busy["n"] < retries:
                    busy["n"] += 1
                    say(f"the provider could not serve the session; again in {pause * busy['n']:.0f} s "
                        f"({busy['n']}/{retries})")
                    self._pause(pause * busy["n"])
                    continue
                busy["spent"] = True                       # busy_retries is its budget: infra_retries adds none
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
        from the task's row). It runs
        on this machine before any container: an OSError there (a full disk, a folder it cannot write) is the host's,
        and the trial or the step that met it is infra (staging_failed). A RuntimeError is the adapter's: its
        deployment or its per-task context failed in their child process, the agent's result (errors.adapter_failed:
        agent_error, scored 0, its message the feedback), which its caller returns; what such a child prints cannot
        tell the host's failure from the adapter's, which could print one."""
        if self.cli is None:
            return None
        from hermit.harbor.launch import context_limit
        return stage(adapter, where, self.python, self.params.get("sandbox_enforce"), self.cli,
                     task=dict(task),
                     context_query=self.params.get("context_query"),
                     context_limit=context_limit(self.cli, self.settings(task)) if self.cli == "codex" else None)

    def _staging_verdict(self, exc: Exception, where: Path) -> dict:
        """The verdict of a staging whose child failed (_stage's RuntimeError) under `where`: the adapter's
        (adapter_failed: scored 0, its message the feedback), unless this process sees this machine fail any adapter's
        staging there (_host_fault): then the host's (staging_failed, infra). What the child printed is not taken for
        either: the adapter could print one."""
        fault = self._host_fault(where)
        if fault is not None:
            return no_trial("staging_failed", f"{fault} (the staging's child: {str(exc)[-1500:]})"[-2000:])
        return adapter_failed(str(exc)[-2000:])

    def _host_fault(self, where: Path) -> str | None:
        """What this process sees on this machine that fails a staging child whatever the adapter: a folder it cannot
        write, less than STAGING_FREE_MB free under it, the strict adapter sandbox not starting (sandbox_fault); None
        when none of them is so. A child killed or run out of its time is the adapter's all the same."""
        try:
            if not os.access(where, os.W_OK | os.X_OK):
                return f"{where} cannot be written by this process"
            free = shutil.disk_usage(where).free
        except OSError as exc:
            return f"{where}: {type(exc).__name__}: {exc}"
        if free < STAGING_FREE_MB << 20:
            return f"{where} has {free >> 20} MB free"
        return sandbox_fault(self.params.get("sandbox_enforce"))

    @staticmethod
    def _plan_refused(exc: Exception) -> dict:
        """A trial's launch plan refused for what this trial's adapter is (dsh's MCP server name from its id, ...): the
        adapter's result, as a deployment of it that failed (adapter_failed), since the plan rendered at startup for the
        same config with a placeholder adapter (check_agent)."""
        return adapter_failed(f"the trial's launch plan refused the adapter: {exc}"[-2000:])

    def _tools_host(self) -> str:
        from hermit.harbor.tools import docker_gateway
        return str(self.params.get("tools_host") or docker_gateway())

    def _launch(self, task: dict, job: Path, deployed: dict | None, served, sign_in=True) -> Path | None:
        """The trial's launch plan under `job` (none for an agent outside the table); `sign_in` as launch.render's (a
        stream's step: "later", copied at its turn)."""
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
                             login_root=self.login_root, sign_in=sign_in, **self._mounted())

    def _serve(self, deployed: dict | None, job: Path, adapter: Path | None):
        """What the trial's agent reaches on this machine: the answer agent's model endpoint, or the adapter's tools
        (none without agent tools); started."""
        if self.cli == "answer":
            from hermit.harbor.tools import ModelEndpoint
            return ModelEndpoint(self.answer_model(), records=job, enforce=self.params.get("sandbox_enforce"),
                                 plan=self.model_plan()).start()
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
        except RuntimeError as exc:                           # the adapter's (_stage), unless the host's
            return self._output(task, self._staging_verdict(exc, job), None, None, started,
                                extra={"harbor_job": str(job)})
        served = plan = None
        try:
            try:
                served = self._serve(deployed, job, adapter)
            except (RuntimeError, OSError) as exc:           # no trial without its tools (or its model)
                return self._output(task, no_trial("tools_failed", str(exc)[-2000:]), None, None, started,
                                    extra={"harbor_job": str(job), "deployed": deployed})
            try:
                try:
                    plan = self._launch(task, job, deployed, served)
                except ValueError as exc:
                    if adapter is None:                      # the config's: rendered at startup (check_agent)
                        raise
                    return self._output(task, self._plan_refused(exc), None, None, started,
                                        extra={"harbor_job": str(job), "deployed": deployed})
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
            verdict = self.session_verdict(verdict, facts, self.plan_login())
        verdict = self.verifier_verdict(verdict, trial, self.redactions)
        try:
            log = curate_mod.curate(trial, job, verdict, redact=self.redactions, audit=job / curate_mod.TOOL_AUDIT,
                                    instruction=self.instruction(task), plan=plan, reply=said.get("reply"))
            self.cleanup_images(task)
        finally:
            if verdict["halt"]:                              # the trial's log kept first, however that went
                self.end_run(verdict, job)
        extra = {"harbor_job": str(job), "harbor_exit": code, "deployed": deployed}
        return self._output(task, verdict, result, log, started, trial=trial.name if trial else None, extra=extra,
                            trajectory=trial / "agent" / "trajectory.json" if trial else None, facts=facts,
                            usage=usage, said=said, deployed=deployed)

    def sign_in_back(self, job: Path) -> None:
        """After the trial (or the step) under `job`, however it ended: the ChatGPT sign-in Codex refreshed in the
        container becomes the shared copy when it passes signin.take_back's checks (a regular file of at most
        signin.MAX_BYTES, a ChatGPT sign-in, stamped at most SKEW ahead of this machine's clock in Harbor's local Docker
        environment, whose containers share that clock, and OTHER_CLOCK in another, whose clock may differ) and is a
        later refresh of the copy, for its account (hermit.agent.base.login.take_back); and no copy of a sign-in is
        left in the job (the home's, the one that came back)."""
        where = Path(job) / launch.LAUNCH_DIR
        returned = where / launch.RETURN_DIR
        try:
            if self.login == "chatgpt" and _folder(returned):
                local = (self.params.get("environment") or {}).get("type") in (None, "docker")
                signin.take_back(self.login_root, returned, local=local)
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
        infra: run again. Either way the session did not run out of its time: `timed_out` stays false (a method that
        reads it, Continual Harness's, tells the agent its session hit its wall clock)."""
        if verdict.get("exception_type") != "VerifierTimeoutError":
            return verdict
        if self.verifier_timeout == "infra":
            return {**verdict, "status": "infra", "failure_cause": "verifier_failed", "timed_out": False}
        return {**verdict, "status": "timeout", "failure_cause": "solution_timed_out", "timed_out": False}

    # --- the acting CLI's session, from its stream ------------------------------------------------------------------
    def session(self, outputs: Path | None, plan: Path | None, served, job: Path, task: dict | None = None):
        """(the session's facts, its usage, what its stream said) of a trial or a step whose outputs are under
        `outputs` (agent/), read as Hermit reads a session of the CLI (its Base Agent's `outcome`, `parse`,
        `adjust_usage`, priced at the binding's price): served, refused for the plan's limit (and until when), the
        provider unavailable, the sign-in lost, rejected, a model's refusal, the plan's report (Claude Code's stream's;
        on a ChatGPT login Codex's session logs', collected), started, the tools down, the tokens used; for Codex, a
        session that served nothing and whose stream says its sign-in or key was refused (signin.codex_signin_lost) has
        lost its sign-in, as Claude Code's Base Agent says of its own, and its provider was unavailable only by its last
        failure (signin.codex_unavailable); (None, None, {}) for an agent outside the table
        or a session that never ran (no stream). The container could write all of it: each file is read only as a
        regular file, never through a link (a link in the stream's place is no stream, a link among the collected files
        is not read), and the stream and the CLI's last message are read once, into this process's own copies under
        `launch/read/`, which the Base Agent then reads."""
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
                # its one request never answered, and the relay held it for its login's plan (tools.ModelEndpoint: the
                # plan's hold it met, before the session): why the plan held it back
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
        last = read / launch.LAST_MESSAGE                # the CLI's last message, as this process copied it
        if collected is None or not _copy(collected / launch.LAST_MESSAGE, last):
            last = None
        said = agent.outcome(raw, stream, last)
        if agent.backend == "codex":
            # Codex's own notices only (signin.lines: never what its log quotes of the session's work, which the Base
            # Agent's reading takes for notices too): a sign-in or a key refused, which no wait restores; the plan's
            # usage limit; the provider unavailable only by the session's last failure (the Base Agent's reading takes
            # a reconnect it recovered from for one too); a refusal of the run's configuration or bill (a 400, 402 or
            # 404, an API account's quota), which no retry changes, as the other CLIs' Base Agents read theirs
            # (`rejected`: model_rejected when it names the configuration, CONFIG_REFUSED; a halt when it is the bill,
            # BILLING_REFUSED)
            served = bool(said.get("served"))
            said["login_failed"] = not served and signin.codex_signin_lost(raw)
            said["unavailable"] = not served and signin.codex_unavailable(raw)
            said["refused"] = not served and signin.codex_refused(raw)
            refusal = None if served or said["login_failed"] else signin.codex_rejected(raw)
            said["rejected"] = refusal is not None
            if refusal is not None:
                said["failure"] = refusal[:600]         # what refused it, as Codex wrote it (its status first)
        started = said.pop("started", None)
        if started is None:
            started = any(line.lstrip().startswith("{") for line in raw.split("\n"))
        model = (settings.get("binding") or {}).get("ref") or settings.get("model") or agent.backend
        handle, usage = agent.parse(stream, model)
        stream.unlink()                                 # read: the trial's own copy stays in its outputs
        if last is not None:
            last.unlink()                               # read: the trial's own copy stays in its outputs
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
        """The trial's verdict with what the session's stream adds. A lost sign-in halts (the run ends: end_run). A
        plan's refusal or a provider
        that could not serve is not the agent's doing (infra: the trial is run again), unless the session ran out of
        the agent's time (its result, kept: a timed-out session has no completed turn, and an earlier notice it
        recovered from is no failure, as Hermit's own sessions keep it); nor is a provider that refused the run's
        configuration before it served any request (a key it does not take, a permission it does not give, a model it
        does not have, a request setting it does not take: model_rejected, config_refused; no halt), or the account's
        bill (its quota or credit spent, a payment it needs: billing_refused; usage_limit, a halt, as nothing later can
        be served), nor a session that never started or whose tools never came up (session_failed). A first request
        refused for what the adapter's or the task's input made it (a prompt too long, an invalid request, a content
        filter) is the agent's result, as Harbor scores it, and so is the model's own refusal (agent_refused_task,
        kept). A usage limit on a `login` (the acting CLI's, or that of the answer agent's model the runner serves:
        HarborAgent.plan_login) is the login's to wait for (the plan's window resets): no halt; without one (an API
        account's), nothing later can be served: a halt. The plan of the login the answer agent's model runs on is that
        login's to wait for too (`plan_held`: its one request unanswered, the relay having held it for that plan; a hold
        that came while the session ran is no wait of its own): a trial that ran out of the agent's time with it is
        infra (usage_limit, no halt), not the agent's timeout."""
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
            if billing_refused(facts):        # nothing later can be served, as after an API account's usage limit
                return {**verdict, "status": "infra", "failure_cause": "usage_limit", "halt": True,
                        "exception_message": f"the provider does not bill the account (its quota or credit spent, "
                                             f"or a payment it needs): {message()}"}
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

    def close(self) -> None:
        """The streams this runner stepped that are still open, closed (_close_runner_streams, which also runs when the
        runner is freed, and at exit)."""
        _close_runner_streams(self._drivers, self._streams, self._streams_lock)

    def _run_step(self, task: dict, adapter: Path | None, started: float) -> RunOutput:
        """One step of a multi-step task, through the stream's driver. While it runs, its index is one of its stream's
        steps in flight (`_in_flight`): a step whose turn cannot come ends at once (infra, stream_order: its driver
        waits for a step no rollout of this runner is running, or it has none and no rollout of its first step is
        running to start one), never waiting for its turn until the deadline, and the last of its stream's rollouts to
        end then stops the driver (_leave)."""
        index = int(task["step_index"])
        # the row's task and rollout, in this runner (its own id): two runs on one jobs directory, at once or one after
        # the other (a resumed train among them: its new runner has no driver for a stream under way, so that stream's
        # remaining steps in the epoch end stream_order, and its first step's next rollout starts it over), never share
        # a driver, its environment or its steps' directories. One runner's rollouts of one stream after it finished
        # (hermit eval's repeats, a training epoch after another) start a new driver there, which sets the earlier
        # trial aside (earlier/<n>/);
        # each rollout of a step works in a directory of its own (_step)
        stream = _safe(f"{task.get('harbor_task') or task['id']}__s{task.get('rollout', 1)}__{self.run_id}")
        with self._streams_lock:
            self._in_flight.setdefault(stream, []).append(index)
        try:
            return self._step(task, adapter, started, stream)
        finally:
            self._leave(stream, index)

    def _give_up(self, stream: str, index: int, cause: str) -> None:
        """A step that ends before it reaches its stream's driver (its staging, its adapter, its tools, its launch plan
        or the disk gate failed, its turn cannot come or did not come within its wait, its turn's sign-in or tools
        could not be readied, or any of them raised): the driver waits for it, so no later step's turn can come. The
        stream is given up, and the last of its rollouts to end stops its driver (_leave), whose environment no step
        can use any more: the stream's next pass (an eval's next repeat, a later epoch) starts from its first step in a
        new environment. The first reason given is kept."""
        with self._streams_lock:
            self._given_up.setdefault(stream, f"its step {index} ended before it reached the stream's driver ({cause})")

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
        if self._stop.is_set():
            return f"the run ended ({self._stop_why})"
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
        # this rollout's own: a rollout of the step after another (an eval's repeat, a later epoch) keeps its records
        step_dir = stream_dir / "steps" / f"{index:03d}-{_safe(name)}-{uuid.uuid4().hex[:8]}"
        step_dir.mkdir(parents=True)
        if index > 1 and client.running() is None and (why := self._first_step_missing(stream, client)):
            # its turn cannot come (no driver, nor a rollout of the first step to start one): it ends before anything
            # is staged or served for it, as the check before the driver's start below would end it
            with self._streams_lock:
                self._given_up.setdefault(stream, why)
            verdict = no_trial("stream_order", str(TurnLostError(index, why)))
            log = curate_mod.curate(None, step_dir, verdict, result=None,
                                    extra_files={DRIVER_LOG: stream_dir / DRIVER_LOG}, redact=self.redactions,
                                    audit=None, instruction=self.instruction(task), plan=None, reply=None)
            return self._output(task, verdict, None, log, started, trial=stream, step=name,
                                extra={"harbor_job": str(step_dir), "stream_dir": str(stream_dir)})
        # every way the step can end before its driver is asked for it (its staging, its tools, its launch plan, the
        # disk gate, a turn that cannot come or did not come in its wait, its turn's own setup; an exception of any of
        # them) gives the stream up: the driver waits for this step, so no later step's turn can come, and the last of
        # the stream's rollouts to end stops it (_leave); the stream's next pass then starts anew. A step the stream
        # already ran (StepAlreadyRunError) gives nothing up: the stream went on without this rollout.
        asked = passed = False
        reply, result, error, cause = None, None, None, "stream_lost"
        try:
            try:
                deployed = self._stage(task, adapter, step_dir)
            except OSError as exc:                                # this machine's, before the step: not run
                self._give_up(stream, index, "staging_failed")
                return self._output(task, no_trial("staging_failed", str(exc)[-2000:]), None, None, started,
                                    extra={"harbor_job": str(step_dir), "stream_dir": str(stream_dir)})
            except RuntimeError as exc:                           # the adapter's (_stage), unless the host's
                verdict = self._staging_verdict(exc, step_dir)
                self._give_up(stream, index, verdict["failure_cause"])
                return self._output(task, verdict, None, None, started,
                                    extra={"harbor_job": str(step_dir), "stream_dir": str(stream_dir)})
            served = endpoint = plan = None
            lost = turn = False
            try:
                try:
                    if self.cli == "answer":
                        from hermit.harbor.tools import ModelEndpoint
                        endpoint = ModelEndpoint(self.answer_model(), records=step_dir,
                                                 enforce=self.params.get("sandbox_enforce"),
                                                 plan=self.model_plan()).start()
                    else:
                        served = self._stream_tools(stream, deployed, step_dir, adapter)
                except (RuntimeError, OSError) as exc:
                    self._give_up(stream, index, "tools_failed")
                    return self._output(task, no_trial("tools_failed", str(exc)[-2000:]), None, None, started,
                                        extra={"harbor_job": str(step_dir), "deployed": deployed})
                try:
                    # the sign-in is copied at the step's turn (below), never now: an earlier step of the stream may
                    # still be running, and refresh the shared copy before this one starts
                    try:
                        plan = self._launch(task, step_dir, deployed, endpoint or served, sign_in="later")
                    except ValueError as exc:
                        if adapter is None:                      # the config's: rendered at startup (check_agent)
                            raise
                        self._give_up(stream, index, "adapter_failed")
                        return self._output(task, self._plan_refused(exc), None, None, started,
                                            extra={"harbor_job": str(step_dir), "stream_dir": str(stream_dir),
                                                   "deployed": deployed})
                    agent = self.agent_config(task, plan, deployed, tools_host=getattr(served, "host", None))
                    (step_dir / "agent.json").write_text(json.dumps(agent, indent=1) + "\n")
                    keep = set(json.loads(plan.read_text()).get("env_from_host", {}).values()) if plan else set()
                except OSError as exc:                            # this machine's, as the staging's: the step not run
                    self._give_up(stream, index, "staging_failed")
                    return self._output(task, no_trial("staging_failed", str(exc)[-2000:]), None, None, started,
                                        extra={"harbor_job": str(step_dir), "stream_dir": str(stream_dir),
                                               "deployed": deployed})
                try:
                    if client.running() is None and index > 1 and (why := self._first_step_missing(stream, client)):
                        raise TurnLostError(index, why)          # no driver (an environment) started for it
                    if client.running() is None and not self.wait_for_disk():
                        self._give_up(stream, index, "disk")
                        gate = self.params.get("disk_gate_gb", DISK_GATE_GB)
                        return self._output(task, no_trial("disk", f"the Docker data root {self._docker_root()} stayed "
                                                                   f"under {gate} GB free: the stream's environment "
                                                                   "was not started"),
                                            None, None, started)
                    with self._streams_lock:                     # this runner's to stop until it finishes (close)
                        self._drivers.setdefault(stream, client)
                    client.ensure(task_dir=self.task_dir(task), name=stream, agent=agent,
                                  environment=self.environment_config(), verifier=self.verifier_config(),
                                  idle_s=float(settings["idle_s"]), env=self.harbor_env(keep),
                                  start_timeout_s=float(settings["start_timeout_s"]),
                                  trial={k: self.timeout_multiplier(k) for k in TIMEOUT_MULTIPLIERS})
                    budget = self.wait_budget(task, build=False)
                    # Earlier steps of the stream may be running in other rollouts of the same batch: wait for them,
                    # each for its own budget at most (or `stream.turn_wait_s`); not for one no rollout is running (rows
                    # are submitted in order: an earlier step of the batch has begun by then), whose turn never comes.
                    turn_wait = float(settings.get("turn_wait_s") or (max(1, index - 1) * budget
                                                                     + float(settings["start_timeout_s"])))
                    client.wait_turn(index, timeout_s=turn_wait, missing=lambda nxt: self._step_missing(stream, nxt))
                    self.stopped()                               # another rollout ended the run while this one waited
                    turn = True
                    try:
                        if plan is not None and self.login == "chatgpt":
                            try:
                                launch.sign_in(plan, self.login_root)    # the shared copy as it is at this step's turn
                            except (OSError, RuntimeError) as exc:       # this machine's (no shared copy): no trial
                                raise _TurnUnready("staging_failed", f"the sign-in's copy for the step's turn: {exc}")
                        if served is not None and (served.adapter, served.audit_log) != \
                                (Path(adapter), step_dir / curate_mod.TOOL_AUDIT):
                            # this step's turn: its adapter behind the same URL, its calls in its own log (the server
                            # may have been started by a later step's rollout, or served the step before)
                            try:
                                served.swap(Path(adapter), step_dir / curate_mod.TOOL_AUDIT)
                            except (RuntimeError, OSError) as exc:      # its tools: none for the step
                                raise _TurnUnready("tools_failed", f"the adapter's tools for the step's turn: {exc}")
                    except _TurnUnready as exc:
                        # the driver waits for this step, which is not asked for: the stream is given up (below), never
                        # this machine's failure taken for the driver's (its stream lost, its driver left running)
                        error, cause = str(exc)[-2000:], exc.cause
                    else:
                        asked = True                             # from here the driver may run the step's session
                        reply = client.step(name=name, index=index, plan=str(plan) if plan else None,
                                            agent_kwargs=agent["kwargs"], timeout_s=budget,
                                            allowed_hosts=agent.get("extra_allowed_hosts") or [])
                except StreamOrderError as exc:
                    error, cause = str(exc), "stream_order"
                    passed = isinstance(exc, StepAlreadyRunError)   # the stream went on past it: not this rollout's
                    if isinstance(exc, TurnLostError):           # nor can a later step's: the stream is given up
                        with self._streams_lock:
                            self._given_up.setdefault(stream, exc.why)
                except (StreamError, RuntimeError) as exc:      # the driver's, or a tool server another step closed
                    # a driver that is gone (it closed the connection without an answer: killed while it answered) is
                    # lost as one whose socket refuses is
                    error, lost = str(exc), isinstance(exc, StreamError) and client.gone()
                except OSError as exc:              # the driver gone (killed), or silent past a request's time (busy)
                    error, lost = f"the stream's driver could not be reached: {exc}", not isinstance(exc, TimeoutError)
            finally:
                if endpoint is not None:
                    endpoint.close()
                self.sign_in_back(step_dir)
        finally:
            if not asked and not passed:
                self._give_up(stream, index, cause if error else "it ended before its driver was asked for it")
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
                verdict = self.session_verdict(verdict, facts, self.plan_login())
            verdict = self.verifier_verdict(verdict, outputs, self.redactions)
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
        try:
            log = curate_mod.curate(outputs, step_dir, verdict, result=result,
                                    extra_files={DRIVER_LOG: stream_dir / DRIVER_LOG}, redact=self.redactions,
                                    audit=step_dir / curate_mod.TOOL_AUDIT if turn else None,
                                    instruction=self.instruction(task), plan=plan, reply=said.get("reply"))
        finally:
            if verdict["halt"]:                              # the step's log kept first, however that went
                self.end_run(verdict, step_dir)
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
