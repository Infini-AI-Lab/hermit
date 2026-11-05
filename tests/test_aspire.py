"""ASPIRE: the aspire template (the skill library, its reader and its check), the coordinator's hook into the
Trainer (next_tasks), and the example's Codex task functions (prompt assembly, the workspace, bin/sim's rules, the
after-the-fact audit, the judge) and pure helpers; no simulator, no model, no network."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from hermit.agent.evolver.base import StepResult
from hermit.agent.evolver.methods.aspire import evolver as E
from hermit.spec import check_spec

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "hermit" / "adapter" / "templates" / "aspire"
EXAMPLE = ROOT / "examples" / "aspire_robosuite" / "utils"

ENTRY = """## Re-localize after the approach
**Failure signature.** The gripper closes on air: the cube moved when the arm passed over it.
**When to apply.** `close_gripper()` reports a gripper width at the fully-closed limit.
Localize again from the eye-in-hand camera before the final descent.

```python
found = localize_object("red cube", camera_name="robot0_eye_in_hand", target_frame=get_robot_state().base_frame)
if not found.ok:
    raise ValueError("cube not found")
target = found.geometry.pose.position
```
**Provenance.** robosuite/cube_lifting: 1/3 -> 3/3 development seeds; verifier 5/5.
"""


@pytest.fixture
def tmp_path():
    """A directory outside /tmp: the template check runs under bubblewrap with an empty tmpfs at /tmp."""
    base = Path.home() / ".cache" / "hermit-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


def seed(tmp: Path) -> Path:
    adapter = tmp / "adapter"
    shutil.copytree(TEMPLATE, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    return adapter


def spec(path) -> dict:
    r = check_spec(Path(path), TEMPLATE, deploy=False)
    return {"passed": r.passed, "issues": [{"path": i.path, "problem": i.problem} for i in r.issues]}


# ---------- the template ----------

def test_template_root_passes_its_own_check():
    assert check_spec(TEMPLATE, TEMPLATE).passed


def test_reader_renders_the_index_then_the_skill_files(tmp_path):
    adapter = seed(tmp_path)
    (adapter / "skills" / "grasp.md").write_text((adapter / "skills" / "grasp.md").read_text().replace(
        "(no entries yet)\n", ENTRY))
    spec_ = importlib.util.spec_from_file_location("aspire_interface", adapter / "interface.py")
    module = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(module)
    out = module.library()
    assert out["files"] == ["localize.md", "grasp.md", "transport.md", "manipulation.md"]
    assert out["entries"] == 1
    assert out["text"].index("Shared Robot Skills") < out["text"].index("Re-localize after the approach")
    assert "description:" not in out["text"]


def test_check_refuses_a_broken_entry_a_bad_snippet_and_an_unlinked_file(tmp_path):
    adapter = seed(tmp_path)
    grasp = adapter / "skills" / "grasp.md"
    grasp.write_text(grasp.read_text() + "\n## No fields\nJust prose.\n\n" +
                     ENTRY.replace("## Re-localize", "## Bad snippet").replace("target = found", "import numpy\nt = "
                                                                                                   "found"))
    (adapter / "skills" / "orphan.md").write_text("---\ndescription: x\n---\n# Orphan\n")
    problems = " ".join(i["problem"] for i in spec(adapter)["issues"])
    assert "No fields: missing" in problems
    assert "Bad snippet after the approach: the snippet would be refused" in problems
    assert "orphan.md is not linked" in problems


def test_check_refuses_staged_proposals_privileged_access_and_reads_headings_in_code_as_code(tmp_path):
    adapter = seed(tmp_path)
    grasp = adapter / "skills" / "grasp.md"
    commented = ENTRY.replace("if not found.ok:", "## a comment, not a heading\nif not found.ok:")
    grasp.write_text(grasp.read_text().replace("(no entries yet)\n", commented))
    assert spec(adapter)["passed"]
    prose = ENTRY.replace("## Re-localize after the approach", "## Warned").replace(
        "Localize again", "Never read env.sim.data; localize again")
    bash = "```bash\nbin/sim run --seed 101\n```\n"
    grasp.write_text(grasp.read_text() + "\n" + prose.replace("**Provenance.**", bash + "**Provenance.**"))
    assert spec(adapter)["passed"]                 # a warning in prose and a shell fence are not program snippets
    grasp.write_text(grasp.read_text() + "\n" + ENTRY.replace("## Re-localize after the approach", "## Peek")
                     .replace("target = found.geometry.pose.position", "target = env.sim.data.qpos"))
    (adapter / "skills" / "proposed.md").write_text(ENTRY)
    (adapter / "skills" / "transport.md").write_text((adapter / "skills" / "transport.md").read_text() + "x" * 100_000)
    problems = " ".join(i["problem"] for i in spec(adapter)["issues"])
    assert "Peek: the snippet uses privileged access (env.sim)" in problems
    assert "past 100000" in problems
    assert "skills/proposed.md stages proposals for a verifier only" in problems


def test_reader_leaves_out_the_staged_proposals(tmp_path):
    adapter = seed(tmp_path)
    (adapter / "skills" / "proposed.md").write_text(ENTRY)
    spec_ = importlib.util.spec_from_file_location("aspire_interface_2", adapter / "interface.py")
    module = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(module)
    assert "proposed.md" not in module.library()["files"]
    assert "Re-localize" not in module.library()["text"]


# ---------- the coordinator ----------

def test_the_coordinator_chooses_tasks_and_returns_its_named_next_task(tmp_path, monkeypatch):
    from hermit.agent.evolver.agent import AgentEvolver
    out = tmp_path / "output"
    out.mkdir()
    ctx = SimpleNamespace(workspace=SimpleNamespace(output=out))
    monkeypatch.setattr(AgentEvolver, "step", lambda self, ctx: StepResult("done", "session finished"))
    evolver = E.AspireEvolver()
    assert evolver.chooses_tasks
    assert evolver.step(ctx).next_tasks is None                  # no file: the Trainer's order
    (out / "next_tasks.json").write_text('["robosuite-nut_assembly"]')
    assert evolver.step(ctx).next_tasks == ["robosuite-nut_assembly"]
    (out / "next_tasks.json").write_text('{"not": "a list"}')
    assert evolver.step(ctx).next_tasks is None


def test_the_coordinator_prompt_carries_the_guides_sections():
    text = (ROOT / "hermit" / "agent" / "evolver" / "methods" / "aspire" / "COORDINATOR.md").read_text()
    for heading in ("### 6. Update skills", "### 7. Dispatch a verifier for proposed skills",
                    "## Curriculum: choosing what runs next"):
        assert heading in text
    for placeholder in ("{learning_material}", "{adapter}", "{scratch}", "{state}", "{output}", "{message}"):
        assert placeholder in text


# ---------- the example's actor helpers ----------

def _actor_module():
    sys.path.insert(0, str(EXAMPLE / "agent"))
    try:
        spec_ = importlib.util.spec_from_file_location("aspire_actor", EXAMPLE / "agent" / "agent.py")
        module = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(EXAMPLE / "agent"))


def test_actor_reads_seeds_and_the_outermost_program_fence():
    A = _actor_module()
    assert A.seeds_of("101-103 7") == [101, 102, 103, 7]
    assert A.seeds_of([1, "3-4"]) == [1, 3, 4]
    reply = "diagnosis\n```python\ns = '''\n```python\nx\n```\n'''\nresult = {}\n```\ntrailing"
    assert A.program_of(reply).endswith("result = {}\n")
    assert A.program_of("no program") is None
    assert A.json_of('text ```json\n{"a": 1}\n```') == {"a": 1}


def test_sim_compact_makes_traces_readable():
    sys.path.insert(0, str(EXAMPLE / "agent"))
    try:
        import sim
    finally:
        sys.path.remove(str(EXAMPLE / "agent"))
    value = {"$type": "Pose", "position": {"$array_inline": [0.123456, 1.0, 2.0], "dtype": "float64", "shape": [3]},
             "obs": {"$type": "Observation", "cameras": {}}, "big": {"$array_inline": list(range(40)), "shape": [40]}}
    out = sim.compact(value)
    assert out == {"type": "Pose", "position": [0.1235, 1.0, 2.0], "obs": "<Observation>",
                   "big": "<array None shape [40]>"}


def test_the_failure_keyframe_is_the_failing_calls_own(tmp_path):
    sys.path.insert(0, str(EXAMPLE / "agent"))
    try:
        import sim
    finally:
        sys.path.remove(str(EXAMPLE / "agent"))
    run = tmp_path / "run"
    (run / "source").mkdir(parents=True)
    trace = [{"tool": "open_gripper", "ok": True}] + [{"tool": "move_to_pose", "ok": i != 2} for i in range(4)]
    (run / "source" / "program-result.json").write_text(json.dumps({"calls": trace}))
    names = ["reset", "before-open-gripper", "after-open-gripper"]
    for i in range(3):   # the third move raised: no after-frame of its own
        names += ["before-move-to-pose"] + (["after-move-to-pose"] if i < 2 else [])
    for n, name in enumerate(names, 1):
        d = run / "media" / "keyframes" / f"{n:06d}-{name}"
        d.mkdir(parents=True)
        (d / "agentview.png").write_bytes(b"png")
    picked = [label for label, _ in sim.keyframes(run, limit=1)]
    assert picked == [f"{names.index('before-move-to-pose', 7) + 1:06d}-before-move-to-pose"]


# ---------- the Codex actor's task functions ----------

def _task_module():
    sys.path.insert(0, str(EXAMPLE))
    try:
        spec_ = importlib.util.spec_from_file_location("aspire_task", EXAMPLE / "task.py")
        module = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(EXAMPLE))


def fake_aspire(tmp: Path) -> Path:
    """The files of an ASPIRE checkout the task functions read: the two templates, the docs, the snapshot program."""
    root = tmp / "ASPIRE"
    skills = root / ".claude" / "skills" / "iterative-debugging"
    (skills / "scripts").mkdir(parents=True)
    (root / "docs").mkdir()
    marks = ("<!-- ==================== TEMPLATE START ==================== -->",
             "<!-- ==================== TEMPLATE END ==================== -->")
    (skills / "subagent-prompt.md").write_text(f"intro\n{marks[0]}\n## Task Assignment\n\nBENCHMARK: <x>\n\n---\n\n"
                                               f"## What You Are\nfix-loop text\n{marks[1]}\n")
    (skills / "verifier-prompt.md").write_text(f"intro\n{marks[0]}\nYou are verifying. Seeds 51-55.\n{marks[1]}\n")
    (skills / "scripts" / "scene_snapshot.py").write_text("result = {}\n")
    evosearch = root / ".claude" / "skills" / "evosearch"
    (evosearch / "skills").mkdir(parents=True)
    (evosearch / "subagent-prompt.md").write_text("intro\n\n```\n## Task Assignment\n\nBENCHMARK: <x>\n\n---\n\n"
                                                  "## EVAL SET LOCKOUT\nsearch text\n```\n")
    (evosearch / "skills" / "evosearch-iteration.md").write_text("# iteration\n")
    for name in ("api-reference.md", "run-artifacts.md", "robosuite.md"):
        (root / "docs" / name).write_text(f"# {name}\nrun examples/robosuite/cube_lifting_seed1.py\nkept\n")
    (skills / "skills").mkdir()
    (skills / "skills" / "task-exploration.md").write_text("# exploration\n")
    return root


def test_the_actor_prompt_is_the_header_the_assignment_and_aspires_template(tmp_path):
    T = _task_module()
    params = {"aspire_root": str(fake_aspire(tmp_path)), "debug_seeds": "101-103", "unseen_seeds": "126",
              "eval_seeds": "1-3"}
    text = T.prompt({"id": "robosuite-cube_lifting", "suite": "cube_lifting"}, params, tmp_path)
    assert "fix-loop subagent" in text and "`$TASK_DIR` is `robosuite/cube_lifting/task_0`" in text
    assert "DEV_SEEDS: 101-103" in text and "UNSEEN_SEEDS: 126" in text and "HELDOUT_COUNT: 3" in text
    assert "## What You Are\nfix-loop text" in text and "BENCHMARK: <x>" not in text   # ours replaces theirs
    # Part 2, the evolutionary search, follows the fix loop with its own page and its template after its assignment
    assert text.index("fix-loop text") < text.index("# Part 2: the evolutionary search") < text.index("search text")
    assert "`TASK_DIR` is `evosearch/robosuite/cube_lifting/task_0`" in text
    assert "# Part 2" not in T.prompt({"id": "x", "suite": "cube_lifting"}, {**params, "evosearch": False}, tmp_path)
    verifier = T.prompt({"id": "verify-x", "suite": "cube_lifting", "role": "verifier", "debug_seeds": "101-105"},
                        params, tmp_path)
    assert "skill verifier" in verifier and "Development seeds: **101-105**" in verifier
    assert "You are verifying. Seeds 51-55." in verifier and "# Part 2" not in verifier


def test_a_verifier_never_takes_the_actors_unseen_or_held_out_seeds(tmp_path):
    T = _task_module()
    params = {"aspire_root": str(fake_aspire(tmp_path)), "unseen_seeds": "126", "eval_seeds": "1-3"}
    s = T.settings({"suite": "cube_lifting", "role": "verifier", "debug_seeds": "101-105"}, params)
    assert (s["dev"], s["unseen"], s["heldout"]) == ("101-105", "", "")
    assert T.settings({"suite": "cube_lifting", "role": "rerun"}, params)["role"] == "actor"


def test_prepare_lays_out_the_workspace_and_bin_sim_refuses_unassigned_seeds(tmp_path):
    T = _task_module()
    work = tmp_path / "work"
    (work / "hermit" / "aspire" / "skills").mkdir(parents=True)
    (work / "hermit" / "aspire" / "skills" / "proposed.md").write_text(ENTRY)
    params = {"aspire_root": str(fake_aspire(tmp_path)), "debug_seeds": "101-103", "unseen_seeds": "126"}
    T.prepare({"id": "verify-x", "suite": "cube_lifting", "role": "verifier", "debug_seeds": "101-105"}, work,
              params, tmp_path)
    assert (work / "proposed_skills.md").read_text() == ENTRY
    T.prepare({"id": "robosuite-cube_lifting", "suite": "cube_lifting"}, work, params, tmp_path)
    for name in ("task.md", "docs/api-reference.md", "docs/evosearch-iteration.md", "docs/task-exploration.md",
                 "scripts/scene_snapshot.py", "bin/sim", "bin/sim.json"):
        assert (work / name).is_file(), name
    (work / "p.py").write_text("result = {}\n")
    sim = lambda *args: subprocess.run([str(work / "bin" / "sim"), *args], capture_output=True, text=True, cwd=work)
    assert "examples/" not in (work / "docs" / "robosuite.md").read_text()
    assert "kept" in (work / "docs" / "robosuite.md").read_text()
    held_out = sim("run-batch", "--seeds", "1-3", "--program", "p.py", "--output-root", "out")
    assert held_out.returncode == 2 and "not assigned to you" in held_out.stderr
    outside = sim("run", "--seed", "101", "--program", "p.py", "--output-root", str(tmp_path / "elsewhere"))
    assert outside.returncode == 2 and "inside the workspace" in outside.stderr


def test_a_verifier_may_run_two_programs(tmp_path):
    T = _task_module()
    work = tmp_path / "work"
    work.mkdir()
    T.prepare({"id": "verify-x", "suite": "cube_lifting", "role": "verifier", "debug_seeds": "101-105"}, work,
              {"aspire_root": str(fake_aspire(tmp_path))}, tmp_path)
    codes = []
    for name in ("a.py", "a.py", "c.py"):                       # the same path, rewritten: a new program
        (work / name).write_text(f"result = {{'v': {len(codes)}}}\n")
        done = subprocess.run([str(work / "bin" / "sim"), "run-batch", "--seeds", "101-105", "--program", name,
                               "--output-root", "out", "--init-mode", "saved"], capture_output=True, text=True,
                              cwd=work)
        codes.append((done.returncode, done.stderr))
    assert "attempt budget" not in codes[0][1] and "attempt budget" not in codes[1][1]
    assert codes[2][0] == 2 and "attempt budget is 2 programs" in codes[2][1]


def test_the_audit_voids_a_session_that_reached_past_its_rules_and_only_that(tmp_path):
    T = _task_module()
    root = fake_aspire(tmp_path)
    solved = root / ".claude" / "skills" / "iterative-debugging" / "robosuite"
    solved.mkdir()
    (solved / "cube_lifting_fix_code.py").write_text("result = 'solved'\n")
    holder = tmp_path / "workspaces"
    (holder / "verify-robosuite-cube_lifting-def" / "work").mkdir(parents=True)
    run_dir = holder / "robosuite-cube_lifting-abc"
    work, log = run_dir / "work", run_dir / "log"
    work.mkdir(parents=True)
    log.mkdir()
    s = T.settings({"suite": "cube_lifting"}, {"aspire_root": str(root), "debug_seeds": "101-103",
                                               "unseen_seeds": "126", "eval_seeds": "1-3"})
    command = lambda c: json.dumps({"item": {"type": "command_execution", "command": c}}) + "\n"
    # what ASPIRE's own text makes actors type, inside their workspace: not a breach
    clean = [command("bin/sim run-batch --seeds 101-103 --program code/v00.py --output-root debug/initial"),
             command("ls .claude/skills/iterative-debugging/skills/ 2>/dev/null; ls .venv-robosuite/bin/cap-harness"),
             command("cp fix_code.py code/v03_fix_code.py"),
             command(f"cat {run_dir}/work/robosuite/cube_lifting/task_0/fix_code.py")]
    (log / "agent-codex.jsonl").write_text("".join(clean))

    def a_run(name, seed, source, code="result = {}\n"):
        d = work / "debug" / name
        (d / "source").mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({"seed": seed}))
        sha = hashlib.sha256(code.encode()).hexdigest()
        (d / "source" / "provenance.json").write_text(json.dumps({"input_path": str(source), "sha256": sha}))
    a_run("0101", 101, work / "code" / "v00.py")
    assert T.audit(s, work, log) == []
    broken = [command(f"python3 - <<'PY'\nopen('{root}/README.md').read()\nPY"),
              command("cat ../../verify-robosuite-cube_lifting-def/work/fix_code.py"),
              command("find / -name cube_lifting_fix_code.py")]
    (log / "agent-codex.jsonl").write_text("".join(clean + broken))
    a_run("0002", 2, work / "code" / "v00.py")
    a_run("0102", 102, root / "examples" / "x.py", code="result = 'solved'\n")
    found = " ".join(T.audit(s, work, log))
    for evidence in (str(root), "verify-robosuite-cube_lifting-def", "cube_lifting_fix_code.py", "seed 2",
                     "outside the workspace", "worked solutions"):
        assert evidence in found, evidence
    (log / "agent-codex.jsonl").unlink()
    assert "no session log" in " ".join(T.audit(s, work, log))


def test_a_refused_program_is_scored_zero_not_an_error(tmp_path, monkeypatch):
    T = _task_module()
    root = fake_aspire(tmp_path)
    work, log = tmp_path / "ws" / "w" / "work", tmp_path / "ws" / "w" / "log"
    (work / "robosuite" / "cube_lifting" / "task_0").mkdir(parents=True)
    log.mkdir()
    (log / "agent-codex.jsonl").write_text("")
    (work / "robosuite" / "cube_lifting" / "task_0" / "fix_code.py").write_text("import os\n")

    class Refusing:
        def __init__(self, *a, **k):
            pass

        def validate(self, program):
            return "ValueError: only math"
    monkeypatch.setattr(T.sim, "Engine", Refusing)
    facts = T.collect({"id": "robosuite-cube_lifting", "suite": "cube_lifting"}, work, log,
                      {"aspire_root": str(root), "eval_seeds": "1-3"})
    assert facts["scored_passed"] == 0 and "only math" in facts["scored_refused"]


def test_the_judge_scores_the_scored_seeds_and_a_verifier_only_when_verified():
    sys.path.insert(0, str(EXAMPLE))
    try:
        spec_ = importlib.util.spec_from_file_location("aspire_judge", EXAMPLE / "judge.py")
        J = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(J)
    finally:
        sys.path.remove(str(EXAMPLE))
    out = lambda facts: SimpleNamespace(extra={"task": facts}, output=None)
    actor = {"role": "actor", "scored_seeds": [1, 2, 3, 4], "scored_passed": 3, "unseen": {"passed": 1, "total": 1}}
    assert J.score({}, out(actor))["score"] == 0.75
    verifier = {"role": "verifier", "scored_seeds": [101, 102, 103, 104, 105], "scored_passed": 5}
    assert J.score({}, out({**verifier, "verification": {"verdict": "verified"}}))["score"] == 1.0
    assert J.score({}, out({**verifier, "verification": {"verdict": "void"}}))["score"] == 0.0
    assert J.score({}, out(verifier))["score"] == 0.0


def test_prune_drops_the_runs_images_and_keeps_their_records(tmp_path):
    T = _task_module()
    run = tmp_path / "run" / "work" / "debug" / "0101" / "run-x"
    (run / "media" / "keyframes").mkdir(parents=True)
    (run / "media" / "keyframes" / "a.png").write_bytes(b"png")
    (run / "run.json").write_text('{"seed": 101}')
    (tmp_path / "run" / "work" / "notes" / "media").mkdir(parents=True)     # not a run's: kept
    T.prune(tmp_path / "run")
    assert not (run / "media").exists() and (run / "run.json").is_file()
    assert (tmp_path / "run" / "work" / "notes" / "media").is_dir()
