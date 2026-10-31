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

### Added

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
  candidates); the OfficeQA Codex agent takes `deploy: adapter`, which deploys `ADAPTER.md` as the session's
  `AGENTS.md` and every other adapter file at the same path in the workspace.
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
