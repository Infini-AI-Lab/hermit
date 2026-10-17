# Visual showcase

GLM 5.3 Flash writes one offline, interactive, animated web page, `index.html`, for a short brief: ocean waves
(`waves`), a lava lamp (`lava`) or wind-driven silk (`silk`). Every brief asks for the same pause and reset buttons,
an intensity slider and a `getState()` hook. A real headless browser runs the page and takes five screenshots;
the judge checks 12 mechanical items and has the same model rate five visual dimensions from the screenshots.
This example trains one adapter per brief with the MWM learner (`mwm_visual/`, an in-process GLM learner that reads
the agent's runs and their screenshots) and with the prompt-only Evolver (`prompt_only/`), and compares them with
two untrained arms: one request without tools (A) and the tool loop with a browser inspection tool (B).

## Setup

Install the framework first ([Installation](../../docs/get-started/installation.md)). The browser side needs Node 20
or newer and the capturer's packages (Playwright, pngjs, Three.js 0.180.0), and a Chromium that renders WebGL
(`VISUAL_CHROME` names the executable or a wrapper; `VISUAL_NODE` names `node` when it is not on PATH):

```bash
npm ci --prefix examples/visual_showcase/utils/runtime
python examples/visual_showcase/utils/prepare.py        # tasks.jsonl, splits/, task-info/
export FIREWORKS_API_KEY=...                             # the agent and the judge
```

[`utils/README.md`](utils/README.md) describes the arms, the limits of a run, the free browser check and every record
a run writes.

## Run

```bash
hermit check --config examples/visual_showcase/prompt_only/smoke.yaml
hermit train --config examples/visual_showcase/mwm_visual/run_waves.yaml
hermit eval  --config examples/visual_showcase/mwm_visual/run_waves.yaml --adapter waves-r5/ckpt-step-5
```

Each case has its own config (`run_waves.yaml`, `run_lava.yaml`, `run_silk.yaml`) and its own adapter tree; arm A is
the same config with `agent.name=../utils/pipeline.py:run_direct`, arm B is `eval --adapter none`.

## Results

Host: GLM 5.3 Flash (Fireworks), temperature 0, reasoning effort 4096; one adapter per brief, five learning rounds,
three eval repeats per checkpoint on the same brief. Mean rubric score of the eval repeats:

| Arm | waves | lava | silk |
|---|---|---|---|
| A: one request, no tools | 0.40 | 0.65 | 0.37 |
| B: tool loop with `inspect_page` | 0.93 | 0.65 | 0.92 |
| **MWM** ([`mwm_visual/`](mwm_visual/)), best checkpoint | 0.95 | 0.95 | 1.00 |
| **prompt-only** ([`prompt_only/`](prompt_only/)), best checkpoint | 0.98 | 0.93 | 0.93 |

Checkpoint-to-checkpoint variance is large (single-brief training, a noisy rubric); the per-checkpoint table and the
costs are in the run records. The whole experiment cost about 8 USD.

## Code

```text
utils/pipeline.py   the acting agent: arms A (run_direct) and B/C (run); records under .logs/
utils/harness.py    the agent's tools: write_file, read_file, replace_text, inspect_page, and the browser capture
utils/judge.py      12 mechanical items and the GLM vision rubric      utils/prepare.py   tasks, splits, task-info
utils/runtime/      browser_capture.cjs (Playwright) and its packages   utils/cases.json   the three briefs
utils/task-info/    the task for the learner, one folder per brief
mwm_visual/         the MWM learner (mwm_visual/) and its template, run_<case>.yaml, smoke.yaml
prompt_only/        run_<case>.yaml and smoke.yaml for the prompt-only Evolver
```
