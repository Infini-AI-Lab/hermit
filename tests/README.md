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
| `methods/<name>/` | one method: its Evolver, its template and its examples (`ace`, `ahe`, `aspire`, `continual_harness`, `evosafe`, `harness2`, `meta_harness_adapter`, `mwm`, `opro`, `optimizers` for GEPA and SkillOpt, `reharness`, `rrsi_adapter`, `she`) |

`--changed` reads `selection.py`: `core/` runs for every change but documentation (its tests also read every
method's template, manifest and packaging); a method's own files (its Evolver, its template, its examples, its tests)
add its group; a shared layer every method is built on (the Base Agents, the acting agents, the Evolver base classes,
the Trainer, the sandbox, the hosts' deployment, the model bindings, the templates' manifests, the toy task's
template, the test helpers) and any path the table does not know run every group; documentation runs none, but for
`docs/_examples/`, which `core/` reads. A new method adds its paths to `METHODS` in `selection.py` and its tests
under `methods/<name>/`. `core/test_selection.py` fails until it does, when a test file is in no group, and when a
framework module a shared layer or a method's tests import is not shared.

A test marked `integration` runs an agent CLI installed on the machine (`codex`, `claude`, `dsh`) and holds what a
release of it does; it is skipped unless `--integration` is given (those that run Claude Code in its container also skip
without Docker and the image `docker/claude/` builds, named by HERMIT_CLAUDE_IMAGE and HERMIT_CLAUDE_NETWORK). Some
methods' tests (`gepa`, `skillopt`, `harness2`, `rrsi`) skip themselves without the method's dependency group; the
sandbox and MWM tests need Linux and a working bubblewrap, and some do not yet skip without it (CONTRIBUTING.md).

CI (`.github/workflows/tests.yml`) runs `--changed` on a pull request and the whole suite on main, daily and on
demand.

Shared helpers: the repository root's `conftest.py` (the options and the groups), `tests/conftest.py` (a private
ChatGPT-login directory and Claude plan for every test, and no copy of the user's own sign-in), `fakes.py` (scripted
models), `toy_task.py` (the toy task the Trainer tests run on), `claude_stand.py` (the stand-in `claude` that replays
`fixtures/claude_streams/`, and the probe for a working bubblewrap, for core's and the methods' Claude tests),
`pi_stand.py` (the stand-in `pi` that replays `fixtures/pi/`, for the methods' Pi tests),
`fixtures/` (`pi/`: Pi's recorded streams, read by `core/test_pi_agent.py` and `pi_stand.py`; `opencode/`: OpenCode's,
read by `core/test_opencode_agent.py`; `dsh/`: DeepSeek Harness's, read by `core/test_dsh_agent.py`).

A new or changed test follows the [test checklist](../.agents/skills/prepare-pr/references/test-checklist.md): where it
goes, how it is named and documented, what it asserts, and what it may touch.
