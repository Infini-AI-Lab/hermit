# Implementation status and limits

## Implemented

- Sandboxed PNG/JPEG reads returned as MCP image blocks, with size limits and audit hashes; explicit Codex reasoning-effort override.

- Prompt-only and MWM starting architectures; minimal-shop as a separate integration demo.
- Optional MCP registration, marked context injection, and native role conversion for Codex and Claude Code.
- Prompt-driven Codex or Claude evolution, with a generic preset and an MWM method preset.
- Separate candidate directories, read-only LearningMaterial, optional visible validation, and protected method/history snapshots.
- OS-enforced tool operations: candidate/scratch writes, configurable environment reads, and networking.
- CLI and YAML launch, source fingerprints, audit records, and static interface checks.

## Current limits

- The agreed iteration protocol has Adapter and optional Training State outputs. Training State input/output and resume exist for optimizer Evolvers (`--training-state`); learner Evolvers still return none.

- A repository-local prototype, not a published SDK or package.
- One adapter per generated acting workspace; no general merge into existing project settings.
- GEPA and SkillOpt run as optimizer Evolvers through `harnessadapter evolve`; any directory with `evolver.toml` (`kind = "optimizer"`) and a `run(context)` plugin is one too. Reef and marketplace integration are not implemented.
- Automated evaluation exists through an Environment (`harnessadapter evaluate`, and inside optimizer Evolvers); learner Evolvers are not scored automatically. No candidate promotion, scheduled updates, or live process reload.
- File snapshots and hashes are implemented; Git release workflows and complete external-state rollback are not.
- Both learner backends currently need Codex CLI for the framework sandbox executor.
- MWM learned-Python admission requires its separate Linux bubblewrap sandbox. On this Mac it fails closed; general evolution commands use a different sandbox path.

## Troubleshooting

If a host CLI is missing, install and sign in before launching. --prepare-only can inspect generated files without opening the host. For evolution, preflight requires a functioning OS sandbox and will refuse to proceed if enforcement is unavailable.

If context-only deployment shows no MCP server, that is expected. If MWM tools are missing, check host workspace trust and MCP approvals. Review the generated manifest metadata and session control files before assuming a capability was loaded.

The examples use synthetic data. Structural checks, permission tests, and interface integration checks are not evidence of learning performance.
