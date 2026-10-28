# Changelog

Changes are grouped by software version. Entries under Unreleased describe work awaiting a tagged release.

## Unreleased

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
