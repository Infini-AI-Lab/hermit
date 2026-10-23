# Visual showcase

GLM 5.3 Flash writes one offline, interactive, animated web page, `index.html`, for a short brief — ocean waves, a lava
lamp or wind-driven silk — and a headless browser's screenshots of the page are judged. The agent gets four generic
primitives inside a bubblewrap sandbox and is told nothing about how a page could be rendered; the learners get the
machine's facts and must grow a render-and-look procedure or tool. Experiment 4, the showcase, trains one adapter per
brief for 48 steps with GLM 5.3 as the learner and three methods: prompt-only (the Codex learner on the prompt-only
template, **PC**), [Toolsmith](../methods/toolsmith.md) with the same Codex learner (**TC**) and Toolsmith with its
scripted learner (**TS**). Its results and animations are in `examples/visual_showcase/README.md`.

The integration lives in `examples/visual_showcase/`: `utils/` holds the task (the agent, the judge, data preparation,
the browser capturer, the sandbox) and one folder per method holds its configs.

## Requirements

Use Linux with the framework installed and a Fireworks credential in `FIREWORKS_API_KEY`: the agent and the judge use
GLM 5.3 Flash through it, the learners GLM 5.3. PC and TC also need the Codex CLI on `PATH` (the learner of
`evolver.name: codex`). The browser side needs:

- Node 20 or newer (the experiment was built with Node 22) and the capturer's packages, pinned in
  `utils/runtime/package.json`: Playwright, pngjs and Three.js 0.180.0. `VISUAL_NODE` names `node` when it is not on
  PATH.
- Playwright's Chromium build (`npx playwright install chromium` from the runtime folder, or `VISUAL_CHROME_BIN`)
  started through `utils/runtime/chrome-gpu.sh`, which adds the flags (`--use-gl=angle --use-angle=gl-egl
  --ignore-gpu-blocklist`) that make WebGL draw on the GPU; without a GPU the sandbox can see, WebGL canvases come out
  blank white. Set `VISUAL_CHROME` to that wrapper so the host's final capture renders the same way. The wrapper's
  built-in fallback for the binary is the experiment machine's browser cache, so elsewhere also export
  `VISUAL_CHROME_BIN` (Playwright's `chromium-*/chrome-linux64/chrome`).
- Bubblewrap (`bwrap`) with unprivileged user namespaces: every `run_command`, every learned tool and the toolsmith's
  self-tests run through `utils/confine.py`. Its pre-flight is free and takes about half a minute (the scratch
  directory must be outside `/tmp`, which the sandbox replaces):

  ```bash
  .venv/bin/python examples/visual_showcase/utils/confine.py selftest ~/.cache/visual_showcase/selftest
  ```

The host's final captures take turns under a file lock (the agents' own browser commands share it), and configs use
`agent.concurrency: 1`, one agent run at a time per training run. Experiment 4 ran three training runs at a time on one
machine.

## Prepare

From the repository root:

```bash
npm ci --prefix examples/visual_showcase/utils/runtime
python examples/visual_showcase/utils/prepare.py     # tasks.jsonl, splits/, task-info-generic/
export FIREWORKS_API_KEY=...
```

`utils/task-info-generic/<case>/` is the learner's text: the brief and its contract, the tool list with the acting
system prompt quoted verbatim, and `environment.md`, the facts of the machine the agent's commands run on
(interpreters, packages, the Chromium build and its GPU flags, the sandbox's paths and limits), with resolved absolute
paths. It is generated on the machine that runs the experiment and not committed.

## The agent and the methods

The agent is `utils/agent/agent.py` (`VisualAgent`). Its tools are `write_file`, `read_file`, `replace_text` and
`run_command` (one `bash -c` command in the sandbox: no network, the workspace writable, PNG files it writes shown to
the model), and its system prompt says nothing about node, Playwright, Chromium or the GPU. With a learned adapter,
the adapter's `ADAPTER.md` joins the system prompt, and a toolsmith adapter's admitted tools become function tools
whose results may carry images. Experiment 4 lengthens every limit a busy machine could cut short (the host's capture:
load 60 s, ready 20 s, whole observation 240 s; a command or tool 300 s; no limit on a run; a model request abandoned
only after hanging 1800 s).

| Row | Template | Learner | Configs |
|---|---|---|---|
| PC | `prompt-only@1.0` | the built-in Codex Evolver, as the CL-Bench poker example runs it | `prompt_only_codex/run4_<case>.yaml` |
| TC | `toolsmith@1.2` | the same Codex Evolver, plus the task tool `test_tools` | `toolsmith_codex/run4_<case>.yaml` |
| TS | `toolsmith@1.2` | the toolsmith Evolver, which self-tests every changed tool in the agent's sandbox | `toolsmith/run4_<case>.yaml` |

Each run trains 48 steps on one brief; each checkpoint evaluated gets 5 eval runs (10 at step 48).

## Launch

Per brief (`waves`, `lava`, `silk`); `--no-probe` skips the paid probe run of `check`. TS:

```bash
hermit check --no-probe --config examples/visual_showcase/toolsmith/run4_waves.yaml
hermit train --config examples/visual_showcase/toolsmith/run4_waves.yaml
hermit eval  --config examples/visual_showcase/toolsmith/run4_waves.yaml --adapter waves-ts/ckpt-step-12
```

PC and TC run one fresh `codex exec` session per step. The session reaches the model through the metering relay
`hermit/runtime/wire_meter.py`; with `UPSTREAM_KEY_ENV` set, the relay authenticates with that variable's key and drops
the client's, so the session holds only a placeholder key. Start the relay, then name it as the `learner` backend:

```bash
export RELAY_PLACEHOLDER_KEY=placeholder     # the session's key: any dummy value; the relay sends FIREWORKS_API_KEY
PORT=3460 UPSTREAM=https://api.fireworks.ai UPSTREAM_KEY_ENV=FIREWORKS_API_KEY LEDGER=waves-pc-ledger.json \
  REWRITE_DEVELOPER_ROLE=1 REWRITE_STRIP_SERVER_TOOLS=1 REWRITE_REPAIR_TOOL_ARGS=1 \
  python -m hermit.runtime.wire_meter &
hermit train --config examples/visual_showcase/prompt_only_codex/run4_waves.yaml \
  backends.learner.base_url=http://127.0.0.1:3460/waves-pc/inference/v1
hermit eval  --config examples/visual_showcase/prompt_only_codex/run4_waves.yaml --adapter waves-pc/ckpt-step-12 \
  backends.learner.base_url=http://127.0.0.1:3460/waves-pc/inference/v1
```

`toolsmith_codex/run4_<case>.yaml` (TC) runs the same way; its one task tool, `test_tools`
(`utils/tool_selftest.py`), runs the scripted toolsmith's self-test on the agent's machine, which the Codex session's
own sandbox is not. Evaluate steps 12, 24, 36 and 48 (`eval.repeats=10` for step 48). Run names are `<case>-pc`,
`<case>-tc` and `<case>-ts`; every step is tagged `<run>/ckpt-step-N`.

## Outputs

| Path under `examples/visual_showcase/<method>/runs/` | Contents |
|---|---|
| `<run>/steps.jsonl` | One record per step: the step's score, the Evolver's summary and usage, the commit |
| `<run>/learning_material/step_NNNN/tasks/<id>.json` | The run's output, the judge's score and feedback, `pipeline_extra` (the agent's counters) |
| `<run>/learning_material/step_NNNN/trajectories/<id>/` | The run record: `request.json`, `calls/`, `tools/`, `cmd-NN/`, `capture/`, `submission/index.html`, `result.json`, `judge/` |
| `<run>/step_NNNN/` | The Evolver's `output/` and a `state/` snapshot of the step (TS: `output/report.md`, `state/tests.json`) |
| `<run>/test_tools/calls.jsonl` | TC: every `test_tools` self-test the Codex session ran |
| `<run>/eval/<label>/summary.json`, `results.jsonl` | The held-out evaluation of one checkpoint: `mean_score` over the repeats |
| `<run>/checkout/`, `trees-v3/<case>/` | The run's adapter and the git tree holding every checkpoint tag |
| `examples/visual_showcase/utils/.logs/` (or `$VISUAL_LOG_ROOT`) | Every run record as written by the agent, including eval runs |

A toolsmith checkpoint's learned files are `ADAPTER.md` and `tools/<name>/` in the tree
(`git -C examples/visual_showcase/toolsmith/runs/trees-v3/waves show waves-ts/ckpt-step-48:ADAPTER.md`); TS's
`<run>/step_NNNN/output/report.md` and `state/tests.json`, and TC's `<run>/test_tools/calls.jsonl`, say which bundles
passed their self-tests.

## Scoring

`utils/judge.py` reads the host's final capture of the submitted page (five screenshots and a summary of errors,
readiness, motion and the controls), checks 12 mechanical items and has GLM 5.3 Flash rate the brief's five review
dimensions from the screenshots without being told the arm or the adapter. The score is the rubric score (0 when no
verdict could be obtained); its feedback is one line per dimension and one per item. The rubric call's cost is
recorded in the run's `judge/` directory, outside the Trainer's budget.

Report the eval `mean_score` of each checkpoint; `solved_once` counts scores of exactly 1.0 and says little here.
Compare methods on the same `ckpt-step-N` tags, not on `final`, which `eval --adapter final` rewrites. The agent's own
counters in `result.json` and `pipeline_extra` (`looked_calls`, `first_look_call`, `tools_exported`, `tools_called`)
say whether a run ever saw its page, independently of the score; `utils/tool_works.py` checks offline whether a
toolsmith checkpoint's tool renders a known page truthfully.

## Runtime and cost evidence

Experiment 4 (measured, one training run per method and brief, three training runs at a time on one machine that
other users shared): a 48-step training run took 3.6 to 6.8 hours with PC, 9.7 to 12.2 with TC and 10.8 to 13.8 with
TS, and the 25 eval runs of its four checkpoints 1 to 3.6 hours. Its model spend was about 214 USD; the nine runs in
its results took about 202 USD of it, the rest an interrupted first attempt, runs stopped when the arms changed, and a
cancelled second seed.

## Limits

- Each adapter is trained on one brief and evaluated on the same brief under another id; the rubric is noisy and
  checkpoint-to-checkpoint variance is large. Experiment 4 has one training run per method and brief: treat its
  differences as descriptions of those runs, not a ranking of the methods.
- The judge is the acting model itself, so a score is that model's view of the page.
- `environment.md` names this machine's paths, Chromium build and GPU flags; regenerate it where the experiment runs.
  A machine without a GPU the sandbox can see renders WebGL blank, which the agent cannot tell from a working render.
- The capture lock makes the host's captures take turns: budget wall-clock time per config accordingly.

For the learner contract see [Toolsmith](../methods/toolsmith.md) and [Prompt-only](../methods/prompt-only.md); for
reporting held-out results see [Evaluation](../guides/evaluation.md).
