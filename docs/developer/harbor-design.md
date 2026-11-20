# How Harbor runs a task

This page records how a task in its own Harbor environment runs: which process does what, and where an agent's
command goes. It shows the common path (a local Docker environment, a shared verifier, a built-in agent); the
contract, with every setting, outcome and refusal, is in [Benchmarks](../framework/task.md#benchmarks).

## In one line

Harbor runs the whole trial with its own `Trial.run()` (the environment, the setup, the agent phase, the verifier, the
teardown); the agent in that trial is a bridge that relays the commands of Hermit's agent. Every agent stays
Hermit's: it is written against the [Environment API](../framework/agent-layers.md#environments) (`exec`, `upload`,
`download`) and knows nothing of Harbor. Only a task set in Harbor's format, run in Harbor's environments, goes
through the bridge; an agent that works through the environment its call is given needs nothing for Harbor (a plain
`run(task, adapter)` function, which takes no environment, cannot run there).

## The processes

```{raw} html
:file: ../_includes/harbor-processes.html
```

Harbor runs in a venv of its own (Python 3.13 and `~/.cache/hermit/harbor-venv` in Hermit's setup; the python beside
the configured `harbor` command). Each environment is one driver process under that python
(`hermit/environment/harbor_driver.py`), which imports Harbor and what it depends on, never Hermit, and
speaks JSON lines with Hermit on its stdin and stdout (`hermit/environment/harbor.py`). The driver closes the trial
when its stdin closes; a driver that had to be killed has its trial's compose projects taken down by name (unless
`environment.harbor.environment.kwargs.keep_containers` is set).

## Who does what

| Step | Who | Where |
|---|---|---|
| Wait for disk room (the host's /tmp, Docker's data root; with `image_sizes`, room kept for the images still to pull) | Hermit | `harbor_docker.py` |
| Check the task's capabilities, then start the environment: the prebuilt image pulled or the Dockerfile built, the healthcheck | Harbor | `Trial.run()` |
| The agent's setup: install what a built-in agent declares (`Agent.installs`), within `setup_timeout_sec` (1200 s) | Harbor, on the bridge's `setup()` | Harbor's installers |
| The agent phase: its network policy, its user, its timeout | Harbor | `Trial.run()` |
| The work in the agent phase | Hermit's agent, through the bridge | the agent's own code |
| End the agent phase: kill what still runs, remove sign-ins, copy the logs | the bridge | `harbor_driver.py` |
| The verifier: the tests uploaded only now (shared mode), run, the reward read | Harbor | `Trial.run()` |
| Stop the environment, write `result.json`, scrub the credentials the trial knows from its files | Harbor | `Trial.run()` |
| Scrub the credentials passed through the bridge from the trial's files, then reply | the driver | `harbor_driver.py` |
| Read the outcome: the agent's result or infra | Hermit | `harbor_errors.py` |
| Remove the task's prebuilt image as `docker_cleanup` says (best effort) | Hermit | `harbor_docker.py` |
| Run again, mark unscored, or score from the verifier's verdict | Hermit, its judge | `hermit/trainer/rollout.py`, `hermit.task.verify` |

The trial's steps and their order are Harbor's. Hermit adds the warm start around the trial (the disk gate and the
image cleanup, which Hermit's earlier Harbor runner had) and reads the outcome as that runner read it. With
`[verifier] environment_mode = "separate"`, Harbor stops the agent's environment and verifies in a fresh one, which
gets Harbor's collected artifacts and its own mounts (not the agent's workspace or `/logs/agent`), and whose image may
bring the tests itself.

## A trial and a command's path

```{raw} html
:file: ../_includes/harbor-trial.html
```

In ⑥ the bridge does not interpret a command; it runs it and returns the result. In a local Docker environment a
variable the agent passes reaches the container by name, never on a command line of this machine (another environment
type passes it as Harbor's own code does). Each command runs in a process group of its own, so the bridge can end the
ones still running when the phase ends. `upload` and `download` take the same path, one transfer at a time, as
Harbor's file copies (an upload is given to the agent user). Commands are accepted only during
the agent phase.

## Two kinds of agent

The commands a CLI's model asks for do not pass through the bridge: the CLI runs in the container, like a terminal
opened there.

```{raw} html
:file: ../_includes/harbor-agents.html
```

| | CLI agent (Codex, Claude Code) | Loop agent (a `ReactAgent`, the answer-only agent) |
|---|---|---|
| Where its loop runs | in the container (the CLI) | in Hermit's process |
| Who calls the model | the CLI, from the container | Hermit |
| What passes through the bridge | a few `exec`s that find the CLI and lay out its home, one that runs the session, the transfers around it | one `exec` for each command, and transfers (the answer-only agent: its answer) |
| How it gets into the container | installed by Harbor's setup (`Agent.installs`; python3 for an adapter's hooks, best effort), unless the image already has the version asked for or the environment mounts a binary of this machine the agent user can run at that version (`agent.<cli>.bin`) | nothing to install |
| Network | the model's host, which it calls from the container | the model is called from this machine |

For either kind, the run adds the agent's model hosts to the hosts a task without a public network lets the agent
phase reach (`allowed_hosts`); a task whose tools need more declares it in its `task.toml`.

Whether an agent is a CLI agent follows its Base Agent, not the benchmark: an agent built on a CLI Base Agent runs as
a CLI session whatever its Layer 3 adds; one built on `ReactAgent` is a loop agent. For a CLI session, Hermit renders
its home here and uploads it (to `/installed-agent/hermit-home` unless `remote_home` says otherwise), starts the CLI
with the prompt on its stdin, reads its stream (the reply, the steps, the usage) from the `exec`'s output as it reads
any session of the CLI, and downloads what the session left: Codex's last-message file (Claude Code's reply is in
its stream) and, with a ChatGPT sign-in, the session files and the refreshed sign-in.

## How the agent phase ends

The phase ends when Hermit's agent is done, or when Harbor's agent timeout ends it. The agent's own budget ends 60 s
before Harbor's (10% of a short one): time kept for its session's end and logs to reach the trial. Then the bridge,
as the earlier runner's agents did:

1. kills the process group of each command it started that still runs; what a finished command left running (a
   server the task asked for) stays;
2. removes the credential files the agent registered (a CLI's sign-in);
3. scrubs a CLI's stream, written to `/logs/agent` as it ran, in place, best effort (in a local Docker environment,
   whose `/logs/agent` is the trial's folder on this machine; elsewhere a shared verifier gets it as it came), then
   copies the log directory Hermit's agent supplied with `done`, if it came in time, scrubbed, to `/logs/agent`, which a
   shared verifier may read.

If a kill, a removal or the copy fails, the trial is infra and its verifier's result is not the agent's; a stream
this machine cannot read is left as it came. Otherwise Harbor runs the
verifier on what the agent left whenever the session's end is the agent's result (a timeout, a CLI that exited with
an error). Otherwise the verifier is skipped: an adapter over Codex's context cap is the agent's 0; a CLI that never
started, or a session the provider could not serve, is infra.

## Outcomes

| Outcome | Examples | What happens |
|---|---|---|
| The agent's result | the verifier's reward; a verifier that found no reward file (0, when no infra signal explains it) or ran out of time on the agent's solution (0, unless `verifier_timeout: infra`); a CLI that exited with an error; an adapter that failed before any session | scored, 0 included |
| Infra | the environment did not start; a verdict with neither a reward (under `reward_key`) nor an exception; a verifier whose tools never arrived or whose disk filled; a session that never started; a provider that could not serve | run again in a fresh environment, up to `infra_retries` (2), each after `infra_wait_s` (300 s); a session a CLI's provider could not serve under `busy_retries` (3, after 120 s × n) instead; a cause that runs no better again (the disk gate, a refused configuration, a task Harbor refuses as `unsupported_task`) is not run again |
| Still infra after the retries | | `unscored`: the judge refuses to score it; `hermit eval` counts it in `errors` and as 0 in `mean_score`; `python -m hermit.harbor.report` gives pass@1 over the agent's results only (with `pass_reward` set) |

A lost sign-in or a spent quota ends the run; a session a subscription plan refused runs again once the plan allows,
outside these budgets.

Infra is never the agent's 0. The classification is the earlier runner's, ported
(`hermit/environment/harbor_errors.py`).

## Why this shape

A first design ran Harbor's trial steps from Hermit (the network switch, the verifier's order, the user, the
teardown). Each of them had to match Harbor's behavior, and the differences kept turning up in review. With Harbor's
own `Trial.run()` owning the trial, those steps are Harbor's code and cannot drift from it; Hermit keeps the agent
and the outcome code it already had. One bridge serves every agent, so a new agent never touches Harbor.

What a Harbor environment does not support yet (a multi-step task, the agent's mounts, ...) is refused rather than
dropped: see [Benchmarks](../framework/task.md#benchmarks).
