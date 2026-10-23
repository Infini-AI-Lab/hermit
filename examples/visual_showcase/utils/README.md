# Task: visual showcase

GLM 5.3 Flash writes one offline, interactive, animated web page, `index.html`, for a short brief: ocean waves
(`waves`), a lava lamp (`lava`) or wind-driven silk (`silk`). Every brief asks for the same pause and reset buttons, an
intensity slider and a `getState()` hook. The agent (`agent/`) has four generic tools and no browser tool. After its
run, a real browser (`runtime/browser_capture.cjs`) runs the submitted page and takes five screenshots; `judge.py`
scores them. `task-info-generic/<case>/` describes one case to the learner, so a learner trained on one case never sees
another.

## The agent

`agent/agent.py` (`VisualAgent`, named by `agent.name: ../utils/agent/agent.py:VisualAgent`) runs GLM 5.3 Flash with
at most 12 model requests, 24 tool calls and 65,536 completion tokens (32,768 per response). A run has no time limit,
and a model request is abandoned only when it hangs for 1800 seconds. The tools:

| Tool | What it does |
|---|---|
| `write_file(path, content)` | Writes a file in the workspace (at most 500,000 bytes; `index.html` is the only file submitted) |
| `read_file(path, start_line, max_lines)` | Numbered lines of a workspace file; `README.md` holds the brief |
| `replace_text(path, old, new)` | Exactly one occurrence |
| `run_command(command, timeout_s)` | `bash -c command` inside the sandbox of `confine.py` (bubblewrap: an allowlist of read-only paths, the workspace read-write, a private `/tmp` discarded after the command, no network, the GPU device nodes visible, an environment without any key); default 120, at most 300 seconds. PNG files the command creates or modifies in the workspace are shown to the model in the next message (at most 5 per call, 40 per run, 2 MB and 2.1 megapixels each). |

The system prompt (`agent/texts.py`) says that and nothing about how a page could be rendered: no node, Playwright,
Chromium, GPU or screenshot procedure. Arms: `harness` without an adapter or with the untouched starting adapter, else
`adapted`. The agent tells them apart by the adapter's `adapter.yaml`: the Trainer adds an
[`origin`](../../../docs/framework/adapter-manifest.md#origin) section only when it commits a learned change. In
`adapted`, the adapter's `ADAPTER.md` joins the system prompt, and a toolsmith adapter's admitted tools
(`tool_catalog`) become function tools dispatched through `tool_call`, whose result images are packed like a command's
(at most 6 adapter tool calls per run). The model, its settings and the host's final capture are in `pipeline.py` and
`harness.py`.

## Requirements

On Linux, from the repository root:

- Node 20 or newer (experiment 4 ran Node 22), and the capturer's packages, pinned in `runtime/package.json`:
  Playwright (drives the browser),
  pngjs (compares screenshots) and Three.js 0.180.0 (served to the page under `/vendor/`):

  ```bash
  npm ci --prefix examples/visual_showcase/utils/runtime
  ```

  `VISUAL_NODE` names another `node`.
- Playwright's Chromium build (`npx playwright install chromium` from `runtime/`) and a GPU the sandbox can see
  (`/dev/dri` or the NVIDIA device nodes): `runtime/chrome-gpu.sh` starts that Chromium with WebGL on the GPU. Export
  `VISUAL_CHROME_BIN` (Playwright's `chromium-*/chrome-linux64/chrome`): the wrapper's own fallback is the experiment
  machine's browser cache. The host capture launches the installed Google Chrome unless `VISUAL_CHROME` names an
  executable; experiment 4 set `VISUAL_CHROME` to `runtime/chrome-gpu.sh`, so the host's captures render as the agent's
  commands do.
- bubblewrap (`bwrap`) with unprivileged user namespaces. The pre-flight is free:
  `python examples/visual_showcase/utils/confine.py selftest ~/.cache/visual_showcase/selftest` (a scratch directory
  outside `/tmp`, which the sandbox replaces) checks the GPU pixel path and the isolation and prints a JSON report.
- `FIREWORKS_API_KEY` for the agent and the judge. It is read from the environment only, and removed from the
  browser's and the sandbox's environment.

Without Three.js a run stops before its first model call. Without Playwright, pngjs or the browser it still calls the
model and then fails its captures. To check the browser side first (free; about a minute and a half, because a
blank page waits out the capturer's ready and action timeouts), with `VISUAL_CHROME` set as above or Google Chrome
installed: `ls` should list the Three.js file, and the capture of a blank page should print a line containing
`"capture_ok":true`:

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

This writes `tasks.jsonl`, `splits/` and `task-info-generic/` here, all gitignored because the facts name this
machine's absolute paths. Each case has a training split (`splits/waves.txt`) and a test split
(`splits/waves-eval.txt`, the same brief under the id `waves-eval`). Per case, `task-info-generic/<case>/` holds:

- `README.md`: "How the agent runs" with the tool list and the acting system prompt quoted verbatim, then the brief,
  the common contract and the environment facts;
- `judge.md`: how a run is observed and recorded;
- `environment.md`: the facts alone: headless only, node and the packages and how `require()` finds them, the Chromium
  build and the GPU flags (`runtime/chrome-gpu.sh`), the venv `python3`, loopback and the `/vendor/` mapping, what is
  writable, the limits, which files come back as images, what the sandbox contains, the agent's tool list and prompt,
  the ready contract and viewports;
- `probes/`: the toolsmith self-tests' probe pages, listed in `probes.json`.

## Runs and scores

- Each run writes its record to `.logs/` (or `$VISUAL_LOG_ROOT`); `task-info-generic/<case>/judge.md` lists its
  files. Browser captures take turns on a machine (a file lock, `~/.cache/visual_showcase/capture.lock` or
  `$VISUAL_CAPTURE_LOCK`), so several runs can share it.
- A run's record has `cmd-NN/` per `run_command` (its launch trace and, when the command produced images, the
  `index.html` of that moment), `tools/NN.json` with the image records (file, sha256, size, non-blank, packed for the
  model) and whether a browser was launched, and in `result.json` the counters `run_commands`, `adapter_tool_calls`,
  `image_bearing_calls`, `looked_calls` (an image-bearing call that launched a browser and returned a non-blank image),
  `first_look_call`, `looked_final`, `tools_exported`, `tools_called`, `tool_errors`, `context_exhausted`,
  `seen_pages` (the looks at `index.html` whose images a later accepted request carried to the model: call and sha256),
  `seen_final` (the submitted version is one of them) and `finalize`. A look is keyed by `index.html` at the time of
  the call; a learned-tool call whose arguments name another `.html` file does not count.
- When the adapter exports `finalize` (toolsmith), the harness submits the version it names: a final version the model
  never saw is replaced by the last one it saw, recorded as `finalize: {replaced, reason, final_sha, submitted_call}`,
  and the replaced version is kept in `unseen_final/index.html`. A prompt-only adapter submits the final version
  (`finalize: null`).
- `judge.py` checks 12 mechanical items of the final capture and has GLM 5.3 Flash rate the case's five review
  dimensions from its screenshots, without telling it the arm or the adapter (details in its docstring). The score is
  the rubric score, or 0 when it could not be obtained. Its feedback, for the learners, has one line per rubric
  dimension with its reason (or one line saying why there is no rubric verdict), then one line per mechanical item.
  The rubric's model cost is recorded in `judge/`, outside the Trainer's budget.
- `tool_works.py` exports a toolsmith checkpoint and checks offline that its tool renders `reference/webgl_probe.html`
  with the GPU and `reference/best_<case>.html` like the host capturer does.

## Configs

[`prompt_only_codex/`](../prompt_only_codex/) (PC), [`toolsmith_codex/`](../toolsmith_codex/) (TC) and
[`toolsmith/`](../toolsmith/) (TS), `run4_<case>.yaml`: 48 steps per case.
