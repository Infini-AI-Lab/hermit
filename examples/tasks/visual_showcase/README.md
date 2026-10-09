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

On macOS or Linux:

- Node 20 or newer, and the capturer's packages (Playwright 1.62.1, pngjs 7.0.0) in `runtime/node_modules`:

  ```bash
  npm ci --prefix examples/tasks/visual_showcase/runtime
  ```

  `VISUAL_NODE` names another `node`, and `VISUAL_NODE_PATH` another folder with these packages.
- Google Chrome in its standard place, or `VISUAL_CHROME` set to a Chrome executable.
- `FIREWORKS_API_KEY` for the agent and the judge. It is read from the environment only, and removed from the
  browser's environment.

A run checks the browser side first: node's version, the packages' exact versions and Chrome, which it records in
`request.json`. A failure that every later run would repeat (no key, HTTP 401 or 403, a missing or changed browser
runtime) stops training; `train` continues from the last completed step once it is fixed.

## Prepare

```bash
python examples/tasks/visual_showcase/prepare.py
```

This checks `cases.json` and `runtime/` against their frozen sha256, warns when the browser side is missing, and
writes `tasks.jsonl`, `splits/` and `task-info/` here. Each case has a training split (`splits/waves.txt`) and a test
split (`splits/waves-eval.txt`, the same brief under the id `waves-eval`). `--check` writes nothing and reports
whether the files are up to date.

## Runs and scores

- Each run writes its record to `.logs/` (or `$VISUAL_LOG_ROOT`); `task-info/<case>/judge.md` lists its files. Browser
  captures take turns on a machine (a file lock, `~/.cache/visual_showcase/capture.lock` or `$VISUAL_CAPTURE_LOCK`),
  so several runs can share it.
- `judge.py` checks 12 mechanical items of the final capture and has GLM 5.3 Flash rate the case's five review
  dimensions from its screenshots, without telling it the arm or the adapter (details in its docstring). The score is
  the rubric score, or 0 when it could not be obtained. The judge returns no feedback, the learner's evidence leaves
  out `judge/`, and the rubric's model cost is recorded there, outside the Trainer's budget. `VISUAL_JUDGE=mechanical`
  scores the mechanical items only.
- `python examples/tasks/visual_showcase/judge.py <run dir>` scores a saved run again. It calls the model (paid; needs
  `FIREWORKS_API_KEY`).
- `VISUAL_SPEED_MIN` (tokens per second) makes each run and learner session wait until the provider is that fast; the
  waiting counts toward `trainer.budget.hours` and `call_timeout_min` (see `speedgate.py`).
- `python -m pytest tests/visual_showcase` runs the tests offline with a scripted model; with
  `VISUAL_REAL_CAPTURE=1`, two more launch Chrome (about 3 minutes).

## Configs

[`mwm/visual_showcase`](../../mwm/)
