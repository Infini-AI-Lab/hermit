"""ASPIRE: the aspire template (the skill library, its reader and its check), the coordinator's hook into the
Trainer (next_tasks), and the example's pieces: the Codex task functions (prompt assembly, the workspace and its
registration), the simulator service's rules as bin/sim meets them (a real service on a loopback port, over a fake
checkout), the judge's Stage 2 (a stand-in engine), and pure helpers; no simulator, no model, no network."""
import importlib.util
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

ROOT = Path(__file__).resolve().parents[3]
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

def _load(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    try:
        spec_ = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(path.parent))


def _task_module():
    return _load("aspire_task", EXAMPLE / "agent" / "task.py")


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
    for name in ("api-reference.md", "run-artifacts.md", "robosuite.md"):
        (root / "docs" / name).write_text(f"# {name}\nrun examples/robosuite/cube_lifting_seed1.py\nkept\n")
    (skills / "skills").mkdir()
    (skills / "skills" / "task-exploration.md").write_text("# exploration\n")
    evosearch = root / ".claude" / "skills" / "evosearch"
    (evosearch / "skills").mkdir(parents=True)
    (evosearch / "subagent-prompt.md").write_text("intro\n\n```\n## Task Assignment\n\nBENCHMARK: <x>\n\n---\n\n"
                                                  "## EVAL SET LOCKOUT\nsearch text\n```\n")
    (evosearch / "skills" / "evosearch-iteration.md").write_text("# iteration\n")
    return root


ROW = {"id": "robosuite-cube_lifting", "suite": "cube_lifting", "task_id": 0, "debug_seeds": "101-103",
       "unseen_seeds": "126", "eval_seeds": "1-3"}
VERIFY = {"id": "verify-robosuite-cube_lifting", "suite": "cube_lifting", "task_id": 0, "role": "verifier",
          "debug_seeds": "101-105", "eval_seeds": "none"}


def test_the_actor_prompt_is_the_header_the_assignment_and_aspires_template(tmp_path):
    T = _task_module()
    params = {"aspire_root": str(fake_aspire(tmp_path))}
    text = T.prompt(ROW, params, tmp_path)
    assert "fix-loop subagent" in text and "`$TASK_DIR` is `robosuite/cube_lifting/task_0`" in text
    assert "DEV_SEEDS: 101-103" in text and "UNSEEN_SEEDS: 126" in text and "HELDOUT_COUNT: 3" in text
    assert "## What You Are\nfix-loop text" in text and "BENCHMARK: <x>" not in text   # ours replaces theirs
    # Part 2, the evolutionary search, follows the fix loop with its own page and its template after its assignment
    assert text.index("fix-loop text") < text.index("# Part 2: the evolutionary search") < text.index("search text")
    assert "`TASK_DIR` is `evosearch/robosuite/cube_lifting/task_0`" in text
    assert "# Part 2" not in T.prompt(ROW, {**params, "evosearch": False}, tmp_path)
    verifier = T.prompt(VERIFY, params, tmp_path)
    assert "skill verifier" in verifier and "Development seeds: **101-105**" in verifier
    assert "You are verifying. Seeds 51-55." in verifier and "# Part 2" not in verifier


class _Service:
    """The simulator service on a free loopback port, over a fake checkout (no simulator: a run that passes the
    rules fails to start cap-harness, which is not what these tests hold)."""

    def __init__(self, tmp: Path, root: Path):
        import threading
        from http.server import ThreadingHTTPServer
        S = _load("aspire_sim_service", EXAMPLE / "agent" / "sim_service.py")
        self.sessions = tmp / "sessions"
        self.sessions.mkdir()
        service = S.Service(root, {r["id"]: r for r in (ROW, VERIFY)}, [self.sessions], S.Ledger(tmp / "ledger.json"),
                            {}, 2)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), S.handler(service))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def service(tmp_path, monkeypatch):
    svc = _Service(tmp_path, fake_aspire(tmp_path))
    monkeypatch.setenv("ASPIRE_SIM_URL", svc.url)
    yield svc
    svc.close()


def _workspace(service, task: dict, hexa: str = "56d9fea2") -> Path:
    """A session's workspace where the framework puts it: <logs>/<role>/<task>-<hex>/workspaces/<...>/work."""
    work = service.sessions / "rollout" / f"{task['id']}-{hexa}" / "workspaces" / f"{task['id']}-da10428a" / "work"
    (work / "hermit" / "aspire" / "skills").mkdir(parents=True)
    return work


def _register(url: str, body: dict) -> dict:
    import urllib.request
    request = urllib.request.Request(url + "/register", data=json.dumps(body).encode())
    with urllib.request.urlopen(request) as reply:
        return json.loads(reply.read())


def _sim(work: Path, *args):
    return subprocess.run([str(work / "bin" / "sim"), *args], capture_output=True, text=True, cwd=work)


def test_prepare_registers_the_session_and_the_service_holds_its_rules(tmp_path, service):
    T = _task_module()
    work = _workspace(service, ROW)
    params = {"aspire_root": str(tmp_path / "ASPIRE")}
    T.prepare(ROW, work, params, tmp_path)
    for name in ("task.md", "docs/api-reference.md", "docs/evosearch-iteration.md", "docs/task-exploration.md",
                 "scripts/scene_snapshot.py", "bin/sim", "bin/sim.json"):
        assert (work / name).is_file(), name
    assert "examples/" not in (work / "docs" / "robosuite.md").read_text()
    assert "kept" in (work / "docs" / "robosuite.md").read_text()
    (work / "p.py").write_text("result = {}\n")
    held_out = _sim(work, "run-batch", "--seeds", "1-3", "--program", "p.py", "--output-root", "out")
    assert held_out.returncode == 2 and "not assigned to you" in held_out.stderr
    outside = _sim(work, "run", "--seed", "101", "--program", "p.py", "--output-root", str(tmp_path / "elsewhere"))
    assert outside.returncode == 2 and "inside the workspace" in outside.stderr
    _sim(work, "run", "--seed", "126", "--program", "p.py", "--output-root", "u", "--init-mode", "saved")
    again = _sim(work, "run", "--seed", "126", "--program", "p.py", "--output-root", "u")
    assert again.returncode == 2 and "run once" in again.stderr
    with pytest.raises(RuntimeError, match="registered its session already"):
        T.prepare(ROW, work, params, tmp_path)                    # a session cannot register itself again


def test_a_session_cannot_forge_a_registration_for_more_seeds(tmp_path, service):
    T = _task_module()
    work = _workspace(service, ROW)
    T.prepare(ROW, work, {"aspire_root": str(tmp_path / "ASPIRE")}, tmp_path)
    forged = work / "x"                                         # a fresh directory the session can write
    forged.mkdir()
    (forged / "p.py").write_text("result = {}\n")
    answer = _register(service.url, {"workspace": str(forged), "task": ROW["id"], "dev_seeds": [1, 2, 3]})
    assert not answer["ok"] and "registered its session already" in answer["error"]
    other = _workspace(service, ROW, "0badc0de")                # another rollout (the framework's): the row's seeds,
    assert _register(service.url, {"workspace": str(other), "task": ROW["id"], "dev_seeds": [1, 2, 3],
                                   "max_programs": None})["ok"]   # whatever the request says
    (other / "bin").mkdir()
    shutil.copy2(EXAMPLE / "agent" / "sim_cli.py", other / "bin" / "sim_cli.py")
    (other / "bin" / "sim.json").write_text(json.dumps({"workspace": str(other), "url": service.url}))
    (other / "p.py").write_text("result = {}\n")
    held_out = subprocess.run([sys.executable, str(other / "bin" / "sim_cli.py"), "run-batch", "--seeds", "1-3",
                               "--program", "p.py", "--output-root", "out"], capture_output=True, text=True, cwd=other)
    assert held_out.returncode == 2 and "not assigned to you" in held_out.stderr
    mismatched = _workspace(service, VERIFY, "12345678")        # a rollout registers only the task its name carries
    answer = _register(service.url, {"workspace": str(mismatched), "task": ROW["id"]})
    assert not answer["ok"] and "is not one of task" in answer["error"]


def test_a_verifier_may_run_two_programs_and_sees_the_staged_proposals(tmp_path, service):
    T = _task_module()
    work = _workspace(service, VERIFY)
    (work / "hermit" / "aspire" / "skills" / "proposed.md").write_text(ENTRY)
    T.prepare(VERIFY, work, {"aspire_root": str(tmp_path / "ASPIRE")}, tmp_path)
    assert (work / "proposed_skills.md").read_text() == ENTRY
    codes = []
    for name in ("a.py", "a.py", "c.py"):                       # the same path, rewritten: a new program
        (work / name).write_text(f"result = {{'v': {len(codes)}}}\n")
        codes.append(_sim(work, "run-batch", "--seeds", "101-105", "--program", name, "--output-root", "out"))
    assert "attempt budget" not in codes[0].stderr and "attempt budget" not in codes[1].stderr
    assert codes[2].returncode == 2 and "attempt budget is 2 programs" in codes[2].stderr


def test_the_judge_runs_stage_two_on_the_rows_seeds_and_a_verifier_only_when_verified(tmp_path, monkeypatch):
    J = _load("aspire_judge", EXAMPLE / "judge.py")
    runs = []

    class Engine:
        def __init__(self, *a, **k):
            pass

        def validate(self, program):
            return "ValueError: only math" if "import os" in program.read_text() else None

        def run(self, program, *, suite, seeds, out, task_id=0):
            runs.append(seeds)
            return [{"task_success": s % 2 == 1} for s in seeds]
    monkeypatch.setattr(J.sim, "Engine", Engine)
    monkeypatch.setenv("ASPIRE_ROOT", str(tmp_path))
    monkeypatch.setenv("ASPIRE_OUT", str(tmp_path / "out"))
    out = lambda facts: SimpleNamespace(extra={"task": facts}, output=None)
    actor = {"role": "actor", "fix_code": "result = {}\n", "unseen": {"passed": 1, "total": 1}}
    assert J.score(ROW, out(actor))["score"] == 2 / 3 and runs[-1] == [1, 2, 3]   # the row's held-out seeds
    assert J.score(ROW, out({**actor, "fix_code": "import os\n"}))["score"] == 0.0
    verified = {"role": "verifier", "fix_code": "result = {}\n", "verification": {"verdict": "verified"}}
    assert J.score(VERIFY, out(verified))["score"] == 3 / 5 and runs[-1] == [101, 102, 103, 104, 105]
    assert J.score(VERIFY, out({**verified, "verification": {"verdict": "void"}}))["score"] == 0.0
    assert J.score(ROW, out({"role": "actor"}))["score"] == 0.0                 # no program: not scored


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
