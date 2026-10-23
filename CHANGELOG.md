# Changelog

Changes are grouped by software version. Entries under Unreleased describe work awaiting a tagged release.

## Unreleased

### Changed

- Require setuptools 77+ for the SPDX license metadata and declared license files.
- Exclude Python bytecode caches from packaged adapter templates and Evolvers.
- Include repository, documentation, issue, and changelog links in package metadata.
- Exclude local design drafts from default documentation builds while retaining them in the local preview.
- Describe MWM bootstrap as step 0 before the first rollout, matching the implementation.

### Added

- Contribution and security-reporting guides, software citation metadata, Issue and PR templates, and optional
  pre-commit file checks.

## 0.2.0 Development Baseline

The package currently declares version 0.2.0. This baseline is not a tagged release in this checkout.

- Adapter templates and specifications, task execution, an Evolver interface, and a Trainer with learning records,
  version history, budgets, and recovery.
- GEPA, SkillOpt, MWM, and agent-based learning integrations.
- Workspace deployment and native plugin export for supported hosts and adapter contents.
- Offline and real-model tutorials, benchmark recipes, and a finished Lean collaboration adapter example.

Consult each recipe and adapter card for its evaluation protocol and observed results.
