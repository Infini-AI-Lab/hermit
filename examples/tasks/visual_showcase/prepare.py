#!/usr/bin/env python3
"""Write tasks.jsonl, the splits and task-info/<case>/ next to this file, from cases.json (a byte copy of the old
experiment's file).

    python examples/tasks/visual_showcase/prepare.py           # check the frozen inputs, then (re)write the files
    python examples/tasks/visual_showcase/prepare.py --check   # check the inputs and that the files are up to date

Rows: {id, case, title, title_zh, prompt, rubric, common_contract}; ids waves, lava, silk and the alias rows
waves-eval, lava-eval, silk-eval (identical except the id, so a config's test split differs from its train split).
Splits: splits/<case>.txt (train) and splits/<case>-eval.txt (test). Each case gets its own task-info directory, so a
learner trained on one case sees only that case's text. tasks.jsonl and splits/ are not committed; task-info/ is.
"""
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from harness import RUNTIME, RUNTIME_SHA256, check_browser  # noqa: E402

CASES_SHA256 = "6b022e3bdbfa53f3bb88bcb626f5794242521b5a28f27bb691f223eb0b5211cb"
PROMPT_SHA256 = {"waves": "89aec6e7", "lava": "dccd0345", "silk": "4b100d2e"}     # prefixes, from the old settings sheet
LICENSE_SHA256 = "bfe119ea4fd413f5f7ca3fcd63adb0c4a073ed39daa2fe7d3e6b769e21272601"   # runtime/vendor/LICENSE (Three.js, MIT)

README = """# Visual showcase: {title}

The acting agent, GLM 5.3 Flash, writes one offline, interactive, animated web page, `index.html`, for the brief
below. This is the only case this adapter is trained on.

## How the agent runs

The agent works in a tool loop: `write_file` (the whole `index.html`), `read_file` (`index.html` or the brief as
`README.md`), `replace_text` (one exact replacement) and `inspect_page` (runs the current page in an offline Chrome and
returns its observations and five screenshots). With a learned adapter, the adapter's knowledge package is appended to
the system prompt and the read-only `wm` tool (`list`, `memory`, `recall`, `search`, `read`, `trace`) is offered too.
Limits per run: 600 seconds, 12 model requests, 24 tool calls, 3 inspections and 65,536 completion tokens. Only the
last `index.html` is submitted. `judge.md` says how a run is observed and recorded.

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


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_inputs() -> dict:
    cases_path = HERE / "cases.json"
    if sha(cases_path) != CASES_SHA256:
        raise SystemExit("cases.json differs from the old experiment's bytes")
    for relative, expected in {**RUNTIME_SHA256, "vendor/LICENSE": LICENSE_SHA256}.items():
        if not (RUNTIME / relative).is_file() or sha(RUNTIME / relative) != expected:
            raise SystemExit(f"runtime/{relative} is missing or differs from the frozen bytes")
    data = json.loads(cases_path.read_text())
    for case in data["cases"]:
        if not hashlib.sha256(case["prompt"].encode()).hexdigest().startswith(PROMPT_SHA256[case["id"]]):
            raise SystemExit(f"the {case['id']} prompt differs from the old one")
    return data


def generated(data: dict) -> dict[str, str]:
    """Every generated file: relative path -> text."""
    contract = data["common_contract"]
    rows = [{"id": c["id"], "case": c["id"], "title": c["title_en"], "title_zh": c["title_zh"], "prompt": c["prompt"],
             "rubric": c["rubric"], "common_contract": contract} for c in data["cases"]]
    rows += [{**row, "id": row["case"] + "-eval"} for row in list(rows)]
    out = {"tasks.jsonl": "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)}
    for c in data["cases"]:
        out[f"splits/{c['id']}.txt"] = c["id"] + "\n"
        out[f"splits/{c['id']}-eval.txt"] = c["id"] + "-eval\n"
        out[f"task-info/{c['id']}/README.md"] = README.format(
            title=c["title_en"], prompt=c["prompt"], contract=json.dumps(contract, ensure_ascii=False, indent=2))
        out[f"task-info/{c['id']}/judge.md"] = JUDGE
    return out


def main(argv: list[str]) -> int:
    files = generated(check_inputs())
    try:
        check_browser()
    except RuntimeError as exc:
        print(f"warning: {exc}; runs cannot capture until it is installed")
    if "--check" in argv:
        stale = [p for p, text in files.items() if not (HERE / p).is_file() or (HERE / p).read_text() != text]
        print("up to date" if not stale else f"stale or missing: {stale}")
        return 1 if stale else 0
    for relative, text in files.items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    print({"tasks": sum(1 for _ in files["tasks.jsonl"].splitlines()), "files": len(files)})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
