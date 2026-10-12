# Task: visual showcase

GLM 5.3 Flash writes one offline, interactive, animated web page, `index.html`, for a short brief: ocean waves
(`waves`), a lava lamp (`lava`) or wind-driven silk (`silk`). Every brief asks for the same pause and reset buttons, an
intensity slider and a `getState()` hook. A real browser (headless Chrome, `runtime/browser_capture.cjs`) runs the
page and takes five screenshots; `judge.py` scores them. `task-info/<case>/` describes one case to the Evolver, so a
learner trained on one case never sees another.

## Conditions

`pipeline.py` runs the agent in one of three conditions (arms):

| Arm | What the agent gets | How it is picked |
|---|---|---|
| A, `direct` | One request, no tools; the HTML is taken from the reply | `pipeline.run: ../../tasks/visual_showcase/pipeline.py:run_direct` |
| B, `harness` | A tool loop: `write_file`, `read_file`, `replace_text` and `inspect_page`, which runs the page in the browser and returns its screenshots | `pipeline.py:run` without an adapter, or with the untouched starting adapter |
| C, `adapted` | B, plus the adapter's knowledge in the system prompt and its read-only `wm` tool | `pipeline.py:run` with a learned adapter |

The pipeline tells B from C by the adapter's `adapter.yaml`: the Trainer adds an `origin` section only when it commits
a learned change. Limits of a B or C run: 600 seconds, 12 model requests, 24 tool calls, 3 inspections and 65,536
completion tokens (32,768 per response).

## Requirements

On macOS or Linux, from the repository root:

- Node 20 or newer, and the capturer's packages, pinned in `runtime/package.json`: Playwright (drives Chrome), pngjs
  (compares screenshots) and Three.js 0.180.0 (served to the page under `/vendor/`):

  ```bash
  npm ci --prefix examples/tasks/visual_showcase/runtime
  ```

  `VISUAL_NODE` names another `node`.
- Google Chrome in its standard place, or `VISUAL_CHROME` set to a Chrome executable. `npm ci` downloads no browser.
- `FIREWORKS_API_KEY` for the agent and the judge. It is read from the environment only, and removed from the
  browser's environment.

Without Three.js a run stops before its first model call. Without Playwright, pngjs or Chrome it still calls the
model and then fails its captures. To check the browser side first (free, about 30 seconds): `ls` should list the
Three.js file, and the capture of a blank page should print a line containing `"capture_ok":true`:

```bash
ls examples/tasks/visual_showcase/runtime/node_modules/three/build/three.module.min.js
d=$(mktemp -d) && mkdir "$d/page" && echo '<!doctype html><title>check</title>' > "$d/page/index.html"
node examples/tasks/visual_showcase/runtime/browser_capture.cjs --workspace "$d/page" --output "$d/out" --case waves
rm -r "$d"
```

## Prepare

```bash
python examples/tasks/visual_showcase/prepare.py
```

This writes `tasks.jsonl`, `splits/` and `task-info/` here. Each case has a training split (`splits/waves.txt`) and a
test split (`splits/waves-eval.txt`, the same brief under the id `waves-eval`).

## Runs and scores

- Each run writes its record to `.logs/` (or `$VISUAL_LOG_ROOT`); `task-info/<case>/judge.md` lists its files. Browser
  captures take turns on a machine (a file lock, `~/.cache/visual_showcase/capture.lock` or `$VISUAL_CAPTURE_LOCK`),
  so several runs can share it.
- `judge.py` checks 12 mechanical items of the final capture and has GLM 5.3 Flash rate the case's five review
  dimensions from its screenshots, without telling it the arm or the adapter (details in its docstring). The score is
  the rubric score, or 0 when it could not be obtained. The judge returns no feedback, the learner's evidence leaves
  out `judge/`, and the rubric's model cost is recorded there, outside the Trainer's budget. `VISUAL_JUDGE=mechanical`
  scores the mechanical items only, without a model call.

## Configs

[`mwm/visual_showcase`](../../mwm/).
