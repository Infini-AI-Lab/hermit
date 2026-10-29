# Extend and contribute

Start with the extension boundary you need. A task integration normally requires no Trainer change.

| Change | Entry point | Verify |
|---|---|---|
| New task, acting agent, or judge | [Bring your task](../tutorials/your-own-task.md), [task contract](../framework/task.md) | No-adapter probe, adapter consumption, split isolation, logs, scoring |
| New instruction/skill/tool structure | [Customize an adapter](../tutorials/customize-an-adapter.md), [adapter metadata](../framework/adapter-manifest.md) | Learnable/fixed rules, checker, exported schemas |
| New learning method | [Evolver tutorial](../tutorials/custom-evolver.md), [full contract](../framework/custom-evolver.md) | Failure handling, state persistence, usage, final selection |
| Reproduction of a published method | [Reproduce a published method](reproducing-a-method.md) | The adapter and the Evolver named, the differences from the authors' code, the protocol, the first evaluation on the shared settings |
| New host exporter | `hermit/hosts/`, `hermit/plugins/` | Supported exports, refusal/omission behavior, repeat generation |
| Trainer behavior | [Trainer contract](../framework/trainer.md), `hermit/trainer/` | Rollback, resume boundaries, records, budget observations |

## Repository map

| Location | Responsibility |
|---|---|
| `hermit/cli.py`, `config.py` | Command parsing and config resolution |
| `hermit/agents/` | Built-in acting agents and the Agent/function interface |
| `hermit/task.py` | Task records, callable loading, pipeline/judge types |
| `hermit/adapter/` | Manifest loading and acting consumption |
| `hermit/algorithms/templates/` | Built-in adapter seeds and specifications |
| `hermit/algorithms/evolvers/` | Built-in learning methods and defaults |
| `hermit/evolver/` | Shared learner contracts and program/agent execution |
| `hermit/trainer/` | Training loop, rollouts, material, budgets, recovery |
| `hermit/runtime/` | Tool serving, sandboxing, and runtime support |
| `examples/<task>/utils/` | Task integrations and benchmark-owned acting/scoring logic |
| `tests/` | Framework regression tests |
| `docs/_examples/` | Small offline examples included in documentation |

## Local development

```bash
python -m pip install -e '.[test]'
python -m pytest tests/test_training.py tests/test_spec.py tests/test_deploy.py
```

Choose tests for the changed contract. Optimizer tests need the method's dependency group (for example `python -m pip install -e . --group gepa`) and skip
without it; sandbox and MWM integration
tests can require Linux, learner CLIs, external packages, or environment fixtures. Inspect their prerequisites
rather than interpreting every skipped test as coverage.

For a host exporter change, run `tests/test_convert.py` and/or `tests/test_deploy.py`. For a new Evolver, use an
offline task or fake client to test real failure and state behavior without paid model calls.

## Documentation changes

```bash
python -m pip install -r docs/requirements.txt
bash docs/build.sh
bash docs/serve.sh 8000
```

The build treats warnings as errors. Link new pages from `docs/index.md`, use repository-relative
`literalinclude` for code and defaults, and execute walkthrough commands in a fresh run directory.

Every recipe should state its prerequisites, data preparation, model setup, launch command, output locations,
evaluation protocol, reproduction limits, and available result cards. Keep observed results distinct from smoke
tests and deterministic demonstrations.

For changes to interactive figures, inspect desktop and narrow layouts plus keyboard operation. The existing
component contracts are described in [Adapter](../framework/adapter.md), [Evolver](../framework/evolver.md), and
[Trainer](../framework/trainer.md).

## Proposing a change

Describe the user-visible behavior, the affected contract, how you verified it, and any environment requirements.
Include a small reproducer for failures. Update commands, defaults, and docs together; do not describe a proposed
feature as available. The generic [service lifecycle proposal](service-design.md) is kept separately from supported usage.
