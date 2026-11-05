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
| task | the declared task paths, packages and variables, the acting layer, the frozen adapter | the data and split files, the judge and its tests, the runs and trees, model keys | bubblewrap; Docker for `environment.image` |
| judge | the task sandbox's settings, the judge's file and `judge.tests`; `separate`: only the collected paths of the agent's work | | as the task |
| evolver | the learner CLI, the workspace under fixed names below `/w`, what the method declares | the framework's code (unless the method needs it), the data, the judge, the test split, model keys | bubblewrap |

Every sandbox is an allowlist: a path, a package, a variable or a host that is not declared does not exist inside.
Nothing is shown and then covered.

Handoffs go through the host and are files the receiving side gets read-only: the frozen adapter and the task into the
task sandbox; the output, the trajectory and the work directory out of it; the collected artifacts and the tests into
the judge sandbox; the score and feedback out of it; the learning material into the evolver sandbox; the adapter out
of it, through a sanitized copy.

## The bubblewrap core

`hermit/sandbox.py` builds every sandbox:

- The system (`/usr`, the `/lib` and `/bin` links, a few `/etc` files) read-only.
- The interpreter with its standard library; every site-packages directory is replaced by an empty one and the
  distributions the sandbox shows are bound back into it: the framework's dependency closure when `framework` (the
  hermit package itself, not the checkout), plus `packages` with their dependencies.
- The granted paths, read-only or read-write, at their own location; binds (`Bind(source, target, writable)`) show a
  path under a name of its own. A `layout` (outside root, inside root) names the working directory a learner is told.
- Paths under a bound source are translated in the command, the environment and `--chdir`; `relative()` writes them
  relative to the layout's root, as a learner is told them; `outside()` maps a path a learner names back.
- `network: isolated` gives the process its own namespace with loopback only; every 127.0.0.1 URL in the command or
  its environment, and the allowlist proxy, is forwarded over a Unix socket by `sandbox_relay.py`. `open` is the host
  network. A run config names these `no-network`, `allowlist` (with `allowed_hosts`) and `public`.
- `env_allow` lists the variables passed (names or patterns such as `MWM_*`); `limits` (memory_mb, file_mb, cpu_s)
  are set on the command by the relay.
- The relay checks the isolation inside before the command starts (outside TCP blocked, forwarded ports open, the
  probe paths absent) and reports it over a socket; each start leaves a record in the run's `sandbox/` directory.
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

The pipeline runs on the host instead in three cases, each recorded: `sandbox.enabled: false`; a Harbor task set
(`agent.name: harbor`, whose environment is Harbor's container); `environment.image`, where a Codex acting agent runs
in a container of its own (the Base Agent's container isolation); and `environment.runner: host`, for a task whose
own code isolates the adapter's code it runs (AgentDyn runs learned hooks in containers of their own).

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

With the sandbox off every mode runs the judge on the host. A Harbor task set keeps Harbor's own verifier (shared or separate as its `task.toml` says), read back on the host.
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
```

The Base Agent translates every host path in the prompt (relative to `/w`), the command, the environment and the
working directory; a call's own writable path under the workspace (the CLI's home) is shown at its layout name. Tools
the learner calls resolve the paths it names (`Sandbox.outside`). The network reaches only the model proxy, the
Trainer's tool endpoint and `sandbox.evolver.allowed_hosts`; the variables are `sandbox.evolver.env`. The interpreter
and its standard library are shown, not the hermit package: an Evolver that runs framework code inside sets
`sandbox_framework = True` (CommandEvolver). A method with a view of its own takes one (`Sandbox.view`): MWM shows its
native names (`/w` as the adapter, `/evidence`, `/environment`, `/skills`, `/patterns`, `/world-model`), RRSI shows
each proposer and role its own directory, harness2 each session its own directory. `evolver.params.hide` is gone.

The adapter leaves the learner's sandbox as a copy made inside it and then sanitized: links that leave it, special
files and setuid bits are dropped.

## Model proxy

No sandbox holds a model key. `hermit/model_proxy.py` serves each provider origin on a loopback route; a sandbox gets
the route in place of the provider's URL and a secret of its own in place of the key, and the proxy puts the real key
(read from the host's environment) under the same header. A server on the loopback that takes no key (a local vLLM)
is reached directly; one whose key is set (a meter in front of a provider) is routed. The task sandbox's bindings (`agent.params.binding`, a chat
agent's model spec) are routed when the sandbox is built; the learner's sessions are routed by the Base Agent, from
the session's binding or the run's (the Evolver's). Exceptions, recorded: a Codex ChatGPT login is copied in (its
hosts reached through the allowlist proxy), and a task whose own code calls a model outside the bindings declares
its key variable in `environment.env`.

Web search runs at the provider; the CLI setting (`web_search`, required and explicit) switches it off.

## Declared tools and host-side adapter code

A template's declared tool runs in a strict sandbox: its directory and the template read-only, the adapter it acts on
writable, no network, no variables. A task's declared tool is task code and runs in a sandbox built from
`environment` (its reads, writes, packages, variables, network), with its own file and the adapter. The template
check (`spec`) and host-side renders run the adapter's modules in the same strict preset, as strict as the run's
`sandbox.enforce` (passed explicitly; unconfined with the sandbox off).

## Known limitations

- No evolver sandbox on macOS: Seatbelt (as Codex and Anthropic's sandbox-runtime use) restricts the network and
  writes but allows reads everywhere by default (roadmap, issue #37 section 6).
- Web search runs at the provider; only the CLI setting controls it.
- A benchmark whose environment and scoring share one package shows its scoring to the agent when the agent needs the
  package: ServeLearnBench (`servelearnbench`), AgentDyn (its judge predicates live beside its tasks), CL-Bench (its
  runner grades each question beside the host it plays). The first and the last call their model from their own loop
  and so hold its key (`SLB_API_KEY`; the litellm key variable the CL-Bench launchers add to `environment.env`).
- A ChatGPT login is copied into the task sandbox when the acting agent uses one.

## Verification

- Unit tests of the core (binds, translation, views, limits, the package allowlist), the task sandbox (what an agent
  sees and cannot do from inside), the judge modes and the model proxy, with real bubblewrap where available.
- The isolation check in every sandbox fails the start when it fails.
