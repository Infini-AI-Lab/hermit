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

### Changed

- Hermit requires Python 3.12+ (was 3.11+): the upstream Harness² package of the `harness2` group supports Python
  3.12 and 3.13 only, so `--group all` failed on Python 3.11.
- The optional Evolvers' dependencies are dependency groups in `pyproject.toml` (`gepa`, `skillopt`, `harness2`,
  and `all` for every method), with the same Git commit pins. Install them from a checkout with
  `python -m pip install -e . --group <name>`, which needs pip 25.1+. The groups are not package extras:
  `pip install hermit-harness` installs only the core.
- An Evolver whose dependencies are missing names both the group command and the pinned requirements, so a user
  without a checkout can install them.

### Removed

- `requirements.txt`, `requirements-gepa.txt`, `requirements-skillopt.txt` and `requirements-harness2.txt`. Replace
  `python -m pip install -e . -r requirements-<name>.txt` with `python -m pip install -e . --group <name>`; the core
  dependencies of `requirements.txt` are in `pyproject.toml`.

### Fixed

- `packaging` is a core dependency. Without it, the sandbox's dependency closure dropped the packages a requirement's
  extras bring (`cryptography` through mcp's `pyjwt[crypto]`), so in an environment with only the core installed,
  `hermit.tools_mcp` and `hermit.runtime.mcp_server` failed to import inside the sandbox: the Codex and Claude
  Evolvers' tool server and a sandboxed `Session`'s adapter server did not start.

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
