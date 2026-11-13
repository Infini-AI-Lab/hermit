# Changelog

Changes are grouped by software version. Entries under Unreleased describe work awaiting a tagged release.

## Unreleased

### Breaking

- One sandbox design replaces every earlier sandbox mode ([design](docs/developer/sandbox-design.md)); no old
  configuration keeps working, and an old key fails with where it went:

  | Old | New |
  |---|---|
  | `sandbox.profiles.rollout` (`read`, `write`, `mask`, `env`, `network`, `allow`, `packages`) | `environment` (`read`, `write`, `env`, `network`, `allowed_hosts`, `packages`, `resources`); no masks: a declared path may not hold the data, the judge or the runs |
  | `sandbox.profiles.rollout.enabled: false` with the agent's `isolation: container` | `environment.image` (+ `environment.container`) |
  | `sandbox.profiles.rollout.enabled: false` for a pipeline that starts containers itself | `environment.runner: host` |
  | `sandbox.network`, `sandbox.allow`, `sandbox.env` (the learner's) | `sandbox.evolver.allowed_hosts`, `sandbox.evolver.env`; the learner's model key variable and provider host are passed in by themselves |
  | `sandbox.read` | `evolver_access.environment` |
  | `sandbox.profiles.tool` | a task's declared tool runs with `environment`; a template's in a strict sandbox |
  | `evolver.params.hide`, `HidingSandbox`, masks | removed: every sandbox is an allowlist |
  | `sandbox.audit: files` | removed; the launch records and the proxies' logs remain |
  | `spec.SANDBOX_ENFORCE` | `check_spec(..., enforce=...)` |

- The learner works in `/w` (`/w/adapter`, `/w/material`, `/w/task-info`, ...): its prompt names the workspace
  relative to `/w`, and the tools it calls resolve the paths it names. Every sandbox shows the Python environment
  the framework runs in as installed (`hermit-harness[sandbox]` adds numpy, pandas, scipy and torch), never an
  editable install's checkout; `sandbox.hide` covers a module of it in every sandbox but the judge's.

### Added

- ASPIRE on LIBERO (`examples/aspire_libero/`): LIBERO's Object, Spatial, Goal, Long and LIBERO-90 suites as tasks
  of ASPIRE's harness, on the Robosuite example's engine, which now takes a task row's `benchmark` and with it the
  venv, the init mode (`seeded` for LIBERO), the environment and the docs page; training, LIBERO-90 and smoke configs.
  The task sandbox of both examples is shown ASPIRE's docs as `launch.sh` stages them outside the checkout
  (`agent.params.aspire_docs`), without what names ASPIRE's worked programs, instead of the checkout's own pages.
- GPUs in the bubblewrap sandbox: `environment.gpu: [<N>, ...]` shows those NVIDIA GPUs (the N of `/dev/nvidia<N>`) in
  the task sandbox and its tools' and judge's, with what CUDA and EGL read to find them (the driver's devices and,
  read-only, `/sys/devices`, `/sys/bus/pci`, `/sys/module/nvidia(_uvm)`), and no other GPU's device (their `/sys`
  entries stay readable); inside they count from 0. `Sandbox(gpu=...)` for a sandbox a task starts itself. Without it,
  as before, a sandbox has no GPU (EGL renders in software where Mesa is installed).
- Pi as a CLI Base Agent (`pi`): the acting agent `agent.base: pi` and the `pi` Evolver run Pi 1.5's `--mode json`
  in its own home, offline, with a model reference as Pi's `models.json` provider (the key as `$VARIABLE`); the
  call's tools are an MCP server offered directly; a failed request fails the session although Pi exits 0. Pi takes
  no roles and has no web search; `image_input: true` declares a bound model that takes images. `hermit deploy
  --host pi` declares the adapter's tools `exposure: direct`, with a 120 s timeout.
- mini-swe-agent as a CLI Base Agent (`mini-swe-agent`): the acting agent (`agent.name` or `agent.base:
  mini-swe-agent`) runs one task through mini-swe-agent 2.4.6's Python API in a process of its own, whose JSON
  events are the stream; a model reference is called through litellm with the key passed per request, and the
  agent's commands get only `env`. The call's tools are one bash command, `hermit-tool`. `hermit deploy --host
  mini-swe-agent` writes `mini-swe-agent.yaml`. Installed with the `mini-swe-agent` extra.
  [Tutorial](docs/tutorials/mini-swe-agent.md).
- OpenCode as a CLI Base Agent (`opencode`): the acting agent `agent.base: opencode` and the `opencode` Evolver run
  OpenCode 2.2's `run --format json --standalone` (a server of the session's own, never its shared background
  service) in its own home and XDG folders, with no update, catalog or download requests. It runs a model reference
  only, as OpenCode's provider `hermit` (the key as `{env:VARIABLE}`, chat completions); what the session must hold
  (the provider, the permissions, no sharing) is its inline config, which outranks a workspace's `opencode.json`;
  the call's tools are an MCP server offered directly (not through code mode). Whether its provider could not serve
  is read from OpenCode's own error type (`provider.rate-limit`, `.internal`, `.transport`, `.timeout`), so the
  acting agent runs such a task again. Its usage counts the model calls of its compactions, which its stream leaves
  out, from its session's totals. OpenCode takes no roles and has no web search. `hermit deploy --host opencode`
  writes `AGENTS.md`, the project's `opencode.json` and `.opencode/skills`.
- DeepSeek Harness as a CLI Base Agent (`dsh`): the acting agent `agent.base: dsh` and the `dsh` Evolver run DeepSeek
  Harness 0.2.2-rc.1's `dsh --profile headless --json` in its own home, with a model reference as its `llm-pi-ai`
  provider (the key by its variable's name); the call's tools are an MCP server; a run with no completed end, or whose
  tools did not start, fails even when `dsh` exits 0. DeepSeek Harness takes no roles and no login;
  `agent.dsh.permission_mode` picks its own sandbox; `image_input: true` declares a bound model that takes images;
  `web_search: true` gives web fetch only unless the model is reached through DeepSeek's API on `DEEPSEEK_API_KEY`. The
  MWM and RRSI Adapter Evolvers refuse `evolver.params.backend: dsh`. `hermit deploy --host dsh` writes the adapter's
  tools, with a 120 s timeout, in a patch layer, `.dsh/hermit.patch.yml`, which a session loads with `--patch`.
- Models in sandboxes as Harbor's agents get theirs: each sandbox gets its models' key variables and provider hosts
  (an isolated network becomes an allowlist) and forwards a model meter on the loopback; what a run keeps (the
  learning material, the step records, the adapter) is scrubbed of key values (`hermit/redact.py`). The allowlist
  proxy goes out through the host's own proxy when it has one.
- `judge.mode`: `host`, `shared` (in the task sandbox after the agent, `judge.tests` shown to it alone) and
  `separate` (a fresh sandbox holding the paths `judge.collect` names, copied through a sanitized archive).
- Sandbox resource limits (`resources`: memory, file size, CPU time), environment-variable patterns (`MWM_*`), and
  `Sandbox.view` for a method's own view of the run's sandbox.

- Benchmarks through one protocol: `agent.name: harbor` runs any task set in [Harbor](https://github.com/laude-institute/harbor)'s
  format (`task.toml`, `environment/`, `tests/test.sh` writing the reward) as the acting pipeline. One rollout is
  one Harbor trial: Harbor starts the task's container, installs the acting agent (Codex by default, with the
  adapter deployed into the container as `hermit deploy` lays it out: context, skills, roles, hooks), runs the
  instruction and the task's own verifier; `judge.path: hermit.harbor.judge` reads the reward. A multi-step task
  (one environment through many steps, each with its instruction and verifier) is a stream: a driver keeps the
  environment between the Trainer's steps so a learner acts between them, and `harbor run` evaluates the whole
  stream with a frozen adapter. `python -m hermit.harbor.dataset` downloads a dataset, indexes task directories
  into `tasks.jsonl` rows (per task, per rollout, per step), writes splits and a content lock. The container-side
  half (`hermit/harbor/harborside/`: the Codex agent, a scripted agent for smoke tests, the stream driver) runs
  under Harbor's own venv. A Harbor task set's pipeline runs on the host, the task's container being its sandbox.
- A test workflow (`.github/workflows/tests.yml`): a pull request runs the test groups it touches
  (`pytest --changed`); main, a daily schedule and a manual run run the whole suite.
- Claude Code on the user's subscription, acting and learning, with no API key
  ([guide](docs/guides/claude-subscription.md), `examples/claude_subscription/`). `login: claude` signs a session in
  with the token of `claude setup-token` in `CLAUDE_CODE_OAUTH_TOKEN`; nothing else may pay (an inherited or a
  workspace's API key, key helper or base URL is dropped and blanked) and no credential is copied or kept: a sandbox
  gets the token by name for the call that runs a session on the login (not for its other commands, nor for its
  judge) and `api.anthropic.com` on its allowlist. Right after each session the token's value is replaced by its name
  in what the session left (its stream, its stderr log, a kept home's transcripts), since a command of the session can
  read it from a parent process. Its sessions share a plan under `~/.cache/hermit/claude-login/plan/` as Codex's do:
  `PAUSE`, `MAX_USED`, `SLOTS`, each window's report, a hold and a rerun on a refusal, and a hold after a session that
  drew usage credits unless `usage_credits: true` (which also lets a session past a credits hold another run wrote). A
  Claude Code session outside a container (in the task sandbox, or with the pipeline on the host) has an empty
  directory of its own in place of the plan's (a nested bubblewrap): its commands can neither change nor lock the plan.
- Claude Code in a Docker container of its own, as Codex (`environment.image` for an acting agent, `isolation:
  container` for `llm: {cli: claude}`): `docker/claude/` builds the image from the machine's own Claude Code, and the
  container gets the network, the egress proxy, the mounts and the limits Codex's gets. Claude Code's own sandbox runs
  inside, as Codex's workspace-write sandbox does: the session's commands have a process namespace of their own, no
  network and writes to the workspace and `/tmp` only, set in the last `--settings`, which no project settings file can
  widen, and the session's own file tools may not edit its Claude Code settings (a hook written there would run
  outside the sandbox). The login's token goes to Docker by name and to Claude Code on a file descriptor, out of every
  environment in the container; the image's CLI is execute-only, so the kernel keeps its memory and descriptors from a
  hook or server beside it; the plan stays with the process outside. Before the first session the agent checks the
  image's Claude Code (2.1.318 or later) and that its sandbox starts there, and `hermit check` does not ask for the
  machine's own `claude`. An adapter's tools reach a Claude Code session in a container from the session's tool server,
  as they reach Codex's.
- `llm: {cli: claude, ...}` and, for acting pipelines, `agent.model: {cli: ...}`: a CLI as the model of code that calls
  a chat API. Each request is one session in a bubblewrap sandbox of its own (`isolation: sandbox`, the default, as for
  `cli: codex`) or in a container (`isolation: container`); a Claude Code session has no tools and can only reply.
  `hermit check` and `train` check a run's logins. On a machine without bubblewrap (and `sandbox.enforce: required`)
  such a sandbox is refused before anything starts (Codex's too, which failed at its first call).
- Harness² with Claude Code as the acting agent: Claude Code runs the same `harness2-codex` tree in its own places
  (`CLAUDE.md`, `.claude/skills/`, `.claude/agents/`, `.claude/settings.json`), and the editors read its event stream in
  upstream's grammar (`harness2_upstream/render.py`: every tool's call and result, what a hook printed, a background
  sub-agent's notices; the login's token replaced by its name there, in a deliverable and in the kept execution logs).
  Working Claude Code sessions print their hook events (`--include-hook-events`). An execution whose session failed
  still scores 0, and `harness2.json` and `scores.jsonl` say why. `setup()` refuses a variable in
  `evolver.params.env` that would pay instead of `login: claude`, and Codex editors take `plan_dir`,
  `plan_max_wait_s` and `usage_credits`.
- OfficeQA's third column: Claude Haiku 5.5 with no skill, acting in Claude Code on a Claude subscription, one
  container per session as in the luna column (`skillopt/run_claude_haiku.yaml`: 0.599 before the one sandbox design,
  0.616 after it).
  `harness2_stream/run_claude_haiku.yaml` sets up the Harness² stream to act and learn the same way
  (`slots_claude.yaml`; `launch.sh` refuses a missing token, pins one Claude Code for the editors, and for an executor
  outside a container also for it, and a resumed run keeps its pin); it ran on the stream's first two questions,
  without containers, not in full.
- The OPRO method (Yang et al., ICLR 2024, arXiv:2309.03409): the `opro` Evolver on the `prompt-only` template, one
  OPRO step per training step, with the authors' meta-prompt verbatim; the optimizer is the run's `llm`
  (`docs/methods/opro.md`). OPRO on OfficeQA: `examples/officeqa/opro/`, in the GPT-5.6-Luna setting.
- ASPIRE (arXiv:2607.00272) as a method, with its three roles as Codex sessions given ASPIRE's own prompts: the
  `aspire` template (a skill library of Markdown entries rendered whole as the actor's context, every snippet checked
  under the CaP program rules, no privileged access) and the `aspire` Evolver (the coordinator: an agent Evolver with
  ASPIRE's guide that repairs graded entries, verifies proposals with `run_agent` on the task's verifier row, and
  chooses the next task through `StepResult.next_tasks`), with `examples/aspire_robosuite/`: the fix-loop subagent,
  its evolutionary search and the verifier as the Codex acting agent in the task sandbox, which reaches ASPIRE's robot
  execution engine through a simulator service on the host (the sandbox has no GPU) that holds its seed rules; the
  judge runs Stage 2 on the host.

### Changed

- A chat request through a CLI session follows a stated contract, on Codex as on Claude Code: what a session cannot do
  (tools, sampling, non-text parts, another model) is a 400, not dropped; `max_tokens` is accepted and said not to apply
  (`unapplied` in the reply); `llm.unapplied` accepts a named field going unapplied.
- RRSI's analyst, digesters and critic, AHE's Evolve Agent and Agent Debugger and MWM's learner run on the Evolver's
  CLI, `codex` or `claude`; Harness²'s Claude channel takes a login.
- Claude Code's prompt goes on stdin, so a prompt starting with `-` is no option, and a line break keeps one starting
  with `/` from being run as a CLI command.
- Outside a container (in the task sandbox, or with the pipeline on the host), a task's data mounted inside a Claude
  Code workspace is bound there by a nested bubblewrap (a real read-only directory, as in a container), not linked: a
  search that walks the tree (`grep -r`, `find`, Claude Code's Glob and Grep) does not follow a link. A rollout's
  record says which (`pipeline_extra.mounts`), and a machine where bubblewrap cannot do it links them with a warning. A
  working session's reply is its whole final message, not only its last text block: a message the CLI continued past
  its output limit is joined where it was cut, and text before the harness's own message (a Stop hook that sent the
  model back to work) or before a result that closed an earlier turn (a background sub-agent's hand-back) is left out.
- Every Claude Code session Hermit starts is denied the tools whose work outlives it or reaches past it
  (`RemoteTrigger`, `CronCreate`, `CronDelete`, `CronList`, `ScheduleWakeup`; `ListAgents` and `SendMessage`, the user's
  other Claude sessions), with web search's in one `--disallowedTools` list.
- A plan's hold (a refusal, or a session on usage credits) lasts to its own end: a later report from a session that
  started before it no longer lifts it, and a later refusal that names an earlier reset no longer shortens it (Codex's
  plan as well).
- On a login, a chat client waits out the plan with its session (its `timeout` is extended by `plan_max_wait_s`)
  instead of hanging up, retrying and failing the call; a session past its clock answers 504 with
  `x-should-retry: false`, which the client honours; a streamed error keeps its `hermit_login_failed` code (Codex's
  route as well).
- The Claude Code Base Agent itself refuses, for every session, an effort outside the CLI's levels (it would warn and go
  on at its default), `login: claude` without a model name (the CLI's default differs by sign-in), and `output_format:
  json` on a login or a session that only replies (the payer, the plan's report and the reply are read from the event
  stream). `instructed()` is part of the Base Agent interface (a caller's instructions in the agent's own place).
  `hermit check` and `train` warn of an acting agent on `login: claude` that runs on the host (its sessions unconfined)
  or in a task sandbox with `environment.network: public`, and of Claude Code with no login where it signs in with a
  copy of the user's own login (a learner; an acting agent on the host or in a container).
- What every session would refuse is refused before the first one, wherever the agent runs (the task sandbox, a
  container, the host): when the config loads, a CLI acting agent's login that is not its CLI's own (Codex's or
  Claude Code's, either way), a login with no model name or with a reference, an effort Claude Code does not take, a
  login's plan setting given beside the model's name (it belongs in `agent.<base>` or `evolver.<base>`), and an
  `environment.env` or `sandbox.evolver.env` pattern that would take the Claude login's token into a whole sandbox;
  when `check` or `train` starts, an agent Evolver's login that is not its CLI's, or `login: claude` with no model.
  A `plan_dir` given as `~/...` or a relative path is one directory for every worker (resolved where it is given).
  A Claude Code session in the task sandbox whose plan a nested bubblewrap cannot cover is refused while
  `sandbox.enforce` is `required`.
- The tests are in groups: `tests/core/` for the framework and `tests/methods/<name>/` for each method
  (`tests/README.md`). `python -m pytest --changed` runs the groups the branch touches since `origin/main`
  (`tests/selection.py`: the core group for every change but documentation, a method's own files its group, a
  shared layer every group); the header says which groups ran and why. Tests that run an agent CLI installed on the
  machine are marked `integration` and run only with `--integration`. Tests another test already covers were
  removed and tests that repeat one setup share it: the full suite runs in about half the time.
- The CLI Base Agents share one runner, `CliAgent` (`hermit/agent/base/cli.py`): what differs per CLI (its command,
  its home, how its event stream is read) is its subclass's hooks, and `CLIS` names every CLI. `codex` and `claude`
  behave as before; a new CLI is one subclass.
- `Usage` gains `unreported_calls`, the calls whose tokens a CLI did not report (only DeepSeek Harness's reader sets
  it): a usage that has any keeps an unknown cost, not the price of the tokens reported.

### Fixed

- `Sandbox.wrap` ran a virtual environment's interpreter (`<venv>/bin/python`, a link to its base interpreter) by
  its real path, so it started without the environment and its packages failed to import. An interpreter whose
  environment is granted (the sandbox's `readable` or `writable`, the call's, or `interpreters`) now runs by its own
  name, with the links on its way to its installation shown; its `pyvenv.cfg` names no installation to show from a
  call's grant, as before. An ungranted one runs as before, and so do one beneath a layout's root (`/w`), one
  reached from its grant through a link to a place nothing shows, and one whose links lead to its installation
  through a linked directory other than the system's (uv's version alias) (#95).
- A redeployment (`hermit deploy` into the same workspace) left a skill the new adapter no longer exports in the
  host's skills folder (`.codex/skills`, `.claude/skills`, `.pi/skills`, `.opencode/skills`, `.dsh/skills`), where the
  agent still loaded it, and a file a skill no longer has inside its copy. Each exported skill's copy is now replaced,
  and a skill the last deployment's record names that this one does not export is moved to `.retired-skills/` (as a
  role is to `.retired-roles/`); a skill the user put there stays. A redeployment refuses, before writing anything,
  when an exported skill would replace a skill folder no deployment wrote or the skills folder is a link (#96).
- ASPIRE's generated programs ran unconfined on the host: cap-harness runs them, and ASPIRE's program validator is
  passed by a format string's attribute traversal, so a program could read and write the host's files (other
  sessions' workspaces, the judge's Stage 2 runs) and run held-out seeds itself. Every cap-harness process (the
  simulator service's, the judge's, the direct actor's) now runs in a bubblewrap sandbox of its own
  (`examples/aspire_robosuite/utils/agent/sim.py`: `confine`) that reads ASPIRE's checkout, writes only its run's
  output, reaches only the providers' ports and renders on the GPUs `ASPIRE_SIM_GPU` names (Mesa's software
  renderer without one).
- A deployed workspace's tool server (`.codex/config.toml`, `.mcp.json`, `.pi/mcp.json`) in a task-sandboxed
  acting run crashed at startup, so the adapter's tools never loaded: its sandboxed Session loads no adapter in
  the server's process, and the server now reads the manifest from the Session.
- A workspace agent's run failed after its session had answered when tidying the kept workspace could not read or
  remove an empty mount point yet (`PermissionError` on `docs/root_1`: Docker Desktop can hold a bind mount's target
  a moment after its container exits); the answer was scored as a pipeline failure. Tidying now leaves such a
  mount point in place (`hermit/agent/acting/workspace.py`, `_clean`).
- The harness2 Evolver refused to continue a stream (`library_from`) on a machine without the session CLI on its
  PATH: its pin kept the bare name `claude` while the continuation's check compared the name's absolute path in the
  current directory, so `claude_bin` always differed. Both now keep the bare name. With `backend: codex`, a step's
  report also re-pinned the missing CLI as `claude`; it now keeps `codex`.
- A failed Claude Code session was read as served: its error result's text came back from the chat endpoint as a 200
  reply, and the acting agent's judge scored it. Now it is no reply: the endpoint answers an error status, an acting
  task is an error record (the workspace agent's with its tokens and log kept; through the acting endpoint the record
  has neither), and an agent Evolver's session with no result commits nothing.
- Claude Code's usage left out prompt-cache writes and later results of a session with background sub-agents; a resumed
  session's cost counted its earlier invocations.
- A Trainer's stop now ends an Evolver's chat call in flight (it ran to the session's clock), and what a failed chat
  call spent is charged.
- A lost sign-in with a long failure text reached a method as an ordinary HTTP error: the endpoint named it after the
  message, which the client's error text cuts at 500 characters. The code now comes first, and the client reads the
  body.
- The login plan's files were read and written through whatever stood at their names: a link a session left in the
  shared directory turned a host process's write into one on the link's target, a fifo blocked a reader, a report of
  the wrong shape stopped every session. They are now read and written only as regular files of the plan's own (what a
  session left is replaced, never followed), their values are checked (non-finite numbers ignored, resets clamped to 31
  days, `SLOTS` to 0 to 1,024), `plan.json` is replaced whole, and a writer gives the plan's lock up after two minutes.
- An agent Evolver dropped an explicit `plan_max_wait_s: null` (no limit) and waited the default 12 h.
- Harness²'s editor sessions on a login asked for the one JSON result object, so neither the payer check nor the plan's
  report saw them; they now print the whole event stream. A session the plan never started crashed the channel.
- EvoSafeHarness's per-role Claude Code sessions turned a lost sign-in into a failed step, and the run went on; it now
  ends the run. They also took Claude Code's refusal for an ordinary failure and retried it. The RRSI Adapter's roles
  and the AHE Agent Debugger retried a session no retry can change (a refusal, a prompt too long), and the Debugger
  took `answer.md` from a session that failed. After a lost sign-in the Debugger still started every queued job's
  session, and it turned a setting Claude Code refuses into every job's fallback answer: both now end the stage, and
  `setup()` checks both agents' efforts. The RRSI Adapter took the proposal a failed proposer session left (Codex's as
  well); it is not taken now, and the step fails, as upstream's driver stops on a failed call.
- The `run_agent` tool says why a run failed (`error`), which Harness²'s records quote; before, they said only that the
  execution failed. Harness²'s `plan_max_wait_s: null` waited 12 h, where it means no limit everywhere else; the
  default is now written out (43,200 s).
- Tests copied the user's own sign-in (`~/.codex/auth.json`, `~/.claude/.credentials.json`) into a session's home for
  the length of a call. No test does now: a test that copies one names its own.
- The AHE Adapter Evolver took a `change_manifest.json` an earlier step left for the current step's: a manifest
  naming the next iteration was refused at its own step, then archived as the next step's when that step's session
  wrote none. The file (or a directory a session made in its place) is now removed before each Evolve session.
- Nine example launchers left their wire meters running after the run (#82), so a later run on the same port
  reused one and wrote its records into the earlier run's ledger. The meters a launch starts now stop within a second
  or two of the runs that use them ending: its own trainings (and, for ServeLearnBench, the tests after them) and
  those of any launch that reuses one, which each list their processes in the port's users file
  (`/tmp/hermit-wire-meter-<uid>-<port>.users`); the harness2 restart loop is stopped with its meter. A
  launch reuses a running meter only if it is still up three seconds on, so it never takes one that is being
  stopped. A meter started on a ledger an earlier one left now carries its totals on (`LEDGER_RESUME=1` in
  `hermit/runtime/wire_meter.py`, which these launchers set): the next run's meter on the port (`launch.sh run &&
  launch.sh run eval`, a ServeLearnBench `test` run again, a CL-Bench tag refused because it exists) wrote an empty
  ledger over the earlier run's bill.
- On macOS a CLI session stopped at its timeout or on cancel could leave processes behind or fail: the processes
  below it were found through `/proc`, which macOS lacks, so one in a session of its own (as a sandbox starts its
  program) was never stopped; and signalling a process group left with only exited, unreaped processes raises
  `PermissionError` there. The processes are now found with `ps` where there is no `/proc`, and that error means the
  group has ended. The same holds for the AgentDyn runner's and the Lean swarm launcher's stops.

## 0.2.0 - 2026-11-04

Three-layer agents, unified adapter deployment, Codex plan-backed acting and learning, and six new method
integrations: ReHarness, Meta-Harness Adapter, ACE, AHE Adapter, RRSI Adapter and EvoSafeHarness.

### Breaking

- The adapter layout changed: a learner may change every file of an adapter except `.git/` and the metadata, and
  every other limit comes from the template (`structure`, `check`) or the Evolver. The metadata moved into `.hermit/`
  (`adapter.yaml`, `EVOLVER.md`, `CARD.md`, the template's check file), the exports moved from the manifest's
  `exports` section into `exports.yaml` at the adapter root, learnable unless the template fixes it, and the
  `origin` section is gone. Hermit no longer loads an adapter in the 0.1 layout (`adapter.yaml` at the root),
  including the one a 0.1 `hermit convert` plugin embeds; it names the layout and the steps instead. Migrate with
  the steps in
  [Migrating from Hermit 0.1](https://infini-ai-lab.github.io/hermit/docs/framework/adapter-manifest.html#migrating-from-hermit-0-1),
  then rebuild converted plugins. The built-in templates' `contract` is `"2"`.
- One acting agent per coding-agent CLI: the `codex` and `claude-code` agents (`hermit/agent/acting/workspace.py`)
  take a task's functions (`prepare`, `prompt`, `answer`, `collect`, as `file.py:function`), a
  container per session (`isolation: container`; `docker/codex/` builds the image and the egress proxy), a ChatGPT
  login (`login: chatgpt`), retries of runs the provider could not serve, and a fixed record: `RunOutput.log` is the
  run's `log/` (the host's event stream unchanged, the prompt, the last message, the deployment), the workspace is
  `work/` beside it, and `RunOutput.extra` has the same keys for every task (`timed_out`, not `session_timed_out`).
  A Codex session has a home of its own, so the user's Codex settings no longer apply, and its commands see only
  Codex's core variables (no `PYTHONPATH`, no proxy, no keys) unless `shell_env: all`; the generic CLI agent passed
  every variable before. OfficeQA's two Codex wrappers
  are gone: `agent.params.harness: codex` (SkillOpt's own Codex harness: the skill as `SKILL.md`, 180 s sessions,
  up to 24) and `utils/agent/codex_agent.py`, with `codex_sandbox.py` and `codex_session.py`; every OfficeQA Codex
  config is the Codex agent with `utils/task.py`'s task functions, one session of up to 3,600 s with the skill as
  `AGENTS.md`. The Codex-harness numbers published for no skill, the initial skill, GEPA and SkillOpt were made with
  SkillOpt's harness (the recipe says which harness made each row). The `OFFICEQA_CODEX_*` variables are
  `agent.codex.container` (or `HERMIT_CODEX_IMAGE`, `HERMIT_CODEX_NETWORK`, `HERMIT_CODEX_PROXY`).
- Agents are built from three layers ([Agent layers](https://infini-ai-lab.github.io/hermit/docs/framework/agent-layers.html)):
  the model (`hermit/agent/model/`), a Base Agent (`hermit/agent/base/`: `direct`, `react`, `codex`, `claude`) and
  the Acting or Evolver Agent (`hermit/agent/acting/`, `hermit/agent/evolver/`, the methods in
  `hermit/agent/evolver/methods/`); the adapter templates are in `hermit/adapter/templates/`. `hermit.backends`,
  `hermit.llm`, `hermit.trajectory`, `hermit.agents` and `hermit.evolver` forward to the new modules for one release
  and warn; `hermit.algorithms` and `hermit.tools_mcp` are gone.
- The Codex and Claude Code agent Evolvers get their tools from the framework's tool server over MCP (streamable
  HTTP, a token in the URL), no longer from a stdio server that forwarded to the Trainer's endpoint; with `tools`
  set, the task message names no endpoint URL. Codex runs with a `config.toml` of the run's own in its home (no
  `--ignore-user-config`), and an Evolver's copied sign-in is removed after each call rather than at the end.
- The `claude-code` acting agent runs with a Claude Code home of its own (`CLAUDE_CONFIG_DIR`, the user's
  `.credentials.json` copied in for the session), so the user's Claude Code settings no longer apply, as for Codex.
- The `codex` and `claude-code` acting agents and the agent Evolvers refuse to run without `web_search` set to
  `true` or `false` (it was off when absent); the built-in Evolvers' defaults set it.
- A ChatGPT login (`login: chatgpt`) has one shared copy for every session, `~/.cache/hermit/codex-login/`, instead
  of one per run's workspaces; sessions in the rollout sandbox get it from outside and hand their refreshed token
  back (also when the call was stopped), so a rotated refresh token reaches the next session. The copy takes only a
  later refresh of a login: a file with a `last_refresh` ahead of now, or no login at all, is left out.
- OfficeQA configs that use the Codex agent are written in the per-layer form; a command-line override of the flat
  `agent.params.model`, `login`, `effort`, `web_search`, `timeout_s`, `isolation` or `container` on them is refused
  as set twice. Override `agent.model.*` and `agent.codex.*` instead.

- One deployment for every acting agent: the `codex` and `claude-code` agents deploy an adapter as `hermit deploy`
  does, and `agent.params.deploy: tree` and `files` are gone (a config that sets them is refused; remove the line),
  with `agent.params.reserved` and `hermit/agent/acting/deploy.py`. Each export goes to the host's own place when the
  host has one; an export it has none for stays a file of the adapter's copy under `hermit/<id>/` and the context
  names it (Pi's roles and hooks, which `--host pi` used to refuse), and the context lists the copy's other files,
  so nothing an adapter holds is out of the agent's reach. An adapter whose context is per task (a `context_tool`) is
  deployed with the task's context; the session calls the deployment's tools without approvals and may start its
  sub-agents. Paths change with it: the adapter's files are under `hermit/<id>/` rather than at the workspace root
  (the context says so), Codex finds skills in `.codex/skills/` and roles in `.codex/agents/`, and Continual
  Harness's memory is `hermit/continual-harness/memory/NOTES.md`. OfficeQA's Codex numbers published before this
  were made with the old deployments. `exports.yaml` takes `internal`: the harness's own files (ReHarness's bank and
  retrieval), neither copied nor named. The `continual-harness`, `harness2-codex` and `reharness` templates are 1.1
  (their fixed files changed): a tree made from 1.0 refuses them, so a new run of these templates needs a new
  `trainer.tree_dir` (or `output_dir`).
- The Codex deployment refuses an `AGENTS.md` longer than Codex reads (`project_doc_max_bytes`, 32 KiB unless set),
  which Codex would cut without a word; `hermit deploy --context-limit` and the agent's `project_doc_max_bytes` set
  the limit, and the OfficeQA Codex configs set 64 KiB.

### Added

- A release workflow with Linux core and sandbox checks, distribution builds, an installed-wheel smoke and
  tag-triggered PyPI Trusted Publishing; `docs/developer/releasing.md` records the repeatable release process.
- An Evolver can choose the next step's tasks: one that sets `chooses_tasks = True` is given the epoch's training
  tasks still to run (`Progress.remaining`, also listed in the call's message) and names the next step's in
  `StepResult.next_tasks` (an external program in `output/step_result.json`). An epoch still runs every training task
  once; a request outside that is refused and recorded, and the next step takes the Trainer's order, which stays the
  default. Other Evolvers see nothing ahead and get the same steps and messages as before. Every step record now
  carries `epoch_end`, which the epoch checkpoint follows, and, when a choice was made, `next_tasks` or
  `next_tasks_refused` and `chosen_by`.
- A ChatGPT login's plan, for every Codex session on it (`hermit/agent/base/login.py`): files under
  `~/.cache/hermit/codex-login/plan/` pause new sessions (`PAUSE`), hold them past a usage threshold (`MAX_USED`) and
  cap the sessions at once on the machine (`SLOTS`); each session's report of the plan's usage is kept (`plan.json`);
  a session the plan refused is run again once the window resets, never scored; "model is at capacity" is a session
  the provider could not serve. A session on the login also runs without apps, plugins or browsers. Each window
  that has not reset counts on its own; a session takes a slot and then checks the plan again; one held back past
  `plan_max_wait_s` (12 h) is unavailable rather than waiting on; the plan directory is the machine's one
  (`plan_dir`), shared into the rollout sandbox.
- `llm: {cli: codex, isolation: container, ...}`: the run's model reached through Codex, for a model only it serves (a
  ChatGPT login). The Trainer answers each chat call with one session of that Base Agent behind a `ChatEndpoint`
  (`hermit/agent/base/endpoint.py`: system messages as instructions, the rest on stdin, the last message as the reply
  with its usage). A container session takes its prompt on stdin too.
- Harness²'s editors on Codex: `evolver.params.backend: codex` (with `login: chatgpt` for a model on the login); the
  editors are sessions of the `codex` Base Agent (Claude Code's effort `max` is Codex's `xhigh`), and `claude_bin`
  pins either CLI. Codex instructions too long for one command-line argument go at the top of the prompt.
- The OfficeQA Flash row with GPT-5.6-Luna: the seven methods with Codex 0.164.0 and GPT-5.6-Luna acting and learning
  on a ChatGPT login, a second column of the results table in `examples/officeqa/README.md` and a section of the
  recipe. Each method's `run_codex_luna.yaml` / `stream_codex_luna.yaml` is its Flash-row config with the models
  changed; `docker/codex/Dockerfile.standalone` builds the image with a machine's standalone Codex;
  `harness2_stream/launch.sh` takes `CONFIG=run_codex_luna.yaml`. (Ported from PR #54 onto the agent layers.)
- The EvoSafeHarness method: the `evosafe` Evolver and `evosafe` template search a defense bundle (`ADAPTER.md`
  prompt + `defense.py` tool hooks) for a frozen victim with Designer, Criticizer and Analyzer roles (Claude Code,
  Codex or the run's `llm`), full scoring on training cases and optional validation confirmation; documented in
  `docs/methods/evosafe.md`. Its AgentDyn integration (`examples/agentdyn/utils/`, run configs in `examples/agentdyn/evosafe/`)
  replays existing frozen benign and injected cases with the native judges. The roles run on the `claude` or
  `codex` Base Agent and every AgentDyn model call on `direct`; the defense metrics are computed by the method
  from run records (`examples/agentdyn/utils/report.py`). Role search gives the Designer explicit UTF-8 file budgets
  (`defense.py` 32,000 bytes by default, capped by the template), repairs specification failures before a review,
  and gives each candidate at most three Criticizer calls, then a final Designer repair that is evaluated once the
  hard checks pass; such an unapproved final revision is disclosed, and the promotion gates are unchanged. No core
  change.
- The Base Agent API (`hermit.agent.base.interface`): `make(name, settings, model).run(prompt, workspace, tools,
  limits, session, roles) -> Result`, `configured(**settings)`, `Tool` and the tool server, and `Limits`, which a
  Base Agent enforces or refuses (`Unsupported`). The `react` agent's loop is the `react` Base Agent; the `codex` and
  `claude-code` acting agents and the agent Evolvers run the `codex` and `claude` Base Agents.
- `ctx.agent`: the run's model as a `direct` Base Agent, metered against the budget, for every Evolver;
  `hermit.agent.evolver.base.ask(ctx.agent, messages, **sampling)` replies as the `llm` tool does. GEPA, SkillOpt,
  ReHarness and Continual Harness call it; the `llm` tool calls the same agent.
- The per-layer form of `agent` and `evolver` in the run config: `base`, `model` and a section named after the base
  (`codex: {...}`), read into the flat form, which still works.
- Every method and benchmark runs on the layers. CL-Bench's runner and ServeLearnBench's agent reach their model
  through a local endpoint over `direct` (`hermit/agent/base/endpoint.py`), which relays each request unchanged to
  the meter or backend it would have reached (a streamed request is asked whole and streamed back; an error keeps its
  status, whole body and Retry-After, also for a stream that has not started; an upstream that gave no answer leaves
  none; a client that hangs up has its call given up upstream). MWM's learner is
  one call of the `codex` Base Agent with MWM's native mounts as its sandbox; each Harness² session is one call of the
  `claude` Base Agent. The tool gate (`hermit.agent.evolver.gate`) and MWM's stdio `learner_tools.py` are gone: the
  call's tool server serves the allowed tools over MCP and as JSON (`MW_TOOLSRV`).
- OfficeQA's direct chat makes each of SkillOpt's model calls through the `direct` Base Agent, with SkillOpt's own
  retry waits (`ChatClient` takes `retry_wait` and `retry_wait_max`).
- `hermit deploy` copies the whole adapter (every file but `.hermit/` and `.git/`) to `hermit/<adapter id>/` in the
  workspace, and the context block says so, with paths in `ADAPTER.md` relative to that folder; what `exports.yaml`
  declares is connected to the host as before. A redeployment replaces the copy, and refuses when a copied file was
  changed or replaced since or another file is in the way. Training with the built-in Codex and Claude Code agents deploys the
  same way.
- A `hooks` export: a JSON file in Claude Code's hook schema, deployed to `.codex/hooks.json` or the `hooks` of
  `.claude/settings.json`, each command run from the adapter's copy; Pi refuses it. The built-in Codex agent trusts
  the hooks its task's deployment wrote (`codex exec` skips untrusted project hooks without a word). No plugin
  carries hooks yet: `hermit convert` refuses an adapter that has them, or with `--allow-lossy` lists them under
  `omitted`.
- `agents` also reads `<name>/AGENT.md` subagents (named by their directory). The `continual-harness` and
  `harness2-codex` templates declare their skills and subagents, and `harness2-codex` its hooks, so a deployment
  connects them.
- `hermit.agent.base.trajectory` (then `hermit.trajectory`): the one reader of the hosts' logs (Codex's `--json` events, Claude Code's stream-json) as
  steps. SkillOpt builds its Codex trace summary from them and GEPA a text of them; ReHarness and Continual Harness
  read them instead of parsing Codex's stream each on its own.

- The AHE Adapter method (Lin et al., arXiv:2604.25850): the `ahe_adapter` Evolver, AHE's iteration of k rollouts
  per task, the attribution of the Evolve Agent's change manifest at the next iteration and the file-wise rollback of
  HARMFUL changes, with the Agent Debugger and the Evolve Agent as sessions of the `codex` Base Agent and upstream's
  queries and attribution rules verbatim; documented in `docs/methods/ahe_adapter.md`.
- The `ahe` adapter template: a system prompt, scripts, a middleware module run as the host's hooks by a fixed hook
  runner, skills, sub-agents and a long-term memory, the same files for Codex and Claude Code; each host's hook
  wire and what it can and cannot do are the template's `hook_hosts.py`, and the template check deploys a copy to
  both hosts and runs every hook as the host runs it.
- AHE Adapter on OfficeQA: `examples/officeqa/ahe_adapter/`, in the GPT-5.6-Luna setting (`run_codex_luna.yaml`).
- The ACE method (Zhang et al., ICLR 2026, arXiv:2510.04618): the `ace` Evolver, ACE's per-sample loop of
  Reflector and Curator over a playbook of bullets with helpful and harmful counters, retry rounds on a wrong answer,
  ADD-only curation and validation-based selection, with ACE's prompts verbatim (the Generator's adapted for an
  acting agent); the Reflector and Curator run through the run's `llm` tool; documented in `docs/methods/ace.md`.
- The `ace` adapter template: an `ADAPTER.md` whose playbook section holds the bullets, after the skill.
- ACE on OfficeQA: `examples/officeqa/ace/`, in the GPT-5.6-Luna setting (`run_codex_luna.yaml`). The run's `final` playbook is included as `examples/officeqa/ace/adapter/`, with its card.
- The ReHarness method (Xiao et al., arXiv:2610.12668): the `reharness` Evolver, one step per instance of an ordered
  stream, revising the retrieved reflections from their source trajectories and writing the instance's reflection
  through the run's `llm` tool, with Qwen3-Embedding-8B or BM25 retrieval; `feedback: false` keeps the judge's verdict
  out of what the agents read, for a stream on the label-free footing; documented in `docs/methods/reharness.md`.
- The `reharness` adapter template: a bank of reflections with a reader that retrieves the top-k for each instance as
  the adapter's per-task context.
- ReHarness on the CL-Bench examples: `examples/clbench_database/reharness/` and `examples/clbench_poker/reharness/`
  (host, learner and embedding meters; one run per order); the hosts read the adapter's context per instance, record
  what the context tool retrieved, and fall back to `ADAPTER.md` when it fails.
- ReHarness on OfficeQA: `examples/officeqa/reharness/` (direct chat, and the Flash row's `stream_codex.yaml` and
  `run_codex_flash.yaml`); the OfficeQA Codex agent deploys a context adapter as the session's `AGENTS.md`, the
  Session's context for the question, and records what the adapter's context tool retrieved.
- `context_tool` in `exports.yaml`: a declared tool the harness calls once per task with `{query, task}`,
  whose text is appended to the context (`Session.context(query=..., task=...)`, `session.last_context`); hidden from
  the agent's tools, called by the deploy check with an empty query, refused by the CLI agents; the `react` agent
  records its result as `RunOutput.extra["context_tool"]`.
- `plugin-portable` in `.hermit/adapter.yaml`: a template that sets it to `false` keeps its adapters inside hermit;
  `hermit deploy` and `hermit convert` refuse them, with the reason, before writing anything. A template whose
  adapters declare a `context_tool` must set it; the `reharness` template does.
- `UPSTREAM_KEY_ENV` for the wire meter: a request that arrives without an `Authorization` header is signed with
  that variable's key, for a sandboxed reader that holds none.
- The Meta-Harness Adapter method: the outer loop of Meta-Harness (Lee et al., arXiv:2603.28052) with the search
  confined to the adapter (the acting agent is fixed). The `meta_harness_adapter` Evolver, one candidate per step, a
  Codex or Claude Code proposer that reads every earlier candidate's files, scores and trajectories and writes k new
  adapters per round, and `finalize` handing back the best mean on the search set; documented in
  `docs/methods/meta_harness_adapter.md`.
- The `meta-harness-adapter` adapter template: every file outside `.hermit/` is learnable; `.hermit/EVOLVER.md` says
  only what an adapter is and that `ADAPTER.md` always reaches the acting agent.
- Meta-Harness Adapter on OfficeQA: `examples/officeqa/meta_harness_adapter/` (the Flash row, six rounds of two
  candidates), with the Codex agent's `deploy: files`, which deploys `ADAPTER.md` as the session's `AGENTS.md`
  and every other adapter file at the same path in the workspace.
- The RRSI Adapter method: RRSI (Xia et al., arXiv:2609.24972), regularized recursive self-improvement, on the
  Trainer with the harness confined to the adapter (the acting agent is fixed). The `rrsi_adapter` Evolver evaluates
  one candidate per step on the evolve set (k rows per task) and leaves every other decision to upstream's code at a
  pinned commit (the `rrsi` dependency group): the annealed edit budget, the edit history, exploration and pruning,
  the analyst and its digesters, the leakage critic, delta's calibration, the noise-adjusted floor,
  the cost rule and the selection. The proposer is a session of the `codex` or `claude` Base Agent, what it must not
  see the call's `Limits.hidden`; the analyst, its digesters and the critic are sessions of the `codex` Base Agent,
  one per call.
  Documented in `docs/methods/rrsi_adapter.md`.
- The `rrsi-adapter` adapter template: every file outside `.hermit/` is learnable; `.hermit/EVOLVER.md` names RRSI's
  component vocabulary and what each component's edit must show in the diff.
- RRSI Adapter on OfficeQA: `examples/officeqa/rrsi_adapter/` (the GPT-5.6-Luna column: ten rounds of two candidates,
  each training question twice), made on the agent layers (four trainings: 0.616 on the 172 test questions).
- `docs/framework/adapter.md` states what every adapter can rely on: `.hermit/` is fixed and `ADAPTER.md` reaches
  the acting agent.
- `docker/offline.sh`: the examples in a container whose only route out is an egress proxy to the model provider.

### Changed

- `run_agent`'s results also say how a session ended when the pipeline reports it (`exit_code`, `timed_out`: the
  Codex and Claude Code agents do).
- The `codex` and `claude` Base Agents take `plan_rerun: false`: a session a ChatGPT plan refused before it finished
  a turn is returned (`extra["refused"]`) instead of run again in the same workspace.
- Hermit requires Python 3.12+ (was 3.11+): the upstream Harness² package of the `harness2` group supports Python
  3.12 and 3.13 only, so `--group all` failed on Python 3.11.
- The optional Evolvers' dependencies are dependency groups in `pyproject.toml` (`gepa`, `skillopt`, `harness2`,
  and `all` for every method), with the same Git commit pins. Install them from a checkout with
  `python -m pip install -e . --group <name>`, which needs pip 25.1+. The groups are not package extras:
  `pip install hermit-harness` installs only the core.
- An Evolver whose dependencies are missing names both the group command and the pinned requirements, so a user
  without a checkout can install them.
- The wire meter listens on `BIND` (default `127.0.0.1`; it listened on every interface before), and a relay that
  signs requests refuses any other address.

### Removed

- `requirements.txt`, `requirements-gepa.txt`, `requirements-skillopt.txt` and `requirements-harness2.txt`. Replace
  `python -m pip install -e . -r requirements-<name>.txt` with `python -m pip install -e . --group <name>`; the core
  dependencies of `requirements.txt` are in `pyproject.toml`.

### Fixed

- Exclude a Git worktree's root `.git` pointer file from workspace deployment, as for a root `.git/` directory.
- Include MWM's hidden pattern-library resource `.lint_blacklist` in installed packages, and check packaging
  coverage for every bundled method file as well as adapter templates.
- EvoSafeHarness's Codex roles no longer require Daybreak by default; it is an explicit opt-in. The AgentDyn
  documentation clarifies task-dependent utility judging and reports completed episodes and errors.
- The chat endpoint closed each connection after its reply without saying so (`Connection: close`); an HTTP/1.1
  client kept the connection for its next request and got a dropped connection instead of the answer, now and then
  (`tests/core/test_base_agents.py`'s error test failed so, about one run in three under load).
- The Base Agent read and wrote a session's home, and its event stream, through what a session could have left
  there: after a session, a linked `auth.json` with a later refresh became the machine's shared login copy, a linked
  `usage/` received the usage ledger (a pipe there blocked the call), a linked last-message file was read as the
  reply, and a linked stream as the session's events; before the next session in a kept home, its config, the
  sign-in copy and the login's temporary copy were written through links left there; a rollout's login copies were
  taken back and deleted through linked directories (`isolate.py`). None of these follows a link now, a usage
  ledger the session rewrote (malformed, or over what Codex reports) is not taken for the thread's earlier total,
  and a login for another ChatGPT account than the shared copy names is never taken back. The stream is read back
  only from the file the Base Agent opened for it, through no link a session left on the way; the caller's own paths
  (`log`, `stderr_log`: a device, a pipe, a link) are opened as they are. Harness²'s channel reads its session's stream and stderr file the same way.
- `unavailable` and a ChatGPT plan's refusal were read from the whole stream, so a session whose own work quoted an
  outage or a usage limit (a log it searched) was retried as the provider's failure or held for the plan. They are
  read from the CLI's own messages and error events now; `served` is read from the events, not the text, and the
  result says `extra["refused"]` (never for a session that was served).
- A hidden path (`Limits.hidden`, `AgentEvolver`'s `hide`) was covered under its real name only, so a place the
  sandbox shows under a link's name (a workspace reached through a link, a venv under a linked cache) left it
  visible there; and hiding the framework's `examples/` covered a run kept below it, workspace and all. A hidden path
  is now covered under every name the sandbox shows it by, and the places the session is given (its writable and
  readable paths, the task description) are bound back on top of a covered directory, shallowest first and under
  each of their names, with what is hidden below them (a hidden path, the sandbox's masks) covered again after them
  (`hermit/sandbox.py`, `HidingSandbox`). A place the call gives (its `readable`, kept visible past `hidden` as
  documented, and its writable paths) or keeps stays visible when it is also hidden; one only the sandbox gives
  stays hidden. A path reached through a link inside a place the session can write has what it leads to covered
  only inside a place the session is given, never a place itself, the system, the interpreter or the sandbox's own
  files: a session that can write where a method hides every entry cannot choose what the next call loses or stop
  it from starting.
- The sandbox read `pyvenv.cfg` in the places a session can write and showed the installation its `home` named in
  the session's next call: a session could have any directory of the host shown read-only. Only places shown
  read-only are read now (a venv made inside the sandbox is made from an installation it shows already).
- The Trainer dropped the usage an Evolver call reported when the hard budget stopped it; it is counted.
- Codex's trajectory left out failed turns, web searches, to-do lists and an MCP call's arguments and result (the
  result is the step's `output`), and a log directory whose stream had another name (a Harbor trial's
  `agent/codex.txt`) gave no steps.
- A chat call through `llm: {cli: ...}` left its Codex event stream as a file in the temporary directory; the
  Trainer keeps it under the run's `llm_sessions/`, and a call without a log directory removes it.
- A CLI call without a `home` setting gets a fresh home under `$HERMIT_HOMES` (default `~/.cache/hermit/homes`), not
  in the temporary directory: Codex makes no helper programs for its sandbox in a home there, and no command of such
  a session ran.
- A resumed Codex session (the `codex` Evolver with `evolver.resume: true`) was charged its earlier steps again:
  Codex reports a thread's running total, which was counted as the step's usage. Each step now counts only what it
  added.
- The trajectory of a Codex session left out its sub-agent calls (`spawn_agent`, `wait`): they are `tool_call` steps
  now, with the prompt the sub-agent was given and its state.
- `packaging` is a core dependency. Without it, the sandbox's dependency closure dropped the packages a requirement's
  extras bring (`cryptography` through mcp's `pyjwt[crypto]`), so in an environment with only the core installed,
  `hermit.tools_mcp` and `hermit.runtime.mcp_server` failed to import inside the sandbox: the Codex and Claude
  Evolvers' tool server and a sandboxed `Session`'s adapter server did not start.
- The sandbox failed to bind uv's version-alias link when the managed interpreter lives inside a granted checkout.
- The sandboxed adapter server lost its channel on a tool result over asyncio's 64 KiB line default.

## 0.1.0 - 2026-10-25

The first release: the framework as described in the documentation, with the Harness² and Continual Harness
reproductions and the OfficeQA Flash row.

### Added

- Harness² under the paper's protocol by the upstream code (`harness2_upstream`, `harness2-codex@1.0`,
  `examples/officeqa/harness2_stream/`), with Codex as the acting agent.
- Continual Harness (`continual_harness`, `continual-harness@1.0`, `examples/officeqa/continual_harness/`): the
  paper's four components on Codex's tree, refined from trajectories; a memory file for Codex sessions.
- The OfficeQA Flash row: every method on one row of settings, with GEPA and SkillOpt as means of four trainings.
- The developer guide for reproducing a published method.
- `requirements-gepa.txt`, `requirements-skillopt.txt` and `requirements-harness2.txt`: the Git-pinned packages of
  the optional Evolvers, formerly the `gepa`, `skillopt` and `harness2` extras (PyPI does not accept direct references).
- A PyPI description (`README-pypi.md`); the package ships the templates' dot-directories.

### Changed

- The repository is `Infini-AI-Lab/hermit`.
- The Codex session of the OfficeQA harness runs as the host user (Codex 0.162).
- Require setuptools 77+ for the SPDX license metadata and declared license files.
- Exclude Python bytecode caches from packaged adapter templates and Evolvers.
- Include repository, documentation, issue, and changelog links in package metadata.
- Exclude local design drafts from default documentation builds while retaining them in the local preview.
- Describe MWM bootstrap as step 0 before the first rollout, matching the implementation.

### Added

- Contribution and security-reporting guides, software citation metadata, Issue and PR templates, and optional
  pre-commit file checks.

### Development baseline (before 0.1.0)

- Adapter templates and specifications, task execution, an Evolver interface, and a Trainer with learning records,
  version history, budgets, and recovery.
- GEPA, SkillOpt, MWM, and agent-based learning integrations.
- Workspace deployment and native plugin export for supported hosts and adapter contents.
- Offline and real-model tutorials, benchmark recipes, and a finished Lean collaboration adapter example.

Consult each recipe and adapter card for its evaluation protocol and observed results.
