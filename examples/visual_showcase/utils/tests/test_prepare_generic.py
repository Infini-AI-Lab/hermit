"""prepare.py: the rows and splits, and the learner texts: a README that quotes the brief, the contract and the
acting system prompt verbatim and embeds the environment facts, a judge.md without inspect_page, and environment.md with
F1-F15 and the paths confine.py resolves."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re

import pytest

UTILS = Path(__file__).resolve().parents[1]
CASES = ("waves", "lava", "silk")
WORDS = re.compile(r"screenshot|render|capture", re.IGNORECASE)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = _load("visual_prepare", UTILS / "prepare.py")
texts = _load("visual_texts", UTILS / "agent" / "texts.py")
try:
    confine = _load("visual_confine", UTILS / "confine.py")
except RuntimeError as exc:          # node, Chromium or node_modules missing on this machine
    confine = None
    CONFINE_ERROR = str(exc)
needs_confine = pytest.mark.skipif(confine is None, reason="confine cannot resolve its paths here")


@needs_confine
def test_rows_and_splits(tmp_path, capsys):
    prepare.main(["--out", str(tmp_path)])
    rows = [json.loads(line) for line in (tmp_path / "tasks.jsonl").read_text().splitlines()]
    assert [r["id"] for r in rows] == ["waves", "lava", "silk", "waves-eval", "lava-eval", "silk-eval"]
    assert rows[0]["prompt"] == rows[3]["prompt"] and rows[0]["case"] == rows[3]["case"] == "waves"
    assert (tmp_path / "splits" / "waves.txt").read_text() == "waves\n"
    assert (tmp_path / "splits" / "silk-eval.txt").read_text() == "silk-eval\n"
    assert "'files': 22" in capsys.readouterr().out


@needs_confine
def test_generic_toolset_texts(tmp_path):
    prepare.main(["--out", str(tmp_path)])
    root = tmp_path / "task-info-generic"
    assert sorted(p.name for p in root.iterdir()) == ["lava", "silk", "waves"]
    probes = json.loads((root / "waves" / "probes" / "probes.json").read_text())
    assert [x["page"] for x in probes] == ["webgl_probe.html"] and probes[0]["expect_rgb"] == [30, 120, 200]
    assert (root / "waves" / "probes" / "webgl_probe.html").read_text() == (UTILS / "reference" / "webgl_probe.html").read_text()
    env = (root / "waves" / "environment.md").read_text()
    readme = (root / "waves" / "README.md").read_text()
    judge = (root / "waves" / "judge.md").read_text()
    fenced = f"```text\n{texts.ACTING_SYSTEM_PROMPT}\n```"

    # environment.md: the fifteen facts with the resolved paths, and no inspection words outside the quoted prompt
    for n in range(1, 16):
        assert f"- F{n}." in env, n
    flat_env = " ".join(env.split())
    assert "counts against the 24 tool calls, the request once against the 12 requests" in flat_env
    assert "Images returned during the agent's last request never reach it" in flat_env
    assert "at most 6 of the calls may be calls of adapter tools" in flat_env
    assert "until a later image-bearing call replaces them" in flat_env
    assert prepare.LIMITS["adapter_calls"] == 6 and prepare.LIMITS["tool_calls"] == 24 and prepare.LIMITS["requests"] == 12
    assert "60-second load timeout (a page that has not loaded by then is not observed at all)" in flat_env
    assert "at most 20 seconds for `getState().ready`" in flat_env
    assert "ends after 240 seconds" in flat_env
    assert ("a `run_command` takes at most 300 seconds and a tool at most 300 seconds; a run has no time limit, "
            "and a model request is abandoned only when it hangs for 1800 seconds") in flat_env
    acting = (UTILS / "agent" / "agent.py").read_text()   # the facts state the agent's own limits
    assert re.search(r"^RUN_SECONDS: float \| None = None ", acting, re.M)
    assert int(re.search(r"^REQUEST_GUARD_S = (\d+)", acting, re.M).group(1)) == prepare.LIMITS["request_guard_s"]
    assert "the run itself has no time limit" in texts.LOOP_GENERIC and "1800 seconds" not in texts.LOOP_GENERIC
    # the facts state the capturer's own deadlines (runtime/browser_capture.cjs TIMEOUTS_MS)
    capturer = (UTILS / "runtime" / "browser_capture.cjs").read_text()
    deadlines = {k: int(v) for k, v in re.findall(r"\b(load|ready|observe):(\d+)", capturer.split("};")[0])}
    assert deadlines == {"load": 1000 * prepare.HOST["load_s"], "ready": 1000 * prepare.HOST["ready_s"],
                         "observe": 1000 * prepare.HOST["observe_s"]}, deadlines
    for value in (confine.NODE, confine.NODE_MODULES, confine.CHROME_BIN, confine.WRAPPER, confine.PYTHON,
                  confine.PREFIX, confine.BASE_PREFIX, confine.NODE_DIR, confine.PLAYWRIGHT_DIR):
        assert value in env, value
    assert f"NODE_PATH={confine.NODE_MODULES}" in env and f"PLAYWRIGHT_BROWSERS_PATH={confine.PLAYWRIGHT_DIR}" in env
    assert "--use-gl=angle --use-angle=gl-egl --ignore-gpu-blocklist" in env and "Only\n  GPU 0 is visible" in env
    assert "/vendor/three.module.min.js" in env and "`file://`" in env and "loopback" in env
    assert "discarded" in env and "HOME=/tmp/home" in env and "spawning node" in env
    assert "ECMAScript `import` ignores NODE_PATH" in env and "`executablePath`" in env
    for package in ("playwright", "pngjs", "three"):
        version = json.loads((Path(confine.NODE_MODULES) / package / "package.json").read_text())["version"]
        assert f"{package} {version}" in env
    assert "Chromium build " in env and "unknown" not in env
    assert "1280×800" in env and "390×844" in env and "0, 2 and 5 seconds" in env and "0.75" in env
    assert env.count(fenced) == 1
    assert not WORDS.search(env.replace(fenced, "")), "only the quoted prompt may mention inspection words"
    assert "{" not in env.replace(fenced, "").replace("{}", ""), "no unfilled placeholder"

    # README: the brief and the contract verbatim, the tool list, the prompt verbatim, the facts embedded
    data = json.loads((UTILS / "cases.json").read_text())
    waves = next(c for c in data["cases"] if c["id"] == "waves")
    contract = json.dumps(data["common_contract"], ensure_ascii=False, indent=2)
    assert readme.startswith(f"# Visual showcase: {waves['title_en']}\n")
    assert f"## The brief (the agent's user message, verbatim)\n\n{waves['prompt']}\n" in readme
    assert f"```json\n{contract}\n```" in readme
    assert "## How the agent runs\n" in readme and readme.count(fenced) == 2       # the section and F12 of the facts
    assert readme.endswith("## Environment facts\n\n" + env)
    flat = " ".join(readme.split())           # the README wraps at ~120 columns
    for phrase in ("`write_file`", "`read_file`", "`replace_text`", "`run_command`", "There is no browser tool",
                   "12 model requests, 24 tool calls and 65,536 completion tokens, and no time limit"):
        assert phrase in flat, phrase
    assert "inspect_page" not in readme and "3 inspections" not in readme

    # judge.md: the host's capture and the record layout of the agent's harness
    assert judge == prepare.JUDGE_GENERIC and "inspect_page" not in judge
    assert judge.count("the host uses (headless Chrome") == 1 and "`cmd-NN/`" in judge and "checks/" not in judge

    # every case gets the same facts and its own brief
    for case in CASES:
        assert (root / case / "environment.md").read_text() == env
        assert (root / case / "judge.md").read_text() == judge
        text = (root / case / "README.md").read_text()
        assert text.startswith("# Visual showcase: ") and text.endswith(env)
    titles = {(root / case / "README.md").read_text().splitlines()[0] for case in CASES}
    assert len(titles) == 3
