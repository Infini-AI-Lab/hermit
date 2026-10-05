# Evolution framework 0.2.0: implementation self-audit

Date: 2026-10-05. Branch `feat/evolution-framework` (worktree `metaHarnessAdapter-framework`). Scope: the code under
`harnessadapter/` against the design in `docs/framework/`, the Lean template migration, the OfficeQA example.

## What exists

| Part | Module | Tested by |
|---|---|---|
| Manifest `adapter.yaml`, structure globs | `adapter/manifest.py`, `adapter/loader.py` | `test_spec.py` |
| `check_spec` (checks 1–6) and repair | `spec.py` | `test_spec.py`, `test_training.py` |
| Git tree: root, run branches, worktrees, tags, invalid + repair commits, history view | `tree.py` | `test_training.py` |
| Evolver interface, loader (`evolver.yaml`), `AgentEvolver`, `CommandEvolver` | `evolver/` | `test_sandboxed_evolvers.py` |
| Built-in Evolvers `prompt_only`, `codex`, `claude` | `evolvers/` | GLM smoke (prompt_only); fake codex |
| Tools: check_spec, template/task tools, budget, llm, run_agent; HTTP endpoint; MCP proxy | `tools.py`, `tools_mcp.py` | `test_sandboxed_evolvers.py` |
| Sandbox (bubblewrap) and allowlist proxy | `sandbox.py` | `test_sandboxed_evolvers.py` |
| Trainer loop, learning material, budgets, records, checkpoints, resume, CARD.md | `trainer/` | `test_training.py`, GLM smoke |
| `check`, `train`, `eval`, `deploy` | `cli.py`, `settings_check.py`, `evaluate.py` | `test_training.py`, GLM smoke |
| Templates `prompt-only@1.0`, `lean-collab@1.0` (from `final_v12_5`) | `templates/` | `test_spec.py`, `test_deploy.py` |
| OfficeQA example (GLM 5.3 Flash pipeline, official strict judge) | `examples/officeqa/` | GLM smoke |

Offline tests: 18 passed (`python -m pytest tests`).

GLM 5.3 Flash smoke on OfficeQA (`examples/officeqa/smoke.yaml`): `check` passed with a one-question probe (89 s,
$0.048); `train` ran 2 steps on 4 questions (2 commits, `smoke/final` and `smoke/ckpt-epoch-1` tags, CARD.md;
$0.081 rollouts + $0.014 Evolver, 12 min); `eval --adapter none` and `--adapter final` ran on 2 test questions
(both 0/2; $0.091 and $0.053); the final card received the eval table. The scores show nothing about the method at
this size: the run checks the mechanics only.

## Fixed during the audit (before → after → what it solved)

1. Sandbox hid the config directory wholesale → the Evolver's own directory is re-exposed → an Evolver whose code
   sits next to `run.yaml` could not even start inside the sandbox.
2. Resume truncated material only when a step had completed → material and step records beyond the last completed
   step are always dropped → a crash before the first completed step left a duplicate `index.jsonl` line.
3. A failing `setup` was ignored → `train` stops with the error → training no longer starts from a half-set-up
   Evolver.
4. The `finalize` commit was labelled as an extra step → labelled `finalize` → commit history matched the steps.
5. Usage of metered tool calls was only in the budget → also in each step record (`metered_tool_usage`) → per-step
   Evolver cost is visible for scripted Evolvers that call `llm`.
6. `AgentEvolver` left copies of the user's login file in the run directory → `finalize` removes them → run
   directories can be shared without the login.
7. The tool endpoint on 127.0.0.1 accepted any local caller → the URL carries a random token → other local processes
   cannot spend the run's budget through `llm` or `run_agent`.
8. `sandbox.hide` could not name machine-specific paths → `${VAR}` is expanded in config paths, and `check` reports a
   hide path that does not exist → the OfficeQA answers can be hidden without hard-coding a home directory.
9. OfficeQA pipeline accepted an empty final turn as the answer (2 of 4 smoke questions) → it asks once more for a
   tagged answer → the example no longer scores empty answers that the model never gave.
10. The example YAML put `${…}` unquoted in a flow list (a parse error) → quoted.

## Open findings (not fixed; each needs a decision or more work)

| # | Finding | Effect | Proposed handling |
|---|---|---|---|
| 1 | The sandbox mounted `/` read-only and hid only the task, the runs and a blacklist; home-directory secrets stayed readable. | Decided (user): every sandbox setting is the task user's, and an allowlist beats a blacklist. | Done: allowlist sandbox (`sandbox.read`), `sandbox.env` allowlist for environment variables, warning when `env` is unset; a test checks that the home directory is invisible. |
| 2 | `restricted` network is a proxy plus proxy variables, not a network namespace. | A program ignoring proxy variables reaches any host. | Decided (user): the task's setting, like the rest of the sandbox; the warning stays. |
| 3 | Agent Evolvers' token usage is read when the session ends. | Mid-call, the hard budget sees only wall time and metered tool calls. | Decided (user): keep; spending between control points is accepted, bounded by `trainer.call_timeout_min`. |
| 4 | An in-process Evolver that ignores `ctx.cancel` cannot be stopped. | Decided (user): stop at the next point the Trainer controls and accept a slight overrun (as official GEPA does). | Done: no grace period or abandoned threads; tools refuse after the stop, started programs are killed, the call is awaited. |
| 5 | The user's pipeline runs unconfined in the Trainer's process. | Rollouts are not isolated by the framework. | Agreed (user): by design. |
| 6 | `check` cannot verify that a template supports the pipeline's host. | Decided (user): no check. Adapters are meant to be usable by any acting agent; each pipeline deploys the exports in its own way. | Done: documented. |
| 7 | The Lean pipeline (multi-agent launcher, grading) was not migrated; only the template. | `lean-collab@1.0` can be checked and deployed, not yet trained end to end inside this framework. | Deferred (user): Lean later. |
| 8 | `AgentEvolver` was exercised with a fake codex only. Real Codex with GLM needs a chat-completions relay that this repository no longer carries. | Untested with a real model. | Running (user): Codex AgentEvolver with GLM 5.3 Flash on Fireworks (`examples/officeqa/agent_smoke.yaml`). |
| 9 | GEPA and SkillOpt (branch `feat/optimizer-evolvers-gepa-skillopt`) are not ported. | Only prompt_only/codex/claude exist. | Deferred (user). |
| 10 | The prompt_only Evolver wrote a task-specific section ("polynomial regression") after one failed question. | Adapter quality, not framework behaviour. | Ignored (user). |

## Incident

While developing `test_sandboxed_evolvers.py`, a fake `codex` placed in the test's temporary directory was hidden by
the sandbox (which hides `/tmp` and the task directory), so the real `codex` on PATH ran for about 30 seconds with a
copy of the user's Codex login, on the toy test task. It was stopped; the copied login file was deleted. The test now
places the fake in the Evolver's directory and asserts it is the one found on PATH.

## Deviations from the design text (documented in each page's implementation notes)

- `adapter.yaml` has an `evolver_tools` section for template tools; exports add `skills`.
- `Context` adds `message`, `sandbox`, `cancel`, `tool_url`; the acting-agent runner is the run tool `run_agent`.
- The run config adds `llm`, `sandbox.hide`, `trainer.tree_dir`.
- `check` also checks `sandbox.hide` paths; it does not check host support.
