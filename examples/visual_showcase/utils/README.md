# Task: visual showcase

GLM 5.3 Flash writes one offline, interactive, animated web page, `index.html`, for a short brief: ocean waves
(`waves`), a lava lamp (`lava`) or wind-driven silk (`silk`). Every brief asks for the same pause and reset buttons, an
intensity slider and a `getState()` hook. A real browser (headless Chrome, `runtime/browser_capture.cjs`) runs the
page and takes five screenshots; `judge.py` scores them. `task-info/<case>/` describes one case to the Evolver, so a
learner trained on one case never sees another. Two toolsets share the task: experiment 1's (`pipeline.py`, a browser
inspection tool) and experiment 2's generic toolset (`agent/`, no browser tool; the section
[below](#the-generic-toolset-experiment-2)).

## Conditions

`pipeline.py` runs the agent in one of three conditions (arms):

| Arm | What the agent gets | How it is picked |
|---|---|---|
| A, `direct` | One request, no tools; the HTML is taken from the reply | `agent.name: ../utils/pipeline.py:run_direct` |
| B, `harness` | A tool loop: `write_file`, `read_file`, `replace_text` and `inspect_page`, which runs the page in the browser and returns its screenshots | `pipeline.py:run` without an adapter, or with the untouched starting adapter |
| C, `adapted` | B, plus the adapter's knowledge in the system prompt: an MWM adapter's `context_package` and its read-only `wm` tool, or another adapter's `context` file (`ADAPTER.md` for `prompt-only@1.0`) | `pipeline.py:run` with a learned adapter |

The pipeline tells B from C by the adapter's `adapter.yaml`: the Trainer adds an
[`origin`](../../../docs/framework/adapter-manifest.md#origin) section only when it commits a learned change. Limits
of a B or C run: 600 seconds, 12 model requests, 24 tool calls, 3 inspections and 65,536 completion tokens (32,768 per
response).

## Requirements

On macOS or Linux, from the repository root:

- Node 20 or newer, and the capturer's packages, pinned in `runtime/package.json`: Playwright (drives Chrome), pngjs
  (compares screenshots) and Three.js 0.180.0 (served to the page under `/vendor/`):

  ```bash
  npm ci --prefix examples/visual_showcase/utils/runtime
  ```

  `VISUAL_NODE` names another `node`.
- Google Chrome in its standard place, or `VISUAL_CHROME` set to a Chrome executable. `npm ci` downloads no browser.
- `FIREWORKS_API_KEY` for the agent and the judge. It is read from the environment only, and removed from the
  browser's environment.
- For the generic toolset (Linux only): bubblewrap (`bwrap`) with unprivileged user namespaces, Playwright's Chromium
  build (`npx playwright install chromium` from `runtime/`, or `VISUAL_CHROME_BIN`), a GPU the sandbox can see
  (`/dev/dri` or the NVIDIA device nodes) so that `runtime/chrome-gpu.sh` renders WebGL, and Node 22. The pre-flight is
  free: `python examples/visual_showcase/utils/confine.py selftest ~/.cache/visual_showcase/selftest` (a scratch
  directory outside `/tmp`, which the sandbox replaces) checks the GPU pixel path and the isolation and prints a JSON
  report.

Without Three.js a run stops before its first model call. Without Playwright, pngjs or Chrome it still calls the
model and then fails its captures. To check the browser side first (free, about 30 seconds): `ls` should list the
Three.js file, and the capture of a blank page should print a line containing `"capture_ok":true`:

```bash
ls examples/visual_showcase/utils/runtime/node_modules/three/build/three.module.min.js
d=$(mktemp -d) && mkdir "$d/page" && echo '<!doctype html><title>check</title>' > "$d/page/index.html"
node examples/visual_showcase/utils/runtime/browser_capture.cjs --workspace "$d/page" --output "$d/out" --case waves
rm -r "$d"
```

## Prepare

```bash
python examples/visual_showcase/utils/prepare.py
```

This writes `tasks.jsonl`, `splits/` and `task-info/` here. Each case has a training split (`splits/waves.txt`) and a
test split (`splits/waves-eval.txt`, the same brief under the id `waves-eval`). `prepare.py --toolset generic` writes
the same rows and splits plus `task-info-generic/` for the generic toolset (below); the rows are byte-identical, so the
two toolsets share `tasks.jsonl`.

## Runs and scores

- Each run writes its record to `.logs/` (or `$VISUAL_LOG_ROOT`); `task-info/<case>/judge.md` lists its files. Browser
  captures take turns on a machine (a file lock, `~/.cache/visual_showcase/capture.lock` or `$VISUAL_CAPTURE_LOCK`),
  so several runs can share it.
- `judge.py` checks 12 mechanical items of the final capture and has GLM 5.3 Flash rate the case's five review
  dimensions from its screenshots, without telling it the arm or the adapter (details in its docstring). The score is
  the rubric score, or 0 when it could not be obtained. Its feedback, for Evolvers that learn from it (Codex, toolsmith),
  has one line per rubric dimension with its reason (or one line saying why there is no rubric verdict), then one
  line per mechanical item; `VISUAL_FEEDBACK=0` leaves it empty. The `mwm_visual` learner never sees it: its evidence
  leaves out scores, feedback and `judge/`. The rubric's model cost is recorded in `judge/`, outside the Trainer's
  budget. `VISUAL_JUDGE=mechanical` scores the mechanical items only, without a model call.

## The generic toolset (experiment 2)

`agent/agent.py` (`VisualAgent`, named by `agent.name: ../utils/agent/agent.py:VisualAgent`) runs the same model with
the same limits, records and final capture as `pipeline.py`, except time and tools. A run has no time limit (600
seconds before experiment 4, 1800 until its amendment 2), and a model request is abandoned only when it hangs for 1800
seconds. It has four generic tools and no browser tool:

| Tool | What it does |
|---|---|
| `write_file(path, content)` | Writes a file in the workspace (at most 500,000 bytes; `index.html` is the only file submitted) |
| `read_file(path, start_line, max_lines)` | Numbered lines of a workspace file; `README.md` holds the brief |
| `replace_text(path, old, new)` | Exactly one occurrence |
| `run_command(command, timeout_s)` | `bash -c command` inside the sandbox of `confine.py` (bubblewrap: an allowlist of read-only paths, the workspace read-write, a private `/tmp` discarded after the command, no network, the GPU device nodes visible, an environment without any key); default 120, at most 300 seconds (60 and 90 before experiment 4). PNG files the command creates or modifies in the workspace are shown to the model in the next message (at most 5 per call, 40 per run, 2 MB and 2.1 megapixels each). |

The system prompt (`agent/texts.py`) says that and nothing about how a page could be rendered: no node, Playwright,
Chromium, GPU or screenshot procedure. Arms: `harness` (B') without an adapter or with the untouched root, else
`adapted` (C): a prompt-only or toolsmith adapter contributes `ADAPTER.md` to the system prompt and a toolsmith adapter's
admitted tools (`tool_catalog`) become function tools dispatched through `tool_call`, whose result images are packed
like a command's (at most 6 adapter tool calls per run); an MWM adapter contributes its context package and read-only
`wm` tool. `agent.params.facts_file` (the hinted baseline) appends a text file to the system prompt.

`prepare.py --toolset generic` writes two task-info sets for the learners (gitignored, because the facts name this
machine's absolute paths):

- `task-info-generic/<case>/README.md`: the brief and the contract as in `task-info/`, then "How the agent runs" with
  the generic tool list and the acting system prompt quoted verbatim, then the environment facts; `judge.md`, how a
  run is observed and recorded (the same final capture); `environment.md`, the facts alone: headless only, node and
  the packages and how `require()` finds them, the Chromium build and the GPU flags (`runtime/chrome-gpu.sh`), the
  venv `python3`, loopback and the `/vendor/` mapping, what is writable, the limits, which files come back as images,
  what the sandbox contains, the agent's tool list and prompt, the ready contract and viewports.
- `task-info-generic/environment-mwm.md`: the same facts with a preamble saying they describe the ACTING agent's
  machine, for the MWM learner (`evolver.params.environment_file`), whose own tools are unchanged.

A generic run's record adds to the layout above: `cmd-NN/` per `run_command` (its launch trace and, when the command
produced images, the `index.html` of that moment), `tools/NN.json` with the image records (file, sha256, size,
non-blank, packed for the model) and whether a browser was launched, and in `result.json` the counters `run_commands`,
`adapter_tool_calls`, `image_bearing_calls`, `looked_calls` (an image-bearing call that launched a browser and
returned a non-blank image), `first_look_call`, `looked_final`, `tools_exported`, `tools_called`, `tool_errors`,
`context_exhausted`, `seen_pages` (the looks at `index.html` whose images a later accepted request carried to the
model: call and sha256), `seen_final` (the submitted version is one of them) and `finalize`. A look is keyed by
`index.html` at the time of the call; a learned-tool call whose arguments name another `.html` file does not count.
When the adapter exports `finalize` (toolsmith 1.1), the harness submits the version it names: a final version the
model never saw is replaced by the last one it saw, recorded as `finalize: {replaced, reason, final_sha,
submitted_call}`, and the replaced version is kept in `unseen_final/index.html`. Other arms submit the final version
(`finalize: null`). `emergence.py` sums them over run records (and re-renders each snapshot to check that the image
the agent saw matches the page); `tool_works.py` exports a toolsmith checkpoint and checks offline that its tool
renders `reference/webgl_probe.html` with the GPU and `reference/best_<case>.html` like the host capturer does.

## Configs

- Experiment 4, the generic toolset, 48 steps: [`prompt_only_codex/`](../prompt_only_codex/) (PC),
  [`toolsmith_codex/`](../toolsmith_codex/) (TC) and [`toolsmith/`](../toolsmith/) `run4_<case>.yaml` (TS).
- Experiment 1: [`mwm_visual/`](../mwm_visual/), the MWM learner, one adapter per case.
- Experiment 2, the generic toolset: [`mwm_visual_generic/`](../mwm_visual_generic/) (arm M: the learner of
  `mwm_visual/` with the acting agent's `environment_file`), [`toolsmith/`](../toolsmith/) `run_<case>.yaml` (arm T)
  and [`baseline/`](../baseline/) (B'-blind and B'-hinted, `eval --adapter none`).
- The prompt-only arms of experiments 1 and 2 used a scripted prompt-only learner that hermit has removed; their
  configs went with it.
