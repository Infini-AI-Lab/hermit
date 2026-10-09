"""The task side reproduces the old experiment's frozen inputs: system prompts, tool schemas, transport settings,
cases.json, the capturer's protocol and the Three.js files; and prepare.py writes the task files. Offline.

The sha256 values are the old freeze.json's (system_prompts, tools) and file hashes. Tests marked `needs_old` also
compare against the old experiment's files (read-only); they run only when VISUAL_OLD_EXPERIMENT is set to the old
experiment's directory (glm-visual-v2-work).
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

TASK = Path(__file__).resolve().parents[2] / "examples" / "tasks" / "visual_showcase"
sys.path.insert(0, str(TASK))
import harness  # noqa: E402
import judge  # noqa: E402
import pipeline  # noqa: E402
import prepare  # noqa: E402

OLD = Path(os.environ.get("VISUAL_OLD_EXPERIMENT") or "")
needs_old = pytest.mark.skipif(not os.environ.get("VISUAL_OLD_EXPERIMENT") or not (OLD / "freeze.json").is_file(),
                               reason="set VISUAL_OLD_EXPERIMENT to the old experiment's directory")

PROMPT_SHA256 = {   # sha256 of freeze.json system_prompts.<name>
    "COMMON": "75cc4ade2fee71a82de5bd56b066e921eec7263e3aa31e54e0b93d8193b43669",
    "DIRECT": "60ffaefa65c4c1a95b31b1e07680a79ea57f6c25cbd635b210dee4c26c535c4f",
    "LOOP": "da0a85db232a13c411d87a8d22b3625de27b160ccd8d40462c8a33a9118a60bf",
    "BRIDGE": "69af5bf887a6caa3d3238744550ad9859b2253b5f229dd7849a3bd0e32590b07",
}
TOOLS_SHA256 = "bdfec82584f85df9e07b08b40fc95d22aa47e1cc243644a9c5ee636c099f7296"   # canonical JSON of freeze.json tools
# JSON of [PLACEHOLDER, IMAGES_REMOVED, IMAGES_INTRO], the old campaign.py texts (test_against_the_old_freeze_json)
TEXTS_SHA256 = "35b86ed3d040d052c0a9cebe6fd2f1ddced4eda9d7ef247ad236387e202d2060"
# The judge's rubric prompts, mechanical items, screenshot labels and thresholds (see test_judge_is_the_frozen_one)
JUDGE_SHA256 = "1a06cacbba352262f0bef9344fc76151f066db6f9c0474ef60a9cf05e7de054a"
# browser_capture.cjs: the old capturer with CAPTURER_CHANGES; changing it means changing this test too
CAPTURER_SHA256 = "57ef214b42582081f47bbc66fa8f2d788c9a64e9ac1dd0bb870a31f4a1c65bb5"
# The capturer's only changes from the old file (a regex on the old text, the new text): where Playwright, pngjs and
# Chrome come from. The capture protocol (timings, viewports, interactions, measurements) is the old text.
CAPTURER_CHANGES = [
    (r"const ROOT = __dirname;\nconst MODULES = '[^']+';\nconst DEFAULT_CHROME = '[^']+';\n",
     "// Playwright and pngjs resolve from runtime/node_modules (npm ci) or NODE_PATH. The browser is the installed"
     " Google\n// Chrome (Playwright channel 'chrome') unless --chrome or VISUAL_CHROME names an executable.\n"
     "const ROOT = __dirname;\n"),
    (re.escape("chrome:DEFAULT_CHROME,playwright:path.join(MODULES,'playwright'),pngjs:path.join(MODULES,'pngjs')"),
     "chrome:process.env.VISUAL_CHROME||null"),
    (re.escape("'vendor','chrome','playwright','pngjs'].includes(name)"), "'vendor','chrome'].includes(name)"),
    (re.escape("for(const k of ['workspace','output','vendor','chrome','playwright','pngjs'])"
               "out[k]=path.resolve(out[k]);"),
     "for(const k of ['workspace','output','vendor'])out[k]=path.resolve(out[k]);\n"
     "  if(out.chrome)out.chrome=path.resolve(out.chrome);"),
    (re.escape("require(opt.playwright),{PNG}=require(opt.pngjs)"), "require('playwright'),{PNG}=require('pngjs')"),
    (re.escape("launchServer({executablePath:opt.chrome,"),
     "launchServer({...(opt.chrome?{executablePath:opt.chrome}:{channel:'chrome'}),"),
    (re.escape("[--vendor FILE] [--record]"), "[--vendor FILE] [--chrome FILE] [--record]"),
]


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_system_prompts_and_tool_schemas_are_the_frozen_ones():
    for name, expected in PROMPT_SHA256.items():
        assert digest(getattr(pipeline, name)) == expected, name
    assert digest(canonical(harness.TOOLS)) == TOOLS_SHA256
    assert [t["function"]["name"] for t in harness.TOOLS] == ["write_file", "read_file", "replace_text", "inspect_page"]
    assert "path" not in harness.TOOLS[0]["function"]["parameters"]["properties"]   # the old write_file friction


def test_budgets_are_the_old_config_values():
    """The old config.json's actor limits and price, and the old campaign.py texts, without the old files (the test
    below compares them with the files themselves)."""
    assert (pipeline.MAX_TOKENS, pipeline.COMPLETION_BUDGET) == (32768, 65536)
    assert pipeline.MAX_REQUESTS == {"direct": 1, "harness": 12, "adapted": 12}
    assert (harness.MAX_TOOL_CALLS, harness.MAX_INSPECTIONS) == (24, 3)
    assert (pipeline.ACTOR_SECONDS, pipeline.REQUEST_TIMEOUT) == (600, 420)
    assert pipeline.PRICE == {"input": 0.15, "cached_input": 0.03, "output": 0.50}
    assert digest(json.dumps([pipeline.PLACEHOLDER, pipeline.IMAGES_REMOVED, pipeline.IMAGES_INTRO])) == TEXTS_SHA256


def test_judge_is_the_frozen_one():
    frozen = [judge.RUBRIC_SYSTEM, judge.RUBRIC_FORMAT, judge.RUBRIC_RETRY, judge.MECHANICAL, judge.SCREENSHOTS,
              judge.MOTION_MIN, judge.PAUSE_MAX, judge.INTENSITY_TOLERANCE]
    assert digest(json.dumps(frozen, sort_keys=True, default=str)) == JUDGE_SHA256


@needs_old
def test_against_the_old_freeze_json():
    frozen = json.loads((OLD / "freeze.json").read_text())
    for name, text in frozen["system_prompts"].items():
        assert getattr(pipeline, name.upper()) == text
    assert json.dumps(harness.TOOLS) == json.dumps(frozen["tools"])
    assert frozen["model"] == pipeline.MODEL
    config = json.loads((OLD / "config.json").read_text())
    for key in ("temperature", "top_k", "presence_penalty", "frequency_penalty", "reasoning_effort"):
        assert pipeline.SAMPLING[key] == config[key]
    assert pipeline.MAX_TOKENS == config["max_tokens_per_response"]
    assert pipeline.COMPLETION_BUDGET == config["max_completion_tokens_per_actor"]
    assert pipeline.MAX_REQUESTS == config["max_model_calls"]
    assert (harness.MAX_TOOL_CALLS, harness.MAX_INSPECTIONS) == (config["max_tool_calls"], config["max_inspections"])
    assert (pipeline.ACTOR_SECONDS, pipeline.REQUEST_TIMEOUT) == (config["actor_timeout_seconds"],
                                                                  config["request_timeout_seconds"])
    assert pipeline.PRICE == config["price_usd_per_million"]
    source = (OLD / "campaign.py").read_text()
    for text in (pipeline.PLACEHOLDER, pipeline.IMAGES_REMOVED, pipeline.IMAGES_INTRO):
        assert repr(text) in source or f"'{text}'" in source


def test_transport_settings():
    client = pipeline.make_client()
    assert (client.base_url, client.model, client.api_key_env) == (
        "https://api.fireworks.ai/inference/v1", "accounts/fireworks/models/glm-5p3-flash", "FIREWORKS_API_KEY")
    assert (client.timeout, client.max_retries, client.price) == (420, 0, {"input": 0.15, "cached_input": 0.03,
                                                                           "output": 0.50})
    assert pipeline.SAMPLING == {"temperature": 0, "top_k": 40, "presence_penalty": 0, "frequency_penalty": 0,
                                 "reasoning_effort": 4096}
    # The rubric model uses the same transport settings through its own client.
    other = judge.make_client()
    assert other is not client
    assert (other.base_url, other.model, other.api_key_env, other.timeout, other.max_retries, other.price) == (
        client.base_url, client.model, client.api_key_env, client.timeout, client.max_retries, client.price)
    assert (judge.SAMPLING, judge.MAX_TOKENS) == (pipeline.SAMPLING, pipeline.MAX_TOKENS)


def test_frozen_files_and_committed_task_info_are_current():
    harness.verify_runtime()
    assert harness.RUNTIME_SHA256["browser_capture.cjs"] == CAPTURER_SHA256
    assert hashlib.sha256((TASK / "cases.json").read_bytes()).hexdigest().startswith("6b022e3b")
    files = prepare.generated(prepare.check_inputs())
    assert sorted(p for p in files if p.startswith("task-info/")) == sorted(
        p.relative_to(TASK).as_posix() for p in (TASK / "task-info").rglob("*") if p.is_file())
    for relative, text in files.items():
        if relative.startswith("task-info/"):
            assert (TASK / relative).read_text() == text, relative


def test_prepare_writes_the_task_files_next_to_itself(tmp_path):
    """prepare.py writes tasks.jsonl and splits/ (not committed) into the task folder; --check then passes."""
    skip = shutil.ignore_patterns("node_modules", ".logs", "__pycache__", "tasks.jsonl", "splits")
    copy = shutil.copytree(TASK, tmp_path / "visual_showcase", ignore=skip)
    check = [sys.executable, str(copy / "prepare.py"), "--check"]
    assert subprocess.run(check, capture_output=True, text=True).returncode == 1        # tasks.jsonl, splits missing
    done = subprocess.run([sys.executable, str(copy / "prepare.py")], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert sorted(p.name for p in (copy / "splits").iterdir()) == [
        "lava-eval.txt", "lava.txt", "silk-eval.txt", "silk.txt", "waves-eval.txt", "waves.txt"]
    assert len((copy / "tasks.jsonl").read_text().splitlines()) == 6
    done = subprocess.run(check, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    ignored = (TASK / ".gitignore").read_text().split()
    assert {"tasks.jsonl", "splits/", ".logs/", "runtime/node_modules/"} <= set(ignored)


@needs_old
def test_runtime_files_are_the_old_ones():
    """The Three.js files and cases.json are byte copies; the capturer is the old one with CAPTURER_CHANGES only."""
    old = (OLD.parent / "visual-showcase-work" / "browser_capture.cjs").read_text()
    for pattern, new in CAPTURER_CHANGES:
        old, count = re.subn(pattern, lambda _, new=new: new, old)
        assert count == 1, pattern
    assert (TASK / "runtime" / "browser_capture.cjs").read_text() == old
    for name in ("three.module.min.js", "three.core.min.js", "LICENSE"):
        assert (TASK / "runtime" / "vendor" / name).read_bytes() == (OLD / "vendor" / name).read_bytes()
    assert (TASK / "cases.json").read_bytes() == (OLD / "cases.json").read_bytes()


def test_capturer_changes_are_in_place():
    text = (TASK / "runtime" / "browser_capture.cjs").read_text()
    for _, new in CAPTURER_CHANGES:
        assert text.count(new) == 1, new


def test_tasks_splits_and_task_info():
    files = prepare.generated(prepare.check_inputs())
    rows = [json.loads(line) for line in files["tasks.jsonl"].splitlines()]
    cases = json.loads((TASK / "cases.json").read_text())
    by_id = {r["id"]: r for r in rows}
    assert sorted(by_id) == ["lava", "lava-eval", "silk", "silk-eval", "waves", "waves-eval"]
    for case in cases["cases"]:
        row, alias = by_id[case["id"]], by_id[case["id"] + "-eval"]
        assert set(row) == {"id", "case", "title", "title_zh", "prompt", "rubric", "common_contract"}
        assert {k: v for k, v in alias.items() if k != "id"} == {k: v for k, v in row.items() if k != "id"}
        assert (row["case"], row["title"], row["prompt"], row["rubric"], row["common_contract"]) == (
            case["id"], case["title_en"], case["prompt"], case["rubric"], cases["common_contract"])
        assert files[f"splits/{case['id']}.txt"].split() == [case["id"]]
        assert files[f"splits/{case['id']}-eval.txt"].split() == [case["id"] + "-eval"]
        info = TASK / "task-info" / case["id"]
        assert sorted(p.name for p in info.iterdir()) == ["README.md", "judge.md"]
        text = (info / "README.md").read_text() + (info / "judge.md").read_text()
        assert case["prompt"] in text
        for other in cases["cases"]:
            if other["id"] != case["id"]:
                assert other["title_en"] not in text and other["prompt"].split("\n\n")[1] not in text
        for word in ("score", "rubric", "vlm", "grade", "rating"):   # nothing hints at a measurement
            assert word not in text.lower(), word
