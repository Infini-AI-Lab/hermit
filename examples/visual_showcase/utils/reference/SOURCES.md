# Reference pages

Fixed pages for the offline checkers (`utils/tool_works.py`, `utils/confine.py selftest`) and, for `webgl_probe.html`,
the toolsmith learner's self-tests (`prepare.py` copies it to `task-info-generic/<case>/probes/`; the learner reads only
its label and note, and its learned tools are called on it). None of them is ever shown to an acting agent; they live
outside every sandbox bind (confine.py binds only the paths its facts name).

| file | what it is | source (read-only) | judge score | sha256 |
|---|---|---|---|---|
| `webgl_probe.html` | the contract's three controls and `getState()`; WebGL clears the scene to rgb(30,120,200) every frame; blank white under SwiftShader | written for experiment 2 (WP1) | n/a | see file |
| `best_waves.html` | experiment 1, arm B (inspect_page harness), waves, best record | `MetaHarnessAdapter/examples/tasks/visual_showcase/.logs/waves-harness-20261013T201159Z-39e47e/submission/index.html` | 1.0 (mechanical 1.0, rubric 1.0; judged 2026-10-13T20:16:38Z; run status completed, 3 inspections, 18 tool calls) | `161bc8c61640a94760d17ac3d51b0c744710d0b1a202c4214247d9b6da2fa862` |
| `best_lava.html` | experiment 1, arm B, lava, best record | `.logs/lava-harness-20261013T221438Z-cc2b07/submission/index.html` | 1.0 (mechanical 1.0, rubric 1.0; judged 2026-10-13T22:20:08Z; run status budget_exhausted, 3 inspections, 23 tool calls) | `65eb77a834db0589229429aee4e52fc3f98259717ee8bf9270395041381f78f6` |
| `best_silk.html` | experiment 1, arm B, silk, best record | `.logs/silk-harness-20261014T001638Z-e32bc8/submission/index.html` | 1.0 (mechanical 1.0, rubric 1.0; judged 2026-10-14T00:22:43Z; run status completed, 3 inspections, 20 tool calls) | `3d36fc5335d61c02696615e8ffa7d198bef30a7beeeb3016fdde7d9f4abf6bf3` |

The three records are the ones the experiment contract names; each `judge/judge.json` was read on 2026-10-17 and
reports `score: 1.0` with `judge_model: accounts/fireworks/models/glm-5p3-flash` (mode `vlm`). Copies are byte-identical
to the sources (sha256 above); the source directories are read-only and were not modified.

## How the checkers use them

- `tool_works.py` seeds a scratch workspace with `webgl_probe.html` as `index.html` and calls a checkpoint's learned
  tool with no arguments: an image whose dominant colour is within ±10 of rgb(30,120,200) proves the GPU path (a
  SwiftShader frame is white). Then it seeds `best_<case>.html` and requires `pngstats.thumb_corr ≥ 0.8` between the
  tool's image and the host capturer's `desktop-t0.png` of the same page.
- `emergence.py` does not use these pages; it re-renders the agent's own `cmd-NN/index.html` snapshots.

## Word audit of the learner-facing texts (`prepare.py --toolset generic`)

`grep -n -i -E 'screenshot|render|capture'` over `task-info-generic/waves/`:

| file | hits | where | why it is there |
|---|---|---|---|
| `environment.md` | 2 | inside the fenced verbatim acting system prompt (F12): "not screenshots of a scene" (COMMON, experiment 1 text) and "the host's final capture" (LOOP_GENERIC) | the prompt is quoted verbatim so the learner knows exactly what the agent is told; the facts F1-F15 themselves contain none of the three words |
| `README.md` | 10 | the same two prompt lines twice (section "How the agent runs" and F12 of the embedded facts); the brief: "a single screenshot", "rendering technique", "rendered content", "screenshots and working controls are independently inspected"; the contract JSON: `"observation": "Actual screenshots near t=0,2,5 seconds ..."` | the brief and contract are byte-identical to experiment 1 by design (arms A and B ran with them); the brief's inspection paragraph is neutralised by the prompt's last-but-one sentence |
| `judge.md` | 5 | "browser capturer", "capture is never shown", `capture/` directory, "1280×800 screenshots", "timed screenshots" | judge.md describes the host's final observation for people; the built-in prompt_only learner reads README.md only, and the toolsmith learner reads README.md and environment.md |

The reference pages themselves contain the words in comments and shader names (waves 8, lava 3, silk 12 lines); they
are never part of a learner's or agent's material.
