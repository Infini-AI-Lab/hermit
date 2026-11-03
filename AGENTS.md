# Hermit

Hermit trains, evaluates and deploys harness adapters: instructions, skills and
tools that improve an existing agent without changing its model weights.

`hermit/` contains the framework, `examples/` task and benchmark integrations,
`tests/` regression tests, and `docs/` usage and development guides.

## Using Hermit

Start with the [Quick Start](docs/get-started/quickstart.md) or [documentation](docs/index.md).

## Developing Hermit

Use Python 3.12+ and Git. In an activated environment, run from the repository root:

```bash
python -m pip install -e '.[test]'
python -m pytest tests/test_training.py tests/test_spec.py tests/test_deploy.py
```

- Read [CONTRIBUTING.md](CONTRIBUTING.md) for additional checks, dependencies and
  platform requirements, and the [developer guide](docs/developer/index.md) for extension points.
- Inspect the relevant implementation, callers and tests to understand current
  behavior. Agents follow [Model → Base Agent → Acting / Evolver Agent](docs/framework/agent-layers.md):
  reuse shared model and CLI execution through Base Agents; keep task-specific
  logic in `examples/`.
- For method reproductions, read the [reproduction guide](docs/developer/reproducing-a-method.md)
  and [reproduce-method skill](.agents/skills/reproduce-method/SKILL.md).
- Before submitting or updating a PR, follow [prepare-pr](.agents/skills/prepare-pr/SKILL.md)
  and perform the applicable local checks.
- After creating a PR or updating its code, use [post-pr-audit](.agents/skills/post-pr-audit/SKILL.md)
  for independent review against the general and applicable task-specific checklists.
