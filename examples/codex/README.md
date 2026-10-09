# Codex agent

A Codex agent is the learner. At each training step it runs in the run's workspace with a prompt
(`START.md`), reads the step's learning material (each task's input, output, score, feedback and full trajectory),
edits the adapter, checks it with the `check_spec` tool and writes a report. The `claude` Evolver is the same with
Claude Code.

## One step

1. The Trainer runs the current adapter on `data.step_size` training tasks and writes the learning material.
2. `codex exec` starts in the sandbox with `START.md`; with `evolver.resume: true`, later steps continue the same
   session with `RESUME.md`.
3. The agent changes only the adapter, calls `check_spec` (served to it as the MCP server `evolver_tools`), and writes
   `output/report.md`, whose first line is the step's summary.
4. The Trainer checks the adapter again, repairs it if needed, and commits it.

## Knobs

| Setting | Meaning |
|---|---|
| `evolver.params.model`, `args` | The agent's model and extra `codex` arguments (here: GLM 5.3 Flash on Fireworks as a model provider) |
| `evolver.params.prompt`, `resume_prompt` | Prompt files in the Evolver's folder |
| `evolver.params.home` | Login files copied from `~/.codex` (here none: the key comes from `FIREWORKS_API_KEY`) |
| `evolver.params.price` | USD per million tokens, for the run's cost accounting |
| `evolver.resume` | Keep one agent session across steps |
| `sandbox` | What the agent may reach: network allow list, readable paths, environment variables |
| `trainer.call_timeout_min` | Time limit of one agent call |

## Examples

| Folder | Task | Configs |
|---|---|---|
| [`officeqa/`](officeqa/) | [OfficeQA](../tasks/officeqa/); GLM 5.3 Flash acts and drives Codex | `smoke.yaml`: 4 training questions in 2 steps, 2 test questions<br>`codex_glm/`: the `codex` Evolver configured for GLM 5.3 Flash |

## Run

Needs the `codex` CLI and `bwrap` for the sandbox (see [Installation](../../docs/get-started/installation.md)).

```bash
python examples/tasks/officeqa/prepare.py /path/to/officeqa/prepared
export FIREWORKS_API_KEY=...
harnessadapter check --config examples/codex/officeqa/smoke.yaml
harnessadapter train --config examples/codex/officeqa/smoke.yaml
harnessadapter eval  --config examples/codex/officeqa/smoke.yaml --adapter final
```
