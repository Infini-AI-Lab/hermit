# Sandbox design

This page records the design of the sandboxes the framework starts. It replaced every earlier sandbox mode in one
change (no old configuration keeps working; an old key fails with where it went). The configuration fields are in
[Configuration](../framework/configuration.md#sandboxes).

## Scope

In scope: every process the framework starts while it trains, evaluates or checks: the acting agent, the judge, the
learner, the adapter's own code and the declared tools. Out of scope: what a host starts after `hermit deploy` or
`hermit convert`, and what a user's own program does through the Python interface.

The host process (the Trainer, `hermit eval`, `hermit check`) is the only trusted part. It holds the splits, the
answers, the run records and the model credentials, and it performs every handoff between sandboxes.

## Roles and sandboxes

Three roles run in three kinds of sandbox.

| Role | Task sandbox | Judge sandbox | Evolver sandbox | Host |
|---|---|---|---|---|
| acting (the agent under test) | runs inside | | | only when the pipeline runs on the host (below) |
| judge | `shared`: after the agent, in a new exec | `separate` | | `host`: reads the answer |
| evolver (the learner) | | | runs inside | |

| Sandbox | Contents | Never inside | Backend |
|---|---|---|---|
| task | the declared task paths, packages and variables, the framework's environment, the acting layer, the frozen adapter, its model's key | the data and split files, the judge and its tests, the runs and trees | bubblewrap; Docker for `environment.image` |
| judge | the task sandbox's settings, the judge's file and `judge.tests`; `separate`: only the collected paths of the agent's work, none of `environment.write` | | as the task |
| evolver | the learner CLI, the workspace under fixed names below `/w`, the framework's environment, what the method declares, its models' keys | the framework's code (unless the method needs it), the data, the judge, the test split | bubblewrap |

Every sandbox is an allowlist: a path, a variable or a host that is not declared does not exist inside. Python
packages are the framework's environment, shown as installed (below). Nothing is shown and then covered, except what
`sandbox.hide` names and, where the framework is not shown, a hermit package installed in that environment.

Handoffs go through the host and are files the receiving side gets read-only: the frozen adapter and the task into the
task sandbox; the output, the trajectory and the work directory out of it; the tests into the judge sandbox; the score
and feedback out of it; the learning material into the evolver sandbox; the adapter out of it, through a sanitized
copy. The one writable handoff is the judge's work directory: a `shared` judge works in the agent's own (its last
use), a `separate` judge in a sanitized copy of the collected paths, so the original stays as the agent left it.

## The bubblewrap core

`hermit/sandbox.py` builds every sandbox:

- The system (`/usr`, the `/lib` and `/bin` links, a few `/etc` files) read-only.
- The interpreter with its standard library and the environment the framework runs in: its site directories (the
  ones on its `sys.path`) as installed, so what is installed there is what a sandbox can import
  (`hermit-harness[sandbox]` installs a base: numpy, pandas, scipy, torch). Never what their `.pth` files add beside
  them (an editable install's checkout: hermit's own, a benchmark's with its data); `packages` names such a
  distribution, or one on `PYTHONPATH`, to show its package. Every other interpreter's site directory (a venv's base
  installation, a system Python under `/usr`) is replaced by an empty one. The hermit package itself (not the
  checkout) is shown when `framework`; a hermit installed in the environment is covered without it.
- `hide` (the run's `sandbox.hide`: module or package names, resolved to their files without importing them) is
  covered in every sandbox but the judge's, under every name it is shown by: a directory by an empty one, a file
  by an empty file and its package's `__pycache__` by an empty directory (a cache the judge's import writes later
  lands there too). The Trainer passes it to the processes it starts in `HA_SANDBOX_HIDE`, so the adapter's sandbox a
  worker starts covers it too. It suits a scoring module the agent does not import; a benchmark whose agent imports
  its scorer (below) cannot hide it.
- The granted paths, read-only or read-write, at their own location; binds (`Bind(source, target, writable)`) show a
  path under a name of its own. A `layout` (outside root, inside root) names the working directory a learner is told.
- Paths under a bound source are translated in the command, the environment and `--chdir`; `relative()` writes them
  relative to the layout's root, as a learner is told them; `outside()` maps a path a learner names back.
- `network: isolated` gives the process its own namespace with loopback only; the sandbox's own ports (a model server or
  meter), the ports the caller names (a tool server), every 127.0.0.1 URL in the environment and the allowlist proxy are
  forwarded over a Unix socket by `sandbox_relay.py` (never a URL in the command's arguments, where a prompt can carry
  the learner's text). `open` is the host network. A run config names these `no-network`, `allowlist` (with
  `allowed_hosts`) and `public`.
- `env_allow` lists the variables passed (names or patterns such as `MWM_*`); `limits` (memory_mb, file_mb, cpu_s)
  are set on the command by the relay.
- The relay checks the isolation inside before the command starts (outside TCP blocked, forwarded ports open, the
  probe paths absent) and reports it over a socket; each start of a task, judge, tool or learner sandbox leaves a
  record in the run's `sandbox/` directory (the strict spec and render checks keep none: they show only the adapter
  and the template).
- `view(...)` gives a method the run's sandbox (network, proxies, environment, enforcement, records, limits) with a
  view of its own in place of the run's binds.
- `enforce: required` refuses to run without bubblewrap; `best_effort` runs unconfined and records that. With the
  sandbox off (`sandbox.enabled: false`) nothing is confined and nothing is held back.

## Task sandbox

`hermit/isolate.py` runs the acting agent in a worker (`hermit/worker.py`) inside the task sandbox, one call per
task, and only the worker can hand a result back. The sandbox is built from the run config's `environment` section:
`read` and `write` (task paths), `packages`, `env`, `network` with `allowed_hosts`, `resources`. The call's own work
directory is writable; the `.logs` beside a task's agent file is a fresh directory each call. A declared path that
holds or lies inside a run secret (the data and split files, the judge and its tests, the runs and trees) refuses
the config; the isolation check proves the secrets absent at every start.

With `environment.image` a Codex acting agent runs in a container of its own (the Base Agent's container isolation),
started from a worker on the host, one per call; the worker puts the adapter's code the agent renders or serves in the
strict sandbox below. The pipeline runs in the host process instead in three cases, each recorded: `sandbox.enabled:
false`; a Harbor task set (`agent.name: harbor`, whose environment is Harbor's container); and `environment.runner:
host`, for a task whose own code isolates the adapter's code it runs (AgentDyn runs learned hooks in containers of their
own); a task's declared tools still run in their sandbox.

Inside the task sandbox the adapter's modules (its tools, its renders) still run in a nested strict sandbox of their
own (`HA_ADAPTER_SANDBOX`, set by the worker for the code the agent starts), which only narrows what the task sandbox
allows.

## Judge

The run config's `judge.mode` says where the judge runs.

| Mode | Where | Use when |
|---|---|---|
| `host` | the host process, on the downloaded output | the judge reads only the answer |
| `shared` | the task sandbox, a new exec over the agent's work directory, with the judge's file and `judge.tests` shown to it alone | the judge reads state in place |
| `separate` | a sandbox holding only the paths `judge.collect` names, copied from the agent's work through a sanitized archive and shown at the agent's own path | the result can be extracted and must be graded clean |

With the sandbox off every mode runs the judge on the host. A Harbor task set keeps Harbor's own verifier (shared or
separate as its `task.toml` says), read back on the host.
What a judge's result contains is the task's decision; which splits' results reach the learner is the method's.

## Evolver sandbox

The Trainer builds the learner's sandbox with the workspace under fixed names, the learner CLI's working directory at
`/w`:

```text
/w/adapter      the working copy (writable)          /w/state       state across calls (writable)
/w/material     the learning material                /w/scratch     scratch (writable)
/w/task-info    the task description                 /w/output      output (writable)
/w/history      the adapter's history                /w/root        the template's root
/w/method       the Evolver's directory              /w/validation, /w/environment/<name>  evolver_access
/w/agent-logs   run_agent's copies of the logs
```

The Base Agent translates every host path in the prompt (relative to `/w`), the command, the environment and the
working directory; a call's own writable path under the workspace (the CLI's home) is shown at its layout name. Tools
the learner calls resolve the paths it names (`Sandbox.outside`). The network reaches only its models (below), the
Trainer's tool endpoint and `sandbox.evolver.allowed_hosts`; the variables are `sandbox.evolver.env` and its models'
key variables. The interpreter
and its standard library are shown, not the hermit package: an Evolver that runs framework code inside sets
`sandbox_framework = True` (CommandEvolver). A method with a view of its own takes one (`Sandbox.view`): MWM shows its
native names (`/w` as the adapter, `/evidence`, `/environment`, `/skills`, `/patterns`, `/world-model`), RRSI shows
each proposer and role its own directory, harness2 each session its own directory. `evolver.params.hide` is gone,
and so is a learner's `isolation` setting: the learner and its roles (EvoSafe's) run in the evolver sandbox whenever
the run's sandbox is on.

A model only a CLI reaches (`llm: {cli: codex}`, the run's `llm` tool for GEPA, SkillOpt, ACE and the like) is an
agent that can run commands: each call is one Codex session in a sandbox of its own (`isolation: sandbox`, the
default; role `llm`): its empty workspace and home writable, its model's hosts (a ChatGPT login's) through a proxy of
its own, its key, nothing of the run. `isolation: container` gives it a container instead.

The adapter leaves the learner's sandbox as a copy made inside it and then sanitized: links that leave it, special
files and setuid bits are dropped.

## Models and keys

A sandbox gets its models' keys as Harbor gives its agents theirs: the key variable of each model a sandbox calls
passes in (the acting agent's binding or model spec in the task sandbox; the Evolver's and its roles' in the learner's),
the provider's host joins the allowlist (an isolated network becomes an allowlist; `public` needs nothing), and a
model server or meter on the loopback is forwarded. A container (`environment.image`, Harbor) gets the key the same
way. A task whose own code calls a model (CL-Bench's and ServeLearnBench's runners) passes its key variable in
`environment.env`. A Codex ChatGPT login is copied in, its hosts allowed; a CLI learner that names no model reference
copies the CLI's own login from the user's home.

What a run keeps and may share is scrubbed of every key value (`hermit/redact.py`); each value of a key-like
variable of the Trainer (`..._API_KEY`, `..._KEY`, `..._TOKEN`, `..._SECRET`, at least 16 characters) becomes
`<VARIABLE>`. Scrubbed: the learning material and the copies of `run_agent` logs the learner opens; each acting
call's directory and its log copy, and each sandboxed judge's directory, after the call (a failed one too); the
step records, `steps.jsonl`, the commits, the card, `summary.json` and `hermit eval`'s `results.jsonl`; the adapter
before each commit; `evolver_access.environment` before each learner call; at the end of the run the learner's
workspace (output, state, scratch, its CLI's home), `environment.write` and a configured `llm.cli` home; `llm.cli`'s
kept streams after each call; the sandbox and proxy records. Not inspected: what a process writes anywhere else (a
benchmark's own files outside its declared paths), and what a sandbox sends to its allowed hosts.

Web search runs at the provider; the CLI setting (`web_search`, required and explicit) switches it off.

A launcher that hands another program a command prefix runs the command through `python -m hermit.sandbox SPEC --`
(SPEC: the Sandbox's fields as JSON; `sockets` forwards a port inside to a Unix socket outside, a service of its own
per sandbox): the Lean swarm's agent jails and its Lean compiles (`examples/lean_swarm/launch.py`).

## Declared tools and host-side adapter code

A template's declared tool runs in a strict sandbox: its directory and the template read-only, the adapter it acts on
writable, no network, no variables. A task's declared tool is task code and runs in a sandbox built from
`environment` (its reads, writes, packages, variables, network), with its own file and the adapter. The template
check (`spec`) and host-side renders run the adapter's modules in the same strict preset, as strict as the run's
`sandbox.enforce` (passed explicitly; unconfined with the sandbox off).

## Known limitations

- MWM's host code channel (`wm run`, `mwm_core/sandbox.py` in the template) starts a bubblewrap child of its own,
  inside the adapter's sandbox, with a builtins and import whitelist: a second, inner layer kept byte-identical to
  native MWM (`examples/clbench_database/utils/tests/test_parity_golden.py`). The adapter's executable tools
  (`hermit/runtime/python_sandbox.py`) run in the framework's sandbox.
- No evolver sandbox on macOS: Seatbelt (as Codex and Anthropic's sandbox-runtime use) restricts the network and
  writes but allows reads everywhere by default (roadmap, issue #37 section 6).
- Web search runs at the provider; only the CLI setting controls it.
- A benchmark whose environment and scoring share one package shows its scoring to the agent when the agent needs the
  package: ServeLearnBench (`servelearnbench`), AgentDyn (its judge predicates live beside its tasks), CL-Bench (its
  runner grades each question beside the host it plays).
- A sandbox holds its models' keys (as Harbor's agents do): an agent or a learner can read them. What the run keeps
  is scrubbed of them; what a sandbox sends to its allowed hosts is not inspected.
- A Claude Code acting agent on a Claude subscription is not supported in the task sandbox yet (only a model
  reference or, for Codex, a ChatGPT login): its login file would need the shared copy and write-back a ChatGPT
  login has (roadmap).
- What a sandbox can import depends on the framework's environment: two machines with different packages installed
  give different sandboxes (each start's record lists what was bound). A benchmark package installed there is shown
  unless `sandbox.hide` names its scoring module; OfficeQA's agent imports SkillOpt's scorer
  (`skillopt.envs.officeqa.rollout` imports `evaluator`), so it stays shown.

## Verification

- Unit tests of the core (binds, translation, views, limits, the package allowlist), the task sandbox (what an agent
  sees and cannot do from inside), the judge modes, the models' access and the key scrubbing, with real bubblewrap
  where available.
- The isolation check in every sandbox fails the start when it fails.
