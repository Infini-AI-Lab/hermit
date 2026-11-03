# Changelog

Changes are grouped by software version. Entries under Unreleased describe work awaiting a tagged release.

## Unreleased

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
- `docs/framework/adapter.md` states what every adapter can rely on: `.hermit/` is fixed and `ADAPTER.md` reaches
  the acting agent.
- `docker/offline.sh`: the examples in a container whose only route out is an egress proxy to the model provider.

### Changed

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
