# Visual showcase

GLM 5.3 Flash writes one offline, interactive, animated web page, `index.html`, for a short brief: ocean waves
(`waves`), a lava lamp (`lava`) or wind-driven silk (`silk`). Every brief asks for the same pause and reset buttons,
an intensity slider and a `getState()` hook. A real headless browser runs the page and takes five screenshots;
the judge checks 12 mechanical items and has the same model rate five visual dimensions from the screenshots.
This example trains one adapter per brief with the MWM learner (`mwm_visual/`, an in-process GLM learner that reads
the agent's runs and their screenshots) and with the prompt-only Evolver (`prompt_only/`), and compares them with
two untrained arms: one request without tools (A) and the tool loop with a browser inspection tool (B). A second
experiment ([below](#experiment-2-growing-the-visual-tool)) takes the browser tool away and asks the learners to grow
one.

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

## Experiment 2: growing the visual tool

Experiment 1 handed the agent a ready-made browser tool. Experiment 2 takes it away. The agent (`utils/agent/agent.py`,
the same model, limits, records and final capture) gets four generic primitives: `write_file`, `read_file`,
`replace_text` and `run_command`, one `bash -c` command inside a bubblewrap sandbox (`utils/confine.py`: no network,
the workspace the only writable place, a private `/tmp`, the GPU visible) whose only picture channel is that PNG
files a command writes into the workspace are shown to the model. The agent is told nothing about node,
Playwright, Chromium or the GPU. The learners are told the machine's facts
(`utils/task-info-generic/<case>/environment.md`, written by `prepare.py --toolset generic`: interpreters, packages,
the Chromium build and its GPU flags, the sandbox's paths and limits) and must grow a render-and-look procedure or
tool. Same briefs and judge; 12 steps per brief, five eval repeats per checkpoint.

| Arm | What the agent gets | Config |
|---|---|---|
| B'-blind | The four primitives, no adapter | [`baseline/`](baseline/) `eval_<case>.yaml`, `eval --adapter none` |
| B'-hinted | B' with the machine facts appended to its system prompt (what the learners know) | [`baseline/`](baseline/) `eval_<case>_hinted.yaml`, `eval --adapter none` |
| **P** | B' plus `ADAPTER.md` written by the prompt-only Evolver ([method](../../docs/methods/prompt-only.md)) | [`prompt_only_generic/`](prompt_only_generic/) `run_<case>.yaml` |
| **M** | B' plus the adapter of the MWM learner of `mwm_visual/` (unchanged code, reading the acting agent's facts): its context package and read-only `wm` tool | [`mwm_visual_generic/`](mwm_visual_generic/) `run_<case>.yaml` |
| **T** | B' plus a toolsmith adapter ([method](../../docs/methods/toolsmith.md)): learned `ADAPTER.md` and learned tool bundles that the harness exposes as function tools whose results carry images; the learner self-tests them in the same sandbox | [`toolsmith/`](toolsmith/) `run_<case>.yaml` |

```bash
python examples/visual_showcase/utils/prepare.py --toolset generic                 # task-info-generic/ for this machine
python examples/visual_showcase/utils/confine.py selftest ~/.cache/visual_showcase/selftest   # the sandbox pre-flight
hermit check --no-probe --config examples/visual_showcase/toolsmith/smoke.yaml
hermit train --config examples/visual_showcase/toolsmith/smoke.yaml
hermit train --config examples/visual_showcase/toolsmith/run_waves.yaml   # and the other two methods
hermit eval  --config examples/visual_showcase/toolsmith/run_waves.yaml --adapter waves-ts/ckpt-step-6
hermit eval  --config examples/visual_showcase/baseline/eval_waves.yaml --adapter none
hermit eval  --config examples/visual_showcase/baseline/eval_waves_hinted.yaml --adapter none
```

Run names are `<case>-pg`, `<case>-mg`, `<case>-ts` and `<case>-bprime[-hinted]`. Besides the score, each run's
`result.json` counts whether the agent ever saw its page (`looked_calls`, `first_look_call`) and which adapter tools it
had and called (`tools_exported`, `tools_called`); `utils/emergence.py` sums them over run records and
`utils/tool_works.py` checks offline whether a toolsmith checkpoint's tool renders a known page truthfully. Results of
experiment 2 are pending; the [recipe](../../docs/recipes/visual-showcase.md) has the requirements and the protocol.

Experiment 4 (`run4_<case>.yaml` in `prompt_only_generic/` and `toolsmith/`) trains P and T for 48 steps with GLM 5.3
(not Flash) as the learner and longer limits everywhere a busy machine could cut a run short: the host's capture
(`runtime/browser_capture.cjs`: load 60 s, ready 20 s, whole observation 240 s, before 10, 4 and 40 s), the agent's
commands and tools (300 s), and the learner's self-tests (300 s per program). Its amendment 2 removed the run's time
limit (1800 s until then); only a model request that hangs for 1800 s is abandoned. While other users
loaded the GPUs, pages that load in about 1 s took over 10 s and their captures failed, scoring good pages 0.

## Code

```text
utils/pipeline.py   the acting agent: arms A (run_direct) and B/C (run); records under .logs/
utils/harness.py    the agent's tools: write_file, read_file, replace_text, inspect_page, and the browser capture
utils/judge.py      12 mechanical items and the GLM vision rubric      utils/prepare.py   tasks, splits, task-info
utils/runtime/      browser_capture.cjs (Playwright) and its packages   utils/cases.json   the three briefs
utils/task-info/    the task for the learner, one folder per brief
mwm_visual/         the MWM learner (mwm_visual/) and its template, run_<case>.yaml, smoke.yaml
prompt_only/        run_<case>.yaml and smoke.yaml for the prompt-only Evolver

utils/agent/        experiment 2's acting agent: agent.py (VisualAgent), generic_harness.py (its tools, image packing),
                    texts.py (its prompts)
utils/confine.py    the bubblewrap sandbox of run_command, the learned tools and the toolsmith's self-tests; its selftest
utils/runtime/chrome-gpu.sh   starts Chromium with the GPU flags
utils/reference/    webgl_probe.html (a GPU probe page) and best_<case>.html (experiment 1's best pages) for the checks
utils/task-info-generic/      the task for experiment 2's learners, with the machine facts (generated, not committed)
utils/tool_works.py, utils/emergence.py   offline checks: does a learned tool render truthfully; did the agent look
utils/tests/        tests of the sandbox, the agent, the generic texts and the checks
prompt_only_generic/   arm P: run_<case>.yaml, smoke.yaml      toolsmith/   arm T: run_<case>.yaml, smoke.yaml
mwm_visual_generic/    arm M: run_<case>.yaml, smoke.yaml      baseline/    eval_<case>.yaml, eval_<case>_hinted.yaml
```
