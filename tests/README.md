# Tests

```bash
python -m pytest --changed          # the groups this branch touches (since origin/main; --base <ref> for another)
python -m pytest tests/core         # one group
python -m pytest                    # everything (tests/ and the examples' own tests)
python -m pytest --integration ...  # also the tests that run an agent CLI installed on the machine
```

| Group | What it covers |
|---|---|
| `core/` | the framework: the Trainer, templates and `check_spec`, deployment and host exporters, the agent layers (Base Agents, CLI agents, model bindings), the sandbox and isolation, the generic AgentEvolvers |
| `methods/<name>/` | one method: its Evolver, its template and its examples (`ace`, `ahe`, `continual_harness`, `harness2`, `meta_harness_adapter`, `mwm`, `optimizers` for GEPA and SkillOpt, `reharness`, `rrsi_adapter`) |

`--changed` reads `selection.py`: a method's own files (its Evolver, its template, its examples, its tests) run its
group; the rest of the framework runs `core/`; a shared layer every method is built on (the Base Agents, the Evolver
base classes, the Trainer, the sandbox, the hosts' deployment, the model bindings, the toy task's template, the test
helpers) and any path the table does not know run every group; documentation runs none. A new method adds its
paths to `METHODS` there and its tests under `methods/<name>/`; `core/test_selection.py` fails until it does.

A test marked `integration` runs an agent CLI installed on the machine (`codex`, `claude`) and holds what a release
of it does; it is skipped unless `--integration` is given. Tests that need bubblewrap or a method's dependency group
skip themselves when it is missing.

Shared helpers: `conftest.py` (the options, a private ChatGPT-login directory for every test), `fakes.py` (scripted
models), `toy_task.py` (the toy task the Trainer tests run on), `fixtures/`.
