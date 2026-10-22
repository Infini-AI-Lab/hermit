"""The harness2_upstream Evolver (upstream Harness² under the paper's protocol) end to end on the Trainer's toy task,
with a scripted session channel (no model, no network)."""
import json
import os
from pathlib import Path
import shutil
import tempfile

import pytest

from harnessadapter.config import Config
from harnessadapter.trainer.trainer import Trainer
from test_training import make_task

pytest.importorskip("harness2.core.schedule")

TEMPLATE = Path(__file__).resolve().parents[1] / "harnessadapter" / "algorithms" / "templates" / "harness2"

# The scripted channel: the INV and DOM editors add a triggered skill, the composer writes the rule the toy pipeline
# needs into systemprompt.md, the reflector writes a lesson. Every session fails the test if its evidence carries the
# judge's feedback, and records what it saw.
PROPOSER = r'''
import json, shutil, tempfile
from pathlib import Path
from harness2.core.proposer import ProposerError, Reply
from harnessadapter.algorithms.evolvers.harness2_upstream.channel import FrameworkProposer

LEAKS = ("expected the sum", '"score"', '"judge"', '"feedback"')


class Scripted(FrameworkProposer):
    def call(self, mission, context_dir, label, **kwargs):
        if self.params.get("endpoint_down"):
            raise ProposerError(f"[{label}] connection refused", attempts=2)
        root = Path(context_dir)
        for f in root.rglob("*"):
            if f.is_file():
                body = f.read_text(errors="replace")
                assert not any(x in body for x in LEAKS), (label, f, body[:200])
        seen = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
        with open(self.params["calls_log"], "a") as out:
            out.write(json.dumps({"label": label, "files": seen, "mission": mission[:300]}) + "\n")
        if label.startswith("experience."):
            text = ("<edits>\ncomponent: systemprompt.md\ncandidates: agg1\nchange: the rule\n"
                    "observed: the deliverable became the sum\nverdict: helped\n</edits>\n<experience>\n"
                    "cue: adding numbers\nlesson: state the operation in systemprompt.md\n"
                    "evidence: the agg1 rollout answered with the sum\nconfidence: high\n</experience>")
            reply = Reply(label=label, text=text, ok=True, attempts=1)
        else:
            work = Path(tempfile.mkdtemp()) / "harness"
            shutil.copytree(root / "harness", work)
            if label.startswith("agg"):
                (work / "systemprompt.md").write_text("Always add the numbers.\n")
            else:
                (work / "skills" / "sum_check").mkdir(parents=True, exist_ok=True)
                (work / "skills" / "sum_check" / "SKILL.md").write_text(
                    "---\ndescription: checking a sum before answering\n---\nAdd again and compare.\n")
                (work / "guardrails.md").write_text("When a sum must be checked, follow the `sum_check` skill.\n")
            reply = Reply(label=label, text=f"<notes>{label}: edited</notes>", ok=True, attempts=1, workspace=work)
        self._calls.append(reply)
        return reply
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


# The task's source material, written slowly enough that two stages gathering it at once would collide.
SOURCES = r'''
import time
from pathlib import Path


def task_files(task, dest):
    dest.mkdir(parents=True, exist_ok=True)
    note = dest / "numbers.txt"
    if not note.exists():
        time.sleep(0.3)
        note.symlink_to(Path(__file__).resolve())
    return dest
'''


def run(tmp_path: Path, **params) -> Path:
    (tmp_path / "proposer.py").write_text(PROPOSER)
    (tmp_path / "sources.py").write_text(SOURCES)
    (tmp_path / "slots.yaml").write_text("work: Adding two numbers given in the task.\ndeliverable_word: sum\n")
    path = make_task(tmp_path, template=str(TEMPLATE),
                     evolver={"name": "harness2_upstream",
                              "params": {"proposer": "proposer.py:Scripted", "backend": None, "model": "scripted",
                                         "slots": "slots.yaml", "name": "toy", "sources": "sources.py:task_files",
                                         "calls_log": str(tmp_path / "calls.jsonl"), **params}},
                     trainer={"run_name": "h2u", "output_dir": "runs", "epochs": 1, "budget": {"usd": 5}})
    (tmp_path / "stream.txt").write_text("t0\nt1\nt2\n")
    Trainer(Config.load(path, ["data.train=stream.txt", "data.step_size=1", "data.shuffle=false"])).train()
    return tmp_path / "runs" / "h2u"


def test_every_task_starts_from_h0_and_only_the_library_carries(tmp_path):
    run_dir = run(tmp_path)
    steps = [json.loads(l) for l in (run_dir / "steps.jsonl").read_text().splitlines()]
    assert [s["tasks"] for s in steps] == [["t0"], ["t1"], ["t2"]]
    assert all(s["rollout"]["mean_score"] == 0.0 for s in steps)          # every Trainer rollout ran H0
    assert not any(s["changed"] for s in steps)
    assert (run_dir / "workspace" / "adapter" / "ADAPTER.md").read_text() == ""
    results = [json.loads((run_dir / f"step_{n:04d}" / "step_result.json").read_text())["extra"]["harness2"]
               for n in (1, 2, 3)]
    for i, r in enumerate(results):
        assert r["status"] == "ok" and r["selection"]["chosen_label"] == "agg1" and r["selection"]["trivial"]
        assert r["runs"]["base1"] == {"answered": True, "score": 0.0}     # the reference is the Trainer's rollout
        assert r["runs"]["agg1"]["score"] == 1.0                           # the composed harness, on the same task
        assert r["judged"]["graded"] and r["judged"]["frac"] == 1.0 and r["judged"]["label"] == "agg1"
        assert r["library"] == {"entries_before": i, "added": [f"t{i}"]}
        assert [s["label"] for s in r["stages"]] == ["inv1", "dom1", "agg1"]
        assert not any(s["fell_back"] for s in r["stages"])
    calls = [json.loads(l) for l in (tmp_path / "calls.jsonl").read_text().splitlines()]
    labels = [c["label"] for c in calls]
    assert labels.count("experience.agg1") == 3 and sorted(labels[:2]) == ["dom1.a1", "inv1.a1"]
    first_inv, later_inv = calls[labels.index("inv1.a1")], [c for c in calls if c["label"] == "inv1.a1"][1]
    assert not any(f.startswith("library/") for f in first_inv["files"])     # the first task sees an empty library
    assert "library/INDEX.md" in later_inv["files"] and "rollouts/base1.md" in later_inv["files"]
    assert "source/numbers.txt" in first_inv["files"]                         # the task's source material
    assert "Adding two numbers" in first_inv["mission"] or "THE WORK" in first_inv["mission"]
    summary = json.loads(next((run_dir / "step_0001" / "output" / "h2").glob("toy-mha-agg1/t0/summary.json"))
                         .read_text())
    assert summary["modes"]["agg"]["ledger"]["R"] == 4 and summary["modes"]["agg"]["ledger"]["P"] == 3
    lib = run_dir / "workspace" / "state" / "library" / "agg" / "default"
    assert sorted(p.name for p in (lib / "harnesses").iterdir()) == ["t0", "t1", "t2"]
    assert "Always add the numbers." in (lib / "harnesses" / "t2" / "harness" / "systemprompt.md").read_text()


def test_one_task_per_step_is_required(tmp_path):
    (tmp_path / "proposer.py").write_text(PROPOSER)
    (tmp_path / "slots.yaml").write_text("work: Adding two numbers given in the task.\n")
    path = make_task(tmp_path, template=str(TEMPLATE),
                     evolver={"name": "harness2_upstream",
                              "params": {"proposer": "proposer.py:Scripted", "backend": None, "model": "scripted",
                                         "slots": "slots.yaml", "calls_log": str(tmp_path / "calls.jsonl")}},
                     trainer={"run_name": "h2u", "output_dir": "runs", "epochs": 1, "budget": {"usd": 5}})
    Trainer(Config.load(path, [])).train()                                # step_size 4: refused, nothing changes
    steps = [json.loads(l) for l in (tmp_path / "runs" / "h2u" / "steps.jsonl").read_text().splitlines()]
    assert all(s["evolver"]["status"] == "failed" for s in steps)
    assert not (tmp_path / "calls.jsonl").exists()


def test_a_step_without_editor_sessions_is_marked_failed(tmp_path):
    run_dir = run(tmp_path, endpoint_down=True)
    steps = [json.loads(l) for l in (run_dir / "steps.jsonl").read_text().splitlines()]
    assert all(s["evolver"]["status"] == "failed" and "NO EDITOR SESSION RAN" in s["evolver"]["summary"] for s in steps)
    extra = json.loads((run_dir / "step_0001" / "output" / "harness2.json").read_text())
    assert extra["editors_failed"] and all(s["fell_back"] for s in extra["stages"])
    scores = [json.loads(l) for l in (run_dir / "step_0001" / "output" / "scores.jsonl").read_text().splitlines()]
    assert sorted(r["run_id"].rsplit("/", 1)[-1] for r in scores) == ["agg1", "base1", "dom1", "inv1"]


def test_a_run_continues_a_finished_stream_from_its_library(tmp_path):
    first = run(tmp_path)                                                 # t0, t1, t2
    (tmp_path / "rest.txt").write_text("t3\nt4\n")
    Trainer(Config.load(tmp_path / "run.yaml", [
        "data.train=rest.txt", "data.step_size=1", "data.shuffle=false", "trainer.run_name=h2u-rest",
        f"evolver.params.calls_log={tmp_path / 'rest-calls.jsonl'}", "evolver.params.library_from=runs/h2u"])).train()
    rest = tmp_path / "runs" / "h2u-rest"
    results = [json.loads((rest / f"step_{n:04d}" / "step_result.json").read_text())["extra"]["harness2"]
               for n in (1, 2)]
    assert [r["library"] for r in results] == [{"entries_before": 3, "added": ["t3"]},
                                               {"entries_before": 4, "added": ["t4"]}]
    assert all(r["status"] == "ok" and r["runs"]["agg1"]["score"] == 1.0 for r in results)
    calls = [json.loads(l) for l in (tmp_path / "rest-calls.jsonl").read_text().splitlines()]
    first_inv = next(c for c in calls if c["label"] == "inv1.a1")         # the continuation's first task
    assert "library/INDEX.md" in first_inv["files"] and "library/harnesses/t2/EXPERIENCE.md" in first_inv["files"]
    state = rest / "workspace" / "state" / "library"
    imported = json.loads((state / "imported.json").read_text())
    assert imported["entries"] == ["default/t0", "default/t1", "default/t2"] and imported["taken"] == ["t0", "t1", "t2"]
    assert imported["stop_reason"] == "completed all epochs"
    assert sorted(p.name for p in (state / "agg" / "default" / "harnesses").iterdir()) == ["t0", "t1", "t2", "t3", "t4"]
    done = first / "workspace" / "state" / "library" / "agg" / "default" / "harnesses"
    assert sorted(p.name for p in done.iterdir()) == ["t0", "t1", "t2"]   # the stream continued is left as it was


def test_a_continuation_refuses_what_would_not_continue_the_stream(tmp_path):
    first = run(tmp_path)                                                 # t0, t1, t2
    (tmp_path / "rest.txt").write_text("t3\n")

    def cont(name, source, stream="rest.txt", *more):
        Trainer(Config.load(tmp_path / "run.yaml", [
            f"data.train={stream}", "data.step_size=1", "data.shuffle=false", f"trainer.run_name={name}",
            f"evolver.params.calls_log={tmp_path / 'more-calls.jsonl'}", f"evolver.params.library_from={source}",
            *more])).train()
        run_dir = tmp_path / "runs" / name
        return [json.loads(l) for l in (run_dir / "steps.jsonl").read_text().splitlines()]

    (first / "summary.json").rename(first / "summary.json.held")
    with pytest.raises(SystemExit, match="has not finished"):
        cont("c-unfinished", "runs/h2u")
    (first / "summary.json.held").rename(first / "summary.json")
    with pytest.raises(SystemExit, match="effort differ"):                # the editor settings are the stream's
        cont("c-effort", "runs/h2u", "rest.txt", "evolver.params.effort=high")
    tainted = tmp_path / "tainted"
    shutil.copytree(first / "workspace" / "state" / "library" / "agg", tainted)
    (tainted / "default" / "harnesses" / "t1" / "EXPERIENCE.md").write_text('{"pass_rate": 1.0}\n')
    with pytest.raises(SystemExit, match="grader-verdict marker"):
        cont("c-tainted", tainted)
    # A start that recorded no step left its import behind: the next start imports again.
    left = tmp_path / "runs" / "c-restart" / "workspace" / "state" / "library"
    (left / "agg" / "default").mkdir(parents=True)
    (left / "agg" / "default" / "INDEX.md").write_text("# a start that recorded no step\n")
    (left / "imported.json").write_text("{}\n")
    steps = cont("c-restart", "runs/h2u")
    assert steps[0]["evolver"]["status"] == "done" and "library 3 -> 4" in steps[0]["evolver"]["summary"]
    # A task the stream took is refused, with or without a library entry of its own.
    shutil.rmtree(first / "workspace" / "state" / "library" / "agg" / "default" / "harnesses" / "t1")
    (tmp_path / "again.txt").write_text("t1\nt3\n")
    steps = cont("c-again", "runs/h2u", "again.txt")
    assert steps[0]["evolver"]["status"] == "failed" and "continues" in steps[0]["evolver"]["summary"]
    assert steps[1]["evolver"]["status"] == "done"
    with pytest.raises(SystemExit, match="is this run"):                  # resume_mode=disable moved the stream away
        cont("h2u", "runs/h2u", "rest.txt", "trainer.resume_mode=disable")
