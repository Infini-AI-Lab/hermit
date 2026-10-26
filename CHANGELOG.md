# Changelog

Changes are grouped by software version. Entries under Unreleased describe work awaiting a tagged release.

## Unreleased

### Added

- The ReHarness method (Xiao et al., arXiv:2610.12668): the `reharness` Evolver, one step per instance of an ordered
  stream, revising the retrieved reflections from their source trajectories and writing the instance's reflection
  through the run's `llm` tool, with Qwen3-Embedding-8B or BM25 retrieval; `feedback: false` keeps the judge's verdict
  out of what the agents read, for a stream on the label-free footing; documented in `docs/methods/reharness.md`.
- The `reharness` adapter template: a bank of reflections with a reader that retrieves the top-k for each instance as
  the adapter's per-task context.
- ReHarness on the CL-Bench examples: `examples/clbench_database/reharness/` and `examples/clbench_poker/reharness/`
  (host, learner and embedding meters; one run per order); the hosts read the adapter's context per instance, record
  what the context tool retrieved, and fall back to `ADAPTER.md` when it fails.
- `exports.context_tool` in `adapter.yaml`: a declared tool the harness calls once per task with `{query, task}`,
  whose text is appended to the context (`Session.context(query=..., task=...)`, `session.last_context`); hidden from
  the agent's tools, called by the deploy check with an empty query, refused by the CLI agents; the `react` agent
  records its result as `RunOutput.extra["context_tool"]`.
- `UPSTREAM_KEY_ENV` for the wire meter: a request that arrives without an `Authorization` header is signed with
  that variable's key, for a sandboxed reader that holds none.
- `docker/offline.sh`: the examples in a container whose only route out is an egress proxy to the model provider.

### Changed

- The wire meter listens on `BIND` (default `127.0.0.1`; it listened on every interface before), and a relay that
  signs requests refuses any other address.

### Fixed

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
