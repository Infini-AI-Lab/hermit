<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/wordmark-dark.svg">
  <img src="assets/wordmark-light.svg" alt="MetaHarnessAdapter" width="540">
</picture>

**Train harness adapters for existing agents**

[Installation](docs/get-started/installation.md) · [Quick Start](docs/get-started/quickstart.md) · [Overview](docs/framework/overview.md) · [Commands](docs/framework/commands.md) · [Configuration](docs/framework/configuration.md) · [Custom Evolvers](docs/framework/custom-evolver.md) · [OfficeQA recipe](docs/recipes/officeqa.md) · [Examples](examples/README.md)

</div>

---

A **harness adapter** is a portable harness that attaches to an existing agent (Codex, Claude Code, or your own) from
the outside: context, MCP tools, native roles and skills, kept as an ordinary directory. MetaHarnessAdapter trains
adapters the way a training framework trains weights:

| Part | Role | Like |
|---|---|---|
| Adapter template | The structure and its specification (what is learnable, what is fixed) | the initial model |
| Evolver | The learning algorithm, a learner agent's harness (prompt-only, Codex/Claude agent, GEPA, ...) | the optimizer |
| Trainer | The outer loop: data, rollouts, learning material, checks, git, budgets, records | the trainer |
| Evolving task | Your task-info, data, judge and acting-agent pipeline | data + reward + rollout |

```bash
pip install -e .
harnessadapter check --config run.yaml       # basic checks of the settings
harnessadapter train --config run.yaml       # train; resumes after a crash
harnessadapter eval  --config run.yaml --adapter final
harnessadapter deploy --adapter <dir> --workspace <workspace> --host codex
```

Every adapter is a commit in a git tree rooted at its template; runs are branches, checkpoints and finals are tags,
and each final adapter carries a `CARD.md`. Built-in templates: `prompt-only@1.0`, `lean-collab@1.0`. Built-in
Evolvers: `prompt_only`, `codex`, `claude`, `gepa`, `skillopt`; [`examples/`](examples/README.md) has a folder per Evolver
with its configs for each task.

Tests: `python -m pytest tests` (offline; the sandbox tests need bubblewrap).
