#!/usr/bin/env python3
"""Write tasks.jsonl, the splits and task-info/<case>/ next to this file, from cases.json.

    python examples/visual_showcase/utils/prepare.py

Rows: {id, case, title, title_zh, prompt, rubric, common_contract}; ids waves, lava, silk and the alias rows
waves-eval, lava-eval, silk-eval (identical except the id, so a config's test split differs from its train split).
Splits: splits/<case>.txt (train) and splits/<case>-eval.txt (test). Each case gets its own task-info directory, so a
learner trained on one case sees only that case's text. tasks.jsonl and splits/ are not committed; task-info/ is.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

README = """# Visual showcase: {title}

The acting agent, GLM 5.3 Flash, writes one offline, interactive, animated web page, `index.html`, for the brief
below. This is the only case this adapter is trained on.

## How the agent runs

The agent works in a tool loop: `write_file` (the whole `index.html`), `read_file` (`index.html` or the brief as
`README.md`), `replace_text` (one exact replacement) and `inspect_page` (runs the current page in an offline Chrome and
returns its observations and five screenshots). With a learned adapter, the adapter's knowledge is appended to the
system prompt; an MWM adapter also offers its read-only `wm` tool (`list`, `memory`, `recall`, `search`, `read`,
`trace`). Limits per run: 600 seconds, 12 model requests, 24 tool calls, 3 inspections and 65,536 completion tokens.
Only the last `index.html` is submitted. `judge.md` says how a run is observed and recorded.

## The brief (the agent's user message, verbatim)

{prompt}

## The common contract

The host-side contract shared by all briefs of this experiment (`cases.json` `common_contract`):

```json
{contract}
```
"""

JUDGE = """# How a run is observed

After the agent's run ends, the host runs the submitted `index.html` once more with the same browser capturer that
`inspect_page` uses (headless Chrome, offline; only `index.html` and the two Three.js files are served). This final
capture is never shown to the agent. It writes the run's `capture/` directory:

- `desktop-t0.png`, `desktop-t2.png`, `desktop-t5.png`: 1280×800 screenshots about 0, 2 and 5 seconds after the page
  reports ready, at the initial intensity 0.4;
- `desktop-interaction.png`: about two seconds after pause, reset and setting the intensity to 0.75;
- `phone.png`: the 390×844 viewport after the same interaction;
- `diagnostic-paused.png`: the scene after the pause interval, kept for diagnosis;
- `summary.json`: page and console errors, whether `getState().ready` became true, the fraction of scene pixels that
  changed between the timed screenshots, the pause, reset, intensity and keyboard checks, control positions in both
  viewports, frame timing and the `getState` values reported at each screenshot.

The run directory also holds `request.json` (the system prompt and tools the agent got), `calls/NN.json` (one record
per model request, without private reasoning), `tools/NN.json` (one record per tool call), `checks/` (the agent's own
inspections, as it saw them), `workspace/`, `submission/index.html` and `result.json` (status, stop reason, requests,
tool calls, inspections and token usage).

Frame differences show visible change, not physical correctness or beauty; `getState` values are the page's own
reports.
"""


def main() -> None:
    data = json.loads((HERE / "cases.json").read_text())
    contract = data["common_contract"]
    rows = [{"id": c["id"], "case": c["id"], "title": c["title_en"], "title_zh": c["title_zh"], "prompt": c["prompt"],
             "rubric": c["rubric"], "common_contract": contract} for c in data["cases"]]
    rows += [{**row, "id": row["case"] + "-eval"} for row in list(rows)]
    files = {"tasks.jsonl": "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)}
    for c in data["cases"]:
        files[f"splits/{c['id']}.txt"] = c["id"] + "\n"
        files[f"splits/{c['id']}-eval.txt"] = c["id"] + "-eval\n"
        files[f"task-info/{c['id']}/README.md"] = README.format(
            title=c["title_en"], prompt=c["prompt"], contract=json.dumps(contract, ensure_ascii=False, indent=2))
        files[f"task-info/{c['id']}/judge.md"] = JUDGE
    for relative, text in files.items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    print({"tasks": len(rows), "files": len(files)})


if __name__ == "__main__":
    main()
