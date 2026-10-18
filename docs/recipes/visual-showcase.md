# Visual showcase

GLM 5.3 Flash writes one offline, interactive, animated web page, `index.html`, for a short brief — ocean waves, a lava
lamp or wind-driven silk — and a headless browser's screenshots of the page are judged. Two experiments share the task,
the briefs, the judge and the acting model. Experiment 1 gives the acting agent a ready-made browser inspection tool
and trains one adapter per brief with the prompt-only Evolver and an in-process MWM learner. Experiment 2 removes the
tool: the agent gets four generic primitives inside a bubblewrap sandbox and is told nothing about how a page could be
rendered; the learners get the machine's facts and must grow a render-and-look procedure or tool. Three learners run in
experiment 2: prompt-only, the same MWM learner, and [Toolsmith](../methods/toolsmith.md).

The integration lives in `examples/visual_showcase/`: `utils/` holds the task (agents, judge, data preparation, the
browser capturer, the sandbox) and one folder per method holds its configs.

## Requirements

Use Linux with the framework installed and a Fireworks credential in `FIREWORKS_API_KEY` (the agent, the judge and the
learners all use GLM 5.3 Flash through it). The browser side needs:

- Node 20 or newer (experiment 2 was built with Node 22) and the capturer's packages, pinned in
  `utils/runtime/package.json`: Playwright, pngjs and Three.js 0.180.0. `VISUAL_NODE` names `node` when it is not on
  PATH.
- A Chromium that renders WebGL headlessly. Experiment 1 uses Google Chrome in its standard place or the executable (or
  wrapper) named by `VISUAL_CHROME`. Experiment 2 uses Playwright's Chromium build (`npx playwright install chromium`
  from the runtime folder, or `VISUAL_CHROME_BIN`) started through `utils/runtime/chrome-gpu.sh`, which adds the flags
  (`--use-gl=angle --use-angle=gl-egl --ignore-gpu-blocklist`) that make WebGL draw on the GPU; without a GPU the
  sandbox can see, WebGL canvases come out blank white. Set `VISUAL_CHROME` to that wrapper so the host's final capture
  renders the same way.
- For experiment 2, bubblewrap (`bwrap`) with unprivileged user namespaces: every `run_command`, every learned tool and
  the toolsmith's self-tests run through `utils/confine.py`. Its pre-flight is free and takes about half a minute
  (the scratch directory must be outside `/tmp`, which the sandbox replaces):

  ```bash
  .venv/bin/python examples/visual_showcase/utils/confine.py selftest ~/.cache/visual_showcase/selftest
  ```

Runs take turns on a machine (a file lock around every browser launch), so configs use `agent.concurrency: 1` and the
runs of one experiment are sequential.

## Prepare

From the repository root:

```bash
npm ci --prefix examples/visual_showcase/utils/runtime
python examples/visual_showcase/utils/prepare.py                       # experiment 1: tasks.jsonl, splits/, task-info/
python examples/visual_showcase/utils/prepare.py --toolset generic     # experiment 2: task-info-generic/ as well
export FIREWORKS_API_KEY=...
```

`utils/task-info/<case>/` describes one brief to a learner of experiment 1. `utils/task-info-generic/<case>/` is the
learner's text for experiment 2: the same brief and contract, the generic tool list with the acting system prompt
quoted verbatim, and `environment.md`, the facts of the machine the agent's commands run on (interpreters, packages,
the Chromium build and its GPU flags, the sandbox's paths and limits), with resolved absolute paths. It is generated on
the machine that runs the experiment and not committed; `environment-mwm.md` is the same text with a preamble for the
MWM learner.

## The two toolsets

| | Experiment 1 (`utils/task-info/`) | Experiment 2 (`utils/task-info-generic/`) |
|---|---|---|
| The agent's tools | `write_file`, `read_file`, `replace_text`, `inspect_page` (runs the page in the browser and returns its screenshots) | `write_file`, `read_file`, `replace_text`, `run_command` (one `bash -c` command in the sandbox: no network, the workspace the only writable place, a private `/tmp`; PNG or JPEG files the command writes into the workspace are shown to the model) |
| What the agent is told | The inspection tool and its budget | Nothing about node, Playwright, Chromium or the GPU ("blind") |
| What the learner is told | The brief, the contract, how a run is observed | The same, plus the machine facts |
| Untrained arms | A (one request, no tools), B (the tool loop) | B'-blind (the four primitives), B'-hinted (B' with the facts in its system prompt) |
| Learners | MWM (`mwm_visual/`), prompt-only (`prompt_only/`) | P prompt-only (`prompt_only_generic/`), M the MWM learner (`mwm_visual_generic/`), T toolsmith (`toolsmith/`) |
| Steps per brief, eval repeats | 6 (5 learning rounds), 3 | 12, 5 |

Experiment 1's agent is `utils/pipeline.py`; experiment 2's is `utils/agent/agent.py` (`VisualAgent`), which keeps the
same limits (600 s, 12 requests, 24 tool calls, 65,536 completion tokens), records and final capture. With a learned
adapter the agent runs as arm C: a prompt-only or toolsmith adapter contributes `ADAPTER.md` to the system prompt and a
toolsmith adapter's admitted tools become function tools whose results may carry images; an MWM adapter contributes
its context package and read-only `wm` tool.

## Launch

Experiment 1, per brief (`waves`, `lava`, `silk`):

```bash
hermit check --config examples/visual_showcase/prompt_only/smoke.yaml
hermit train --config examples/visual_showcase/mwm_visual/run_waves.yaml
hermit eval  --config examples/visual_showcase/mwm_visual/run_waves.yaml --adapter waves-r5/ckpt-step-5
```

Experiment 2, per brief; check and smoke each method first (`--no-probe` skips the paid probe run of `check`; a smoke
is one step with a 0.3 USD budget):

```bash
hermit check --no-probe --config examples/visual_showcase/toolsmith/smoke.yaml
hermit train --config examples/visual_showcase/toolsmith/smoke.yaml
hermit train --config examples/visual_showcase/prompt_only_generic/run_waves.yaml   # arm P
hermit train --config examples/visual_showcase/mwm_visual_generic/run_waves.yaml    # arm M
hermit train --config examples/visual_showcase/toolsmith/run_waves.yaml             # arm T
hermit eval  --config examples/visual_showcase/toolsmith/run_waves.yaml --adapter waves-ts/ckpt-step-6
hermit eval  --config examples/visual_showcase/baseline/eval_waves.yaml --adapter none          # B'-blind
hermit eval  --config examples/visual_showcase/baseline/eval_waves_hinted.yaml --adapter none   # B'-hinted
```

Run names are `<case>-pg`, `<case>-mg`, `<case>-ts`, `<case>-bprime` and `<case>-bprime-hinted`; every step is tagged
`<run>/ckpt-step-N`. The baseline configs train nothing: evaluate them with `--adapter none` only.

## Outputs

| Path under `examples/visual_showcase/<method>/runs/` | Contents |
|---|---|
| `<run>/steps.jsonl` | One record per step: the step's score, the Evolver's summary and usage, the commit |
| `<run>/learning_material/step_NNNN/tasks/<id>.json` | The run's output, the judge's score and feedback, `pipeline_extra` (the agent's counters) |
| `<run>/learning_material/step_NNNN/trajectories/<id>/` | The run record: `request.json`, `calls/`, `tools/`, `cmd-NN/` (experiment 2), `capture/`, `submission/index.html`, `result.json`, `judge/` |
| `<run>/eval/<label>/summary.json`, `results.jsonl` | The held-out evaluation of one checkpoint: `mean_score` over the repeats |
| `<run>/checkout/`, `trees/<case>/` | The run's adapter and the git tree holding every checkpoint tag |
| `examples/visual_showcase/utils/.logs/` (or `$VISUAL_LOG_ROOT`) | Every run record as written by the agent, including eval runs |

A toolsmith checkpoint's learned files are `ADAPTER.md` and `tools/<name>/` in the tree
(`git -C examples/visual_showcase/toolsmith/runs/trees/waves show waves-ts/ckpt-step-6:ADAPTER.md`); the step's
`output/report.md` and `state/tests.json` under the run's learning material say which bundles passed their self-tests.

## Scoring

`utils/judge.py` reads the host's final capture of the submitted page (five screenshots and a summary of errors,
readiness, motion and the controls), checks 12 mechanical items and has GLM 5.3 Flash rate the brief's five review
dimensions from the screenshots without being told the arm or the adapter. The score is the rubric score (0 when no
verdict could be obtained); its feedback is one line per dimension and one per item. The rubric call's cost is
recorded in the run's `judge/` directory, outside the Trainer's budget. `VISUAL_JUDGE=mechanical` scores the
mechanical items only, without a model call, for free smokes.

Report the eval `mean_score` of each checkpoint; `solved_once` counts scores of exactly 1.0 and says little here.
Compare methods on the same `ckpt-step-N` tags, not on `final`, which `eval --adapter final` rewrites. For experiment
2 the agent's own counters in `result.json` and `pipeline_extra` (`looked_calls`, `first_look_call`,
`tools_exported`, `tools_called`) say whether a run ever saw its page, independently of the score;
`utils/emergence.py` recomputes them over run records and `utils/tool_works.py` checks offline whether a toolsmith
checkpoint's tool renders a known page truthfully.

## Runtime and cost evidence

Experiment 1 (measured, one run each): an agent run takes about four minutes and costs about 0.03 USD plus the judge's
rubric call; the whole experiment — three briefs, two learners, six steps each, three eval repeats per checkpoint —
cost about 8 USD. Its mean scores are in `examples/visual_showcase/README.md`.

Experiment 2 is budgeted at 2.5 USD and 8 hours per brief for P and M and 3.0 USD for T (`trainer.budget`); the
sandbox, the self-tests and the offline checks make no model calls. Its results are pending: nothing below the
experiment-1 table in the example README is a measurement yet.

## Limits

- Each adapter is trained on one brief, evaluated on the same brief under another id; the rubric is noisy and
  checkpoint-to-checkpoint variance is large. Treat single runs as planning observations.
- `environment.md` names this machine's paths, Chromium build and GPU flags; regenerate it where the experiment runs.
  A machine without a GPU the sandbox can see renders WebGL blank, which the agent cannot tell from a working render.
- The browser lock serialises runs: budget wall-clock time per config accordingly (`trainer.budget.hours: 8`).
- The hinted baseline gives the agent the facts, not a procedure; it bounds what instructions alone can add.
- Keep smoke evidence (one step, `VISUAL_JUDGE=mechanical`) separate from the experiment's measurements.

For the learner contract see [Toolsmith](../methods/toolsmith.md) and [Prompt-only](../methods/prompt-only.md); for
reporting held-out results see [Evaluation](../guides/evaluation.md).
