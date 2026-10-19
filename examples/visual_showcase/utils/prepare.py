#!/usr/bin/env python3
"""Write tasks.jsonl, the splits and task-info/<case>/ next to this file, from cases.json.

    python examples/visual_showcase/utils/prepare.py                       # experiment 1: the inspect_page toolset
    python examples/visual_showcase/utils/prepare.py --toolset generic     # experiment 2: the generic toolset

Rows: {id, case, title, title_zh, prompt, rubric, common_contract}; ids waves, lava, silk and the alias rows
waves-eval, lava-eval, silk-eval (identical except the id, so a config's test split differs from its train split).
Splits: splits/<case>.txt (train) and splits/<case>-eval.txt (test). Each case gets its own task-info directory, so a
learner trained on one case sees only that case's text. tasks.jsonl and splits/ are not committed; task-info/ is.

`--toolset generic` writes the same rows and splits (the brief is byte-identical across toolsets) plus
task-info-generic/<case>/{README.md, judge.md, environment.md} and task-info-generic/environment-mwm.md (gitignored):
the learner-facing texts for the acting agent that has only write_file/read_file/replace_text/run_command. README.md
quotes the acting system prompt verbatim from agent/texts.py and embeds environment.md, the machine facts F1-F15 with
the absolute paths confine.py resolves; probes/ holds the toolsmith self-tests' probe pages (probes.json lists them). `--out DIR` writes everywhere under DIR instead of next to this file (tests).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

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

# --- the generic toolset (experiment 2) ------------------------------------------------------------------------------

README_GENERIC = """# Visual showcase: {title}

The acting agent, GLM 5.3 Flash, writes one offline, interactive, animated web page, `index.html`, for the brief
below. This is the only case this adapter is trained on.

## How the agent runs

The agent works in a tool loop with four generic tools: `write_file` (a file in its workspace; `index.html` is the
only file submitted), `read_file` (numbered lines of a workspace file; `README.md` holds the brief), `replace_text`
(one exact replacement) and `run_command` (one `bash -c` command inside a sandbox whose working directory is the
workspace; no network; at most {command_s} seconds; PNG files the command creates or modifies in the workspace are
returned to the agent as images, at most five per command and forty per run; a JPEG is reported, not shown). There is
no browser tool. With a learned adapter, the adapter's context is appended to the system prompt and the tools the
adapter exports, if any, are offered next to the four above; an MWM adapter offers its read-only `wm` tool (`list`,
`memory`, `recall`, `search`, `read`, `trace`). Limits per run: {run_s} seconds, {requests} model requests,
{tool_calls} tool calls and 65,536 completion tokens. Only the last `index.html` is submitted. The brief's inspection
paragraph refers to the host's final observation after the run; the system prompt tells the agent so. `judge.md` says
how a run is observed and recorded.

The agent's system prompt, verbatim (`agent/texts.py`, `ACTING_SYSTEM_PROMPT`):

```text
{system_prompt}
```

## The brief (the agent's user message, verbatim)

{prompt}

## The common contract

The host-side contract shared by all briefs of this experiment (`cases.json` `common_contract`):

```json
{contract}
```

## Environment facts

{environment}"""

# judge.md for the generic toolset: the inspect_page sentence replaced, and the record layout of the generic harness
# (cmd-NN/ instead of checks/); each pair must match once so that an edit of JUDGE above cannot silently drift.
JUDGE_RECORD_INSPECT = """\
The run directory also holds `request.json` (the system prompt and tools the agent got), `calls/NN.json` (one record
per model request, without private reasoning), `tools/NN.json` (one record per tool call), `checks/` (the agent's own
inspections, as it saw them), `workspace/`, `submission/index.html` and `result.json` (status, stop reason, requests,
tool calls, inspections and token usage)."""
JUDGE_RECORD_GENERIC = """\
The run directory also holds `request.json` (the system prompt and tools the agent got), `calls/NN.json` (one record
per model request, without private reasoning), `tools/NN.json` (one record per tool call), `cmd-NN/` (one directory
per `run_command`: its launch trace and, when the command produced images, the `index.html` of that moment),
`workspace/`, `submission/index.html` and `result.json` (status, stop reason, requests, tool calls, commands, images
and token usage)."""
JUDGE_GENERIC_EDITS = (("`inspect_page` uses (headless Chrome", "the host uses (headless Chrome"),
                       (JUDGE_RECORD_INSPECT, JUDGE_RECORD_GENERIC))

ENVIRONMENT = """Facts about the machine where the acting agent's commands run (`run_command` and the tools an adapter
exports execute there, in the same sandbox). Paths are absolute and resolved. These are facts, not instructions.

- F1. Headless only: there is no display server (DISPLAY is unset); only headless programs run.
- F2. Node: node {node_version} is at `{node}`. It is not on the agent's PATH.
- F3. Node packages at `{node_modules}`: playwright {playwright}, playwright-core {playwright_core}, pngjs {pngjs},
  three {three}. CommonJS `require()` finds them when the child's environment has `NODE_PATH={node_modules}` or when
  the absolute path is required (`require('{node_modules}/playwright')`); ECMAScript `import` ignores NODE_PATH.
- F4. Chromium build {chromium_build} (Chrome for Testing {chromium_version}) is at `{chrome_bin}`. Playwright's
  bundled-browser lookup fails under HOME=/tmp/home unless `PLAYWRIGHT_BROWSERS_PATH={playwright_dir}` is set or
  `executablePath` is given. Playwright passes `--no-sandbox` itself.
- F5. WebGL draws on the GPU only when Chromium starts with `--use-gl=angle --use-angle=gl-egl --ignore-gpu-blocklist`;
  on Chromium's default software path a WebGL canvas comes out blank white with no error. `{wrapper}` is an executable
  that starts Chromium with those flags (usable as `executablePath`); it finds the Chromium of F4 by itself. Only
  GPU 0 is visible inside the sandbox.
- F6. `python3` on the agent's PATH is `{python}` (Python {python_version}; standard library{python_extras}; no
  playwright package, no PIL). A browser is driven by spawning node.
- F7. No network; loopback (127.0.0.1) works. `index.html` imports `/vendor/three.module.min.js` and
  `/vendor/three.core.min.js` by absolute URL; the host runs the page from a loopback HTTP server that serves the
  workspace at `/` and maps `/vendor/<file>` to `{node_modules}/three/build/<file>`; a `file://` load cannot resolve
  those URLs, and Chromium blocks module scripts from `file://`.
- F8. Writable: the workspace (the working directory) only. `/tmp` is private per command and discarded afterwards, so
  outputs must go under the working directory. HOME=/tmp/home.
- F9. Limits: a `run_command` takes at most {command_s} seconds, a tool at most {tool_s} seconds, a run {run_s} seconds;
  a host lock may delay a command (the wait is not charged to its timeout).
- F10. PNG files that a command or tool writes or modifies under the workspace are returned to the agent as images
  (at most {images_per_call} per call, {images_per_run} per run, {image_mb} MB and {image_mpx} megapixels each); a JPEG is
  reported as rejected, never shown, and the self-tests count only non-blank PNGs.
- F11. Inside the sandbox only `/usr`, a few `/etc` files, `/sys`, the venv `{prefix}`, its base Python
  `{base_prefix}`, node `{node_dir}`, `{node_modules}`, the browsers directory `{playwright_dir}` and the wrapper exist;
  no other host path. The agent's PATH is `{prefix}/bin:/usr/bin:/bin`; its environment holds only PATH, HOME, TMPDIR,
  XDG_CACHE_HOME, XDG_CONFIG_HOME, LANG and TERM (no NODE, NODE_PATH, PLAYWRIGHT_* or VISUAL_* variable).
- F12. The agent's tools are {tools}, plus the tools an adapter exports. Its system prompt, verbatim:

```text
{system_prompt}
```

- F13. From the brief: `window.__SHOWCASE__.getState()` returns ready, paused, simTime, intensity and resetCount, with
  ready true only after the initial scene has been drawn; viewports {desktop} (desktop) and {mobile} (mobile); the host
  observes the page near 0, 2 and 5 seconds after ready, then after pause, reset and intensity {inspection}, then at the
  mobile viewport.
- F14. One model request of the agent may hold several tool calls: each call counts against the {tool_calls} tool calls,
  the request once against the {requests} requests; at most {adapter_calls} of the calls may be calls of adapter tools (a
  further one returns an error and no image). Only the images of a request's last image-bearing call are attached:
  first to the agent's next request, and to its later requests until a later image-bearing call replaces them. Images
  returned during the agent's last request never reach it.
- F15. After the run, the host opens `index.html` from its loopback server with a {load_s}-second load timeout (a page
  that has not loaded by then is not observed at all), then waits at most {ready_s} seconds for `getState().ready` to
  become true (a page that is not ready by then fails that check and is observed as it is); its whole observation ends
  after {observe_s} seconds. It records whether the pause, reset and intensity controls lie entirely inside the
  viewport at {desktop} and at {mobile}.
"""

MWM_PREAMBLE = """# The acting agent's machine

This file describes the machine of the ACTING agent, the one whose `index.html` runs you learn from: what its
`run_command` tool and the tools an adapter exports find there. It does not describe your own session. Your own tools
(`list_files`, `read_file`, `read_image`, `write_file`, `delete_path`, `move_path`, `validate`, `get_policy`), areas and
limits are unchanged: the adapter is the only writable area, the default command working directory is
`{working_directory}`, evidence, method, history and this environment area are read-only, and you have no network.

"""

# The acting harness's limits (agent/agent.py, pinned by the experiment contract); repeated here as facts.
LIMITS = {"command_s": 300, "tool_s": 300, "run_s": 1800,  # generic_harness.MAX_TIMEOUT_S, toolsmith's tool maximum,
                                                         # agent.RUN_SECONDS (90, 90 and 600 s before experiment 4)
          "images_per_call": 5, "images_per_run": 40,     # why: five is one capture's worth; forty bounds a run's bytes
          "image_mb": 2, "image_mpx": 2.1,               # why: pngstats decodes up to 2.2 Mpx; 1280x800 is 1.0 Mpx
          "requests": 12, "tool_calls": 24,              # agent.MAX_REQUESTS, generic_harness.MAX_TOOL_CALLS
          "adapter_calls": 6}                            # generic_harness.MAX_ADAPTER_CALLS
# The host's observation deadlines (runtime/browser_capture.cjs: goto timeout, ready wait, the whole body).
HOST = {"load_s": 60, "ready_s": 20, "observe_s": 240}     # 10, 4 and 40 s before experiment 4
# Probe pages for the toolsmith self-tests (task-info-generic/<case>/probes/): pages whose correct picture is known.
PROBES = [{"page": "webgl_probe.html", "label": "webgl-solid-colour", "expect_rgb": [30, 120, 200], "tolerance": 10,
           "note": "a page with the contract's controls whose WebGL scene canvas is cleared to rgb(30,120,200) every "
                   "frame; a true picture of it is dominated by that colour"}]


def load_by_path(name: str, path: Path):
    """Import a sibling module by file path (the examples are not a package)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _package_version(node_modules: str, package: str) -> str:
    try:
        return str(json.loads(Path(node_modules, package, "package.json").read_text())["version"])
    except (OSError, ValueError, KeyError):
        return "unknown"


def _chromium(chrome_bin: str, node_modules: str) -> tuple[str, str]:
    """(build, browser version) of the Playwright Chromium at chrome_bin, from its path and playwright-core's list."""
    match = re.search(r"chromium-(\d+)", chrome_bin)
    build = match.group(1) if match else "unknown"
    try:
        listed = json.loads(Path(node_modules, "playwright-core", "browsers.json").read_text())["browsers"]
        version = next(b["browserVersion"] for b in listed if b.get("name") == "chromium" and b.get("revision") == build)
    except (OSError, ValueError, KeyError, StopIteration):
        version = "unknown"
    return build, version


def _python_facts(python: str) -> tuple[str, str]:
    """(version, extras text) of the agent's python3, asked of that interpreter itself (it may differ from ours)."""
    probe = ("import importlib.util as u, platform, json; print(json.dumps({'v': platform.python_version(), "
             "'yaml': u.find_spec('yaml') is not None, 'jsonschema': u.find_spec('jsonschema') is not None}))")
    try:
        out = json.loads(subprocess.run([python, "-I", "-c", probe], capture_output=True, text=True, timeout=30,
                                        check=True).stdout.strip().splitlines()[-1])
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        return "3.12", ""
    extras = [name for name, present in (("PyYAML", out.get("yaml")), ("jsonschema", out.get("jsonschema"))) if present]
    return str(out.get("v", "3.12")), (", " + " and ".join(extras)) if extras else ""


def environment_facts(confine, texts, contract: dict) -> str:
    """The environment.md text: F1-F15 with the paths confine resolved and the acting prompt from texts."""
    build, version = _chromium(confine.CHROME_BIN, confine.NODE_MODULES)
    python_version, python_extras = _python_facts(confine.PYTHON)
    values = {
        "node": confine.NODE, "node_version": os.path.basename(confine.NODE_DIR), "node_dir": confine.NODE_DIR,
        "node_modules": confine.NODE_MODULES, "chrome_bin": confine.CHROME_BIN, "playwright_dir": confine.PLAYWRIGHT_DIR,
        "wrapper": confine.WRAPPER, "python": confine.PYTHON, "prefix": confine.PREFIX,
        "base_prefix": confine.BASE_PREFIX,
        "playwright": _package_version(confine.NODE_MODULES, "playwright"),
        "playwright_core": _package_version(confine.NODE_MODULES, "playwright-core"),
        "pngjs": _package_version(confine.NODE_MODULES, "pngjs"),
        "three": _package_version(confine.NODE_MODULES, "three"),
        "chromium_build": build, "chromium_version": version,
        "python_version": python_version, "python_extras": python_extras,
        "tools": ", ".join(texts.PRIMITIVES), "system_prompt": texts.ACTING_SYSTEM_PROMPT,
        "desktop": "×".join(str(v) for v in contract["desktop"]), "mobile": "×".join(str(v) for v in contract["mobile"]),
        "inspection": contract["intensity"]["inspection"], **LIMITS, **HOST,
    }
    return ENVIRONMENT.format(**values)


def judge_generic() -> str:
    text = JUDGE
    for old, new in JUDGE_GENERIC_EDITS:
        if text.count(old) != 1:
            raise RuntimeError(f"prepare: the judge text no longer contains exactly once: {old!r}")
        text = text.replace(old, new)
    return text


def generic_files(data: dict, contract: dict) -> dict[str, str]:
    """task-info-generic/** as {relative path: text}; imports confine.py (raises when node/Chromium are missing)."""
    texts = load_by_path("visual_texts", HERE / "agent" / "texts.py")
    confine = load_by_path("visual_confine", HERE / "confine.py")
    environment = environment_facts(confine, texts, contract)
    files = {"task-info-generic/environment-mwm.md": MWM_PREAMBLE + environment}
    for c in data["cases"]:
        files[f"task-info-generic/{c['id']}/README.md"] = README_GENERIC.format(
            title=c["title_en"], system_prompt=texts.ACTING_SYSTEM_PROMPT, prompt=c["prompt"],
            contract=json.dumps(contract, ensure_ascii=False, indent=2), environment=environment, **LIMITS)
        files[f"task-info-generic/{c['id']}/judge.md"] = judge_generic()
        files[f"task-info-generic/{c['id']}/environment.md"] = environment
        for probe in PROBES:
            files[f"task-info-generic/{c['id']}/probes/{probe['page']}"] = (HERE / "reference" / probe["page"]).read_text()
        files[f"task-info-generic/{c['id']}/probes/probes.json"] = json.dumps(PROBES, indent=1) + "\n"
    return files


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--toolset", choices=("inspect", "generic"), default="inspect",
                        help="inspect (default): task-info/ for the inspect_page harness; generic: task-info-generic/")
    parser.add_argument("--out", type=Path, default=HERE,
                        help="write under this directory instead of next to prepare.py")
    args = parser.parse_args(argv)
    data = json.loads((HERE / "cases.json").read_text())
    contract = data["common_contract"]
    rows = [{"id": c["id"], "case": c["id"], "title": c["title_en"], "title_zh": c["title_zh"], "prompt": c["prompt"],
             "rubric": c["rubric"], "common_contract": contract} for c in data["cases"]]
    rows += [{**row, "id": row["case"] + "-eval"} for row in list(rows)]
    files = {"tasks.jsonl": "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)}
    for c in data["cases"]:
        files[f"splits/{c['id']}.txt"] = c["id"] + "\n"
        files[f"splits/{c['id']}-eval.txt"] = c["id"] + "-eval\n"
        if args.toolset == "inspect":
            files[f"task-info/{c['id']}/README.md"] = README.format(
                title=c["title_en"], prompt=c["prompt"], contract=json.dumps(contract, ensure_ascii=False, indent=2))
            files[f"task-info/{c['id']}/judge.md"] = JUDGE
    if args.toolset == "generic":
        files.update(generic_files(data, contract))
    for relative, text in files.items():
        path = args.out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    print({"tasks": len(rows), "files": len(files)})


if __name__ == "__main__":
    main()
