"""The harness2 Evolver end to end on the Trainer's toy task, with a scripted proposer (no model, no network)."""
import json
import os
from pathlib import Path
import shutil
import tempfile

import pytest
import yaml

from harnessadapter.config import Config
from harnessadapter.trainer.trainer import Trainer
from test_training import make_task

TEMPLATE = Path(__file__).resolve().parents[1] / "harnessadapter" / "algorithms" / "templates" / "harness2"

# The scripted proposer: every editing stage writes the rule the toy pipeline needs into systemprompt.md, the INV
# stage also adds a (triggered) skill; the selector picks the first candidate; the reflector writes a lesson. It
# records each call and fails the test if any evidence file carries the judge's feedback.
PROPOSER = r'''
import json, shutil
from pathlib import Path
from harnessadapter.algorithms.evolvers.harness2.core.proposer import Reply

LEAKS = ("expected the sum", '"score"', '"judge"', "correct")
MODE = {"value": "good"}


class Scripted:
    def __init__(self, ctx=None, log_dir=None):
        self.ctx = ctx
        self.calls_file = Path(ctx.workspace.state) / "calls.jsonl"

    def call(self, mission, root, label, *, harvest=None, expect=None):
        root = Path(root)
        for f in (root / "context").rglob("*"):
            if f.is_file():
                body = f.read_text(errors="replace")
                assert not any(x in body for x in LEAKS), (label, f, body[:200])
        with open(self.calls_file, "a") as out:
            out.write(json.dumps({"label": label, "mission": mission[:200], "rejected": "REJECTED" in mission}) + "\n")
        mode = (self.ctx.params.get("script") or "good")
        if label.startswith("select."):
            ids = sorted(p.stem for p in (root / "context" / "candidates").glob("*.md"))
            return Reply(label, f"<why>first</why>\n```choice\n{ids[0]}\n```")
        if label.startswith("experience"):
            return Reply(label, "<edits>\ncomponent: harness/systemprompt.md\ncandidates: agg1\nchange: rule\n"
                         "observed: answers appeared\nverdict: helped\n</edits>\n<experience>\ncue: sums\n"
                         "lesson: state the operation in harness/systemprompt.md\nevidence: the row above\n"
                         "confidence: high\n</experience>")
        work = root / "proposal"
        if work.exists():
            shutil.rmtree(work)
        shutil.copytree(root / harvest, work)
        if mode == "invalid":
            (work / "skills" / "bad").mkdir(parents=True, exist_ok=True)
            (work / "skills" / "bad" / "SKILL.md").write_text("no frontmatter here\n")
            return Reply(label, "<notes>invalid on purpose</notes>", work)
        if mode == "keep":
            return Reply(label, "<notes>KEEP_BASE</notes>", work)
        (work / "harness" / "systemprompt.md").write_text("Always add the numbers.\n")
        if label.startswith("inv"):
            (work / "skills" / "sum_check").mkdir(parents=True, exist_ok=True)
            (work / "skills" / "sum_check" / "SKILL.md").write_text(
                "---\ndescription: checking a sum before answering\n---\nAdd again and compare.\n")
        return Reply(label, f"<notes>kept -> rule -> {label}</notes>", work)
'''


@pytest.fixture
def tmp_path():
    """A directory outside /tmp: the template check runs under bubblewrap with an empty tmpfs at /tmp."""
    base = Path.home() / ".cache" / "harnessadapter-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


def run(tmp_path: Path, **params) -> Path:
    (tmp_path / "proposer.py").write_text(PROPOSER)
    path = make_task(tmp_path, template=str(TEMPLATE),
                     evolver={"name": "harness2", "params": {"proposer": "proposer.py:Scripted", **params}},
                     trainer={"run_name": "h2", "output_dir": "runs", "epochs": 1, "budget": {"usd": 5}})
    Trainer(Config.load(path, [])).train()
    return tmp_path / "runs" / "h2"


def records(run_dir: Path) -> list[dict]:
    return [json.loads(l) for l in (run_dir / "workspace" / "state" / "harness2.jsonl").read_text().splitlines()]


def test_parallel_k1_learns_and_stores_a_lesson(tmp_path):
    run_dir = run(tmp_path)
    recs = records(run_dir)
    assert len(recs) == 2                                         # 6 training tasks, step_size 4
    first = recs[0]
    assert first["result"]["ledger"]["R"] == 4 and first["result"]["ledger"]["P"] == 3
    assert first["selection"]["chosen"] == "agg1" and first["selection"]["trivial"]
    adapter = run_dir / "workspace" / "adapter"
    text = (adapter / "ADAPTER.md").read_text()
    assert text.startswith("Always add the numbers.") and "### sum_check" not in text   # agg wrote only the rule
    steps = [json.loads(l) for l in (run_dir / "steps.jsonl").read_text().splitlines()]
    assert steps[0]["rollout"]["mean_score"] == 0.0 and steps[1]["rollout"]["mean_score"] == 1.0
    lib = run_dir / "workspace" / "state" / "library" / "default"
    assert (lib / "INDEX.md").exists() and len(list((lib / "harnesses").iterdir())) == 2
    calls = [json.loads(l) for l in (run_dir / "workspace" / "state" / "calls.jsonl").read_text().splitlines()]
    assert sorted(c["label"] for c in calls[:4]) == ["agg1.a1", "dom1.a1", "experience", "inv1.a1"]
    assert sorted(c["label"] for c in calls[:2]) == ["dom1.a1", "inv1.a1"]      # proposals before the composition
    report = (run_dir / "step_0001" / "output" / "report.md").read_text()
    assert "### agg1 (AGG)" in report and "## Lesson" in report
    assert (run_dir / "step_0001" / "output" / "trees" / "inv1" / "skills" / "sum_check" / "SKILL.md").exists()


def test_parallel_k2_blind_selection_and_sequential(tmp_path):
    run_dir = run(tmp_path, k=2)
    rec = records(run_dir)[0]
    assert rec["result"]["ledger"]["R"] == 7 and rec["result"]["ledger"]["P"] == 6
    assert not rec["selection"]["trivial"] and set(rec["selection"]["picks"]) == set(rec["tasks"])
    shutil.rmtree(tmp_path / "runs")
    for f in ("task-info", "toy"):
        shutil.rmtree(tmp_path / f, ignore_errors=True)
    for f in ("tasks.jsonl", "train.txt", "val.txt", "test.txt", "pipeline.py", "judge.py", "run.yaml"):
        (tmp_path / f).unlink(missing_ok=True)
    run_dir = run(tmp_path, k=2, recursion="sequential")
    rec = records(run_dir)[0]
    assert rec["result"]["mode"] == "seq" and rec["result"]["finals"] == ["c1", "c2"]
    assert rec["result"]["ledger"]["R"] == 7 and rec["result"]["ledger"]["P"] == 6


def test_invalid_proposals_retry_once_then_fall_back(tmp_path):
    run_dir = run(tmp_path, script="invalid")
    rec = records(run_dir)[0]
    stages = {s["label"]: s for s in rec["result"]["stages"]}
    assert all(s["fell_back"] for s in stages.values()) and "description" in stages["inv1"]["error"]
    calls = [json.loads(l) for l in (run_dir / "workspace" / "state" / "calls.jsonl").read_text().splitlines()]
    assert any(c["label"] == "inv1.a2" and c["rejected"] for c in calls)
    assert not rec["changed"]["modified"] and not rec["changed"]["added"]


def test_keep_base_leaves_the_adapter_unchanged(tmp_path):
    run_dir = run(tmp_path, script="keep")
    rec = records(run_dir)[0]
    assert all(s["kept_base"] for s in rec["result"]["stages"])
    assert (run_dir / "workspace" / "adapter" / "ADAPTER.md").read_text() == ""
