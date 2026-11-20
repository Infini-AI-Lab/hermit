# How Harbor runs a task

This page records how a task in its own Harbor environment runs: which process does what, and where an agent's
command goes. The contract (the settings, the outcomes, what is refused) is in
[Benchmarks](../framework/task.md#benchmarks); this page explains its shape.

## In one line

Harbor runs the whole trial with its own `Trial.run()` (the environment, the setup, the agent phase, the verifier, the
teardown); the agent in that trial is a bridge that relays the commands of Hermit's agent. Every agent stays
Hermit's: it is written against the [Environment API](../framework/agent-layers.md#environments) (`exec`, `upload`,
`download`) and knows nothing of Harbor. Only a task set in Harbor's format, run in Harbor's environments, goes
through the bridge; an agent written for any other task needs nothing for Harbor.

## The processes

```{raw} html
:file: ../_includes/harbor-processes.html
```

Harbor needs Python 3.13 and its own dependencies, so it runs in a venv of its own
(`~/.cache/hermit/harbor-venv`). Each environment is one driver process under that venv's python
(`hermit/environment/harbor_driver.py`), which imports Harbor and the standard library only, never Hermit, and
speaks JSON lines with Hermit on its stdin and stdout (`hermit/environment/harbor.py`). The driver closes the trial
when its stdin closes; a driver that had to be killed has its trial's compose projects taken down by name.

## Who does what

| Step | Who | Where |
|---|---|---|
| Wait for disk room (the host's /tmp, Docker's data root, room kept for the images still to pull) | Hermit | `harbor_docker.py` |
| Start the environment: the prebuilt image pulled or the Dockerfile built, the healthcheck, the capability check | Harbor | `Trial.run()` |
| The agent's setup: install what the agent declares (`Agent.installs`), within `setup_timeout_sec` (1200 s) | Harbor, on the bridge's `setup()` | Harbor's installers |
| The agent phase: its network policy, its user, its timeout | Harbor | `Trial.run()` |
| The work in the agent phase | Hermit's agent, through the bridge | the agent's own code |
| End the agent phase: kill what still runs, remove sign-ins, copy the logs | the bridge | `harbor_driver.py` |
| The verifier: the tests uploaded only now, run, the reward read | Harbor | `Trial.run()` |
| Scrub credentials from the trial's files, write `result.json`, stop the environment | Harbor | `Trial.run()` |
| Read the outcome: the agent's result or infra; retry; mark unscored | Hermit | `harbor_errors.py`, `hermit/trainer/rollout.py` |
| Score from the verifier's verdict | Hermit's judge | `hermit.task.verify` |
| Remove the task's image as `docker_cleanup` says | Hermit | `harbor_docker.py` |

The order of every step is Harbor's. Hermit adds the warm start around the trial (the disk gate and the image
cleanup, which Hermit's earlier Harbor runner had) and reads the outcome as that runner read it.

## A trial and a command's path

```{raw} html
:file: ../_includes/harbor-trial.html
```

In ⑥ the bridge does not interpret a command; it runs it and returns the result. A variable the agent passes reaches the
container by name, never on a command line of this machine. Each command runs in a process group of its own, so the
bridge can end the ones still running when the phase ends. `upload` and `download` take the same path, one transfer
at a time. Commands are accepted only during the agent phase.

## Two kinds of agent

A CLI's commands do not pass through the bridge: the CLI runs in the container, like a terminal opened there.

```{raw} html
:file: ../_includes/harbor-agents.html
```

| | CLI agent (Codex, Claude Code) | Loop agent (a `ReactAgent`, the answer-only agent) |
|---|---|---|
| Where its loop runs | in the container (the CLI) | in Hermit's process |
| Who calls the model | the CLI, from the container | Hermit |
| What passes through the bridge | one `exec` that starts the session, the uploads and downloads around it | one `exec` for each command |
| How it gets into the container | installed by Harbor's setup (`Agent.installs`), or a binary of this machine the environment mounts (`agent.<cli>.bin`) | nothing to install (python3 for an adapter's hooks) |
| Network the agent phase needs | the model's host: the run adds the agent's model hosts to `allowed_hosts` | none of its own: the model is called from this machine |

Whether an agent is a CLI agent follows its Base Agent, not the benchmark: an agent built on a CLI Base Agent runs as
a CLI session whatever its Layer 3 adds; one built on `ReactAgent` is a loop agent. For a CLI session, Hermit renders
its home here and uploads it to `/installed-agent/hermit-home`, starts the CLI with the prompt on its stdin, reads its
stream as it reads any session of the CLI, and downloads its logs and usage when it ends.

## How the agent phase ends

The phase ends when Hermit's agent is done, or when Harbor's agent timeout ends it. The agent's own budget ends 60 s
before Harbor's (10% of a short one), so its session's end and logs reach the trial first. Then the bridge, as the
earlier runner's agents did:

1. kills the process group of each command it started that still runs; what a finished command left running (a
   server the task asked for) stays;
2. removes the credential files the agent registered (a CLI's sign-in);
3. copies the agent's logs, scrubbed, to `/logs/agent`, which a shared verifier may read.

If any of these fails, the trial is infra and its verifier's result is not the agent's. Otherwise Harbor runs the
verifier on what the agent left, whatever the session's end (a timeout, a CLI that exited with an error).

## Outcomes

| Outcome | Examples | What happens |
|---|---|---|
| The agent's result | a reward; a verifier with no reward or out of time on the agent's solution (0); a CLI error; an adapter that failed before any session | scored, 0 included |
| Infra | the environment did not start; the verifier failed or ran no test; a session that never started; a provider that could not serve | run again in a fresh environment, up to `infra_retries` (2), each after `infra_wait_s` (300 s); a session the provider could not serve under `busy_retries` (3) instead |
| Still infra after the retries | | `unscored`: the judge refuses to score it; `hermit eval` counts it in `errors` and as 0 in `mean_score`; `python -m hermit.harbor.report` gives pass@1 over the agent's results only |

Infra is never the agent's 0. The classification is the earlier runner's, ported
(`hermit/environment/harbor_errors.py`).

## Why this shape

A first design ran Harbor's trial steps from Hermit (the network switch, the verifier's order, the user, the
teardown). Each of them had to match Harbor's behavior, and the differences kept turning up in review. With Harbor's
own `Trial.run()` owning the trial, those steps are Harbor's code and cannot drift from it; Hermit keeps the agent
and the outcome code it already had. One bridge serves every agent, so a new agent never touches Harbor.

What a Harbor environment does not support yet (the adapter's tools, the agent's mounts, a multi-step task, ...) is
refused rather than dropped: see [Benchmarks](../framework/task.md#benchmarks).
