"""prepare.py: the default output stays byte-identical to the committed task-info/ (experiment 1), and
`--toolset generic` writes the learner texts of the generic toolset: a README that quotes the acting system prompt
verbatim and embeds the environment facts, a judge.md without inspect_page, environment.md with F1-F15 and the paths
confine.py resolves, environment-mwm.md with its preamble; plus the mwm evolver's new environment_file param."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re

import pytest
import yaml

UTILS = Path(__file__).resolve().parents[1]
MWM = UTILS.parent / "mwm_visual" / "mwm_visual"
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


def test_default_output_is_byte_identical_to_the_committed_task_info(tmp_path, capsys):
    prepare.main(["--out", str(tmp_path)])
    for case in CASES:
        for name in ("README.md", "judge.md"):
            mine, committed = tmp_path / "task-info" / case / name, UTILS / "task-info" / case / name
            assert mine.read_bytes() == committed.read_bytes()
    rows = [json.loads(line) for line in (tmp_path / "tasks.jsonl").read_text().splitlines()]
    assert [r["id"] for r in rows] == ["waves", "lava", "silk", "waves-eval", "lava-eval", "silk-eval"]
    assert rows[0]["prompt"] == rows[3]["prompt"] and rows[0]["case"] == rows[3]["case"] == "waves"
    assert (tmp_path / "splits" / "waves.txt").read_text() == "waves\n"
    assert (tmp_path / "splits" / "silk-eval.txt").read_text() == "silk-eval\n"
    assert not (tmp_path / "task-info-generic").exists()
    assert "'files': 13" in capsys.readouterr().out


def test_judge_generic_edits_apply_exactly_once():
    generic = prepare.judge_generic()
    assert "inspect_page" not in generic and generic.count("the host uses (headless Chrome") == 1
    assert "`cmd-NN/`" in generic and "checks/" not in generic
    original = prepare.JUDGE
    try:
        prepare.JUDGE = original.replace("`inspect_page` uses (headless Chrome",
                                         "`inspect_page` drives (headless Chrome")
        with pytest.raises(RuntimeError, match="no longer contains exactly once"):
            prepare.judge_generic()
    finally:
        prepare.JUDGE = original


@needs_confine
def test_generic_toolset_texts(tmp_path):
    prepare.main(["--toolset", "generic", "--out", str(tmp_path)])
    root = tmp_path / "task-info-generic"
    assert not (tmp_path / "task-info").exists()
    assert sorted(p.name for p in root.iterdir()) == ["environment-mwm.md", "lava", "silk", "waves"]
    probes = json.loads((root / "waves" / "probes" / "probes.json").read_text())
    assert [x["page"] for x in probes] == ["webgl_probe.html"] and probes[0]["expect_rgb"] == [30, 120, 200]
    assert (root / "waves" / "probes" / "webgl_probe.html").read_text() == (UTILS / "reference" / "webgl_probe.html").read_text()
    env = (root / "waves" / "environment.md").read_text()
    readme = (root / "waves" / "README.md").read_text()
    judge = (root / "waves" / "judge.md").read_text()
    mwm = (root / "environment-mwm.md").read_text()
    fenced = f"```text\n{texts.ACTING_SYSTEM_PROMPT}\n```"

    # environment.md: the fifteen facts with the resolved paths, and no inspection words outside the quoted prompt
    for n in range(1, 16):
        assert f"- F{n}." in env, n
    flat_env = " ".join(env.split())
    assert "counts against the 24 tool calls, the request once against the 12 requests" in flat_env
    assert "images returned during the agent's last request never reach it" in flat_env
    assert "10-second load timeout" in flat_env and "at most 4 seconds for `getState().ready`" in flat_env
    assert "ends after 40 seconds" in flat_env
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

    # README: same brief and contract as experiment 1, the generic tool list, the prompt verbatim, the facts embedded
    inspect_readme = (UTILS / "task-info" / "waves" / "README.md").read_text()
    head, rest = inspect_readme.split("## How the agent runs\n")
    brief_and_contract = rest.split("## The brief (the agent's user message, verbatim)\n")[1]
    assert readme.startswith(head) and brief_and_contract in readme
    assert "## How the agent runs\n" in readme and readme.count(fenced) == 2       # the section and F12 of the facts
    assert readme.endswith("## Environment facts\n\n" + env)
    flat = " ".join(readme.split())           # the README wraps at ~120 columns
    for phrase in ("`write_file`", "`read_file`", "`replace_text`", "`run_command`", "There is no browser tool",
                   "600 seconds, 12 model requests, 24 tool calls and 65,536 completion tokens"):
        assert phrase in flat, phrase
    assert "inspect_page" not in readme and "3 inspections" not in readme

    # judge.md and environment-mwm.md
    assert judge == prepare.judge_generic() and "inspect_page" not in judge
    assert mwm.startswith("# The acting agent's machine\n") and mwm.endswith(env)
    assert "ACTING agent" in mwm and "{working_directory}" in mwm and "{working_directory}" not in env

    # every case gets the same facts and its own brief
    for case in CASES:
        assert (root / case / "environment.md").read_text() == env
        assert (root / case / "judge.md").read_text() == judge
        text = (root / case / "README.md").read_text()
        assert text.startswith("# Visual showcase: ") and text.endswith(env)
    titles = {(root / case / "README.md").read_text().splitlines()[0] for case in CASES}
    assert len(titles) == 3


@needs_confine
def test_environment_mwm_fills_only_the_working_directory(tmp_path):
    prepare.main(["--toolset", "generic", "--out", str(tmp_path)])
    evolver = _load("mwm_visual_evolver", MWM / "evolver.py")
    text = (tmp_path / "task-info-generic" / "environment-mwm.md").read_text()
    filled = evolver.fill(text, {"working_directory": "/work/adapter"})
    assert "`/work/adapter`" in filled and "{working_directory}" not in filled
    assert filled.replace("/work/adapter", "{working_directory}") == text


def test_mwm_evolver_has_the_environment_file_param():
    evolver = _load("mwm_visual_evolver", MWM / "evolver.py")
    assert evolver.DEFAULTS["environment_file"] == "environment.md"
    params = yaml.safe_load((MWM / "evolver.yaml").read_text())["params"]
    assert params["environment_file"] == "environment.md"
    source = (MWM / "evolver.py").read_text()
    assert 'fill((directory / p["environment_file"]).read_text()' in source
    assert (MWM / "environment.md").is_file()
