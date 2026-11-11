"""ASPIRE: the aspire template (the skill library, its reader and its check), the coordinator's hook into the
Trainer (next_tasks), and the example's pieces: the Codex task functions (prompt assembly, the workspace and its
registration), the simulator service's rules as bin/sim meets them (a real service on a loopback port, over a fake
checkout), the judge's Stage 2 (a stand-in engine), and pure helpers; no simulator, no model, no network."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

from hermit.agent.evolver.base import StepResult
from hermit.agent.evolver.methods.aspire import evolver as E
from hermit.spec import check_spec

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "hermit" / "adapter" / "templates" / "aspire"
EXAMPLE = ROOT / "examples" / "aspire_robosuite" / "utils"
LIBERO = ROOT / "examples" / "aspire_libero"         # LIBERO on the same engine: its own tasks and configs

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


def test_check_reads_the_snippets_outside_the_entries_too(tmp_path):
    """The actor reads a skill file whole and ADAPTER.md above it: a snippet before a file's entries or in ADAPTER.md
    is checked like an entry's."""
    adapter = seed(tmp_path)
    grasp = adapter / "skills" / "grasp.md"
    head, sep, rest = grasp.read_text().partition("\n## ")
    grasp.write_text(head + "\n```python\nimport os\n```\n" + sep + rest if sep else
                     grasp.read_text() + "\n```python\nimport os\n```\n")
    (adapter / "ADAPTER.md").write_text((adapter / "ADAPTER.md").read_text() + "\n```python\nx = env.sim\n```\n")
    problems = " ".join(i["problem"] for i in spec(adapter)["issues"])
    assert "skills/grasp.md (before its entries): the snippet would be refused" in problems
    assert "ADAPTER.md: the snippet uses privileged access (env.sim)" in problems


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

def test_next_tasks_reads_only_a_regular_file(tmp_path):
    """output/next_tasks.json is the learner's, read on the host: a link or a pipe in its place names no task."""
    path = tmp_path / "next_tasks.json"
    path.write_text('["robosuite-cube_stack"]')
    assert E.next_tasks(path) == ["robosuite-cube_stack"]
    path.unlink()
    (tmp_path / "host.json").write_text('["robosuite-cube_stack"]')
    path.symlink_to(tmp_path / "host.json")
    assert E.next_tasks(path) is None
    path.unlink()
    os.mkfifo(path)
    assert E.next_tasks(path) is None                           # returns at once, not blocked on the pipe

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


def test_the_direct_actor_keeps_its_models_settings_and_an_unknown_cost_unknown(monkeypatch):
    import threading
    from hermit.agent.model import llm
    A = _actor_module()
    specs = []
    monkeypatch.setattr(llm.ChatClient, "from_spec", classmethod(lambda cls, spec, **k: specs.append(spec)))
    monkeypatch.setattr("hermit.agent.base.interface.make", lambda *a, **k: object())
    model = {"base_url": "http://127.0.0.1:9/v1", "model": "m", "timeout": 7,
             "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    A.AspireActor({"model": model, "effort": "high"})._base_agent()
    assert specs[-1]["timeout"] == 7
    assert specs[-1]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "high"}
    A.AspireActor({"model": {**model, "extra_body": {"reasoning_effort": "low"}}})._base_agent()
    assert specs[-1]["extra_body"] == {"reasoning_effort": "low"}            # the model's own, when the actor names none
    run = SimpleNamespace(totals={"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "cut_off": 0,
                                  "unpriced": 0}, _lock=threading.Lock(), transcript=[])
    A._Run.account(run, [SimpleNamespace(input_tokens=10, output_tokens=5, cost_usd=None)], "v00", "r", "", "stop")
    assert run.totals["unpriced"] == 1 and run.totals["cost_usd"] == 0.0     # RunOutput then carries None


def test_a_failed_direct_task_keeps_what_it_spent(tmp_path, monkeypatch):
    A = _actor_module()

    class Engine:
        def __init__(self, root, **k):
            self.root = Path(root)

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-reference.md").write_text("# api\n")

    def explore(run):                                           # one paid call, then an outage past the retries
        run.account([SimpleNamespace(input_tokens=10, output_tokens=5, cost_usd=0.5)], "explore", "r", "", "stop")
        raise RuntimeError("model unavailable: 503")
    monkeypatch.setattr(A.sim, "Engine", Engine)
    monkeypatch.setattr(A._Run, "explore", explore)
    actor = A.AspireActor({"aspire_root": str(tmp_path), "scratch": str(tmp_path / "scratch")}, base=tmp_path)
    monkeypatch.setattr(actor, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(actor, "session", lambda adapter: None)
    out = actor.run(dict(ROW), None)
    assert out.cost_usd == 0.5 and out.tokens == 15 and "503" in out.extra["session_failed"]
    from hermit.agent.base.interface import LoginFailed
    from hermit.trainer.rollout import run_one
    monkeypatch.setattr(A._Run, "explore", lambda run: (_ for _ in ()).throw(RuntimeError("hermit_login_failed: x")))
    with pytest.raises(LoginFailed):                            # a lost sign-in stops the run, not one task
        run_one(dict(ROW), None, lambda task, adapter: actor.run(task, adapter), lambda task, out: {"score": 0})


def test_sim_compact_makes_traces_readable():
    sim = _sim_module()
    value = {"$type": "Pose", "position": {"$array_inline": [0.123456, 1.0, 2.0], "dtype": "float64", "shape": [3]},
             "obs": {"$type": "Observation", "cameras": {}}, "big": {"$array_inline": list(range(40)), "shape": [40]}}
    out = sim.compact(value)
    assert out == {"type": "Pose", "position": [0.1235, 1.0, 2.0], "obs": "<Observation>",
                   "big": "<array None shape [40]>"}


def test_the_failure_keyframe_is_the_failing_calls_own(tmp_path):
    sim = _sim_module()
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
    (root / "docs" / "libero-pro.md").write_text("# LIBERO-Pro\nkept\n\n## Task examples\n\n| `close_microwave.py` | "
                                                 "`libero_90:33` | Close the open microwave. |\n")
    for venv in (".venv-robosuite", ".venv-libero"):
        harness = root / venv / "bin" / "cap-harness"
        harness.parent.mkdir(parents=True)
        harness.write_text(FAKE_HARNESS.format(python=sys.executable))
        harness.chmod(0o755)
    return root


# cap-harness without a simulator: the run's files where ASPIRE writes them (run.json, results.jsonl naming the run's
# directory, outcome.json), the program and settings it was given; a program saying SLEEP runs until stopped, and its
# `# WRITE <path>` / `# READ <path>` lines are tried as a program that escaped ASPIRE's validator would (in run.json),
# and after the run `# LINK <file> <target>` leaves a link in its output, `# RUNDIR <dir>` names another run_dir and
# `# CHMOD <path> <mode>` changes a mode (last).
FAKE_HARNESS = """#!{python}
import argparse, json, os, pathlib, sys, time
ap = argparse.ArgumentParser()
for flag in ("--benchmark", "--suite", "--task-id", "--program", "--output-root", "--max-steps", "--seed", "--workers",
             "--results-jsonl", "--log-dir", "--init-mode"):
    ap.add_argument(flag)
ap.add_argument("--seeds", nargs="+")
ap.add_argument("--flat-run-dir", action="store_true")
ap.add_argument("--no-videos", action="store_true")
ap.add_argument("cmd")
a = ap.parse_args()
code = pathlib.Path(a.program).read_text()
if "SLEEP" in code:
    time.sleep(60)
tried = {{}}
for line in code.splitlines():
    what, _, path = line.partition(" ")[2].partition(" ")
    try:
        if what == "WRITE":
            pathlib.Path(path).write_text("escaped")
        elif what == "READ":
            pathlib.Path(path).read_text()
        else:
            continue
        tried[path] = "done"
    except OSError as exc:
        tried[path] = type(exc).__name__
out = pathlib.Path(a.output_root)
run = out / "run-1"
run.mkdir(parents=True, exist_ok=True)
(out / "run.json").write_text(json.dumps({{"program": a.program, "max_steps": a.max_steps, "code": code,
                                          "env": sorted(os.environ), "home": os.environ.get("HOME"),
                                          "tried": tried}}))
(run / "outcome.json").write_text(json.dumps({{"task_success": True}}))
rows = [{{"seed": int(s), "run_dir": str(run)}} for s in (a.seeds or [a.seed])]
for line in code.splitlines():
    what, _, rest = line.partition(" ")[2].partition(" ")
    if what == "LINK":
        name, target = rest.split()
        (out / name).unlink(missing_ok=True)
        (out / name).symlink_to(target)
    elif what == "RUNDIR":
        rows = [dict(r, run_dir=rest) for r in rows]
pathlib.Path(a.results_jsonl or out / "results.jsonl").write_text("".join(json.dumps(r) + chr(10) for r in rows))
for line in code.splitlines():
    what, _, rest = line.partition(" ")[2].partition(" ")
    if what == "CHMOD":
        name, mode = rest.split()
        os.chmod(out / name, int(mode, 8))
print("ran", a.program, "into", out, "max_steps", a.max_steps)
"""


def _bwrap_works() -> bool:
    """A bubblewrap that can make its namespaces here: every simulator run is confined (sim.confine)."""
    if not shutil.which("bwrap"):
        return False
    return subprocess.run(["bwrap", "--unshare-user", "--unshare-net", "--ro-bind", "/", "/", "true"],
                          capture_output=True).returncode == 0


needs_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap: simulator runs are confined")


LIBERO_ROW = {"id": "libero_90-t33", "benchmark": "libero-pro", "suite": "libero_90", "task_id": 33,
              "debug_seeds": "51-53", "unseen_seeds": "66", "eval_seeds": "1-3"}
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
    """The simulator service on a free loopback port, over a fake checkout whose cap-harness writes a run's files
    without a simulator (FAKE_HARNESS)."""

    def __init__(self, tmp: Path, root: Path, **options):
        import threading
        from http.server import ThreadingHTTPServer
        S = _load("aspire_sim_service", EXAMPLE / "agent" / "sim_service.py")
        self.sessions = tmp / "sessions"
        self.sessions.mkdir()
        self.service = service = S.Service(root, {r["id"]: r for r in (ROW, VERIFY, LIBERO_ROW)}, [self.sessions],
                                           S.Ledger(tmp / "ledger.json"), {}, 2, **options)
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
    T.prepare(ROW, work, params, tmp_path)                        # a retry's prepare: the registration as it was
    again = _sim(work, "run", "--seed", "126", "--program", "p.py", "--output-root", "u")
    assert again.returncode == 2 and "run once" in again.stderr     # what the session spent stays spent


def test_a_libero_task_runs_on_its_own_venv_seeded_with_its_own_docs(tmp_path, service, monkeypatch):
    T = _task_module()
    params = {"aspire_root": str(tmp_path / "ASPIRE")}
    text = T.prompt(LIBERO_ROW, params, tmp_path)
    assert "BENCHMARK: libero-pro" in text and "`$TASK_DIR` is `libero-pro/libero_90/task_33`" in text
    assert "the benchmark (libero-pro)" in text and "`docs/libero-pro.md`" in text and "robosuite" not in text
    assert "`TASK_DIR` is `evosearch/libero-pro/libero_90/task_33`" in text and "DEV_SEEDS: 51-53" in text
    work = _workspace(service, LIBERO_ROW)
    T.prepare(LIBERO_ROW, work, params, tmp_path)
    page = (work / "docs" / "libero-pro.md").read_text()
    assert "kept" in page and "Task examples" not in page and "microwave" not in page   # no worked solution named
    assert not (work / "docs" / "robosuite.md").exists()
    ran = []
    monkeypatch.setattr(service.service, "_execute", lambda cmd, env, *a, **k: ran.append((cmd, env)) or (0, "", ""))
    (work / "p.py").write_text("result = {}\n")
    _sim(work, "run-batch", "--seeds", "51-52", "--program", "p.py", "--output-root", "out", "--init-mode", "saved")
    cmd, env = ran[-1]
    assert cmd[0] == str(tmp_path / "ASPIRE" / ".venv-libero" / "bin" / "cap-harness")
    option = lambda flag: cmd[cmd.index(flag) + 1]
    assert (option("--benchmark"), option("--suite"), option("--task-id")) == ("libero-pro", "libero_90", "33")
    assert option("--init-mode") == "seeded"                    # the engine's, whatever the session passed
    assert env["LIBERO_CONFIG_PATH"] == str(tmp_path / "ASPIRE" / ".libero")
    assert env["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] == "1"
    _sim(work, "validate", "--program", "p.py")
    assert ran[-1][0][0] == str(tmp_path / "ASPIRE" / ".venv-libero" / "bin" / "python")
    # Robosuite's runs are as they were: its venv, no init mode
    robosuite = _workspace(service, ROW)
    T.prepare(ROW, robosuite, params, tmp_path)
    (robosuite / "p.py").write_text("result = {}\n")
    _sim(robosuite, "run", "--seed", "101", "--program", "p.py", "--output-root", "out")
    cmd, env = ran[-1]
    assert cmd[0].endswith(".venv-robosuite/bin/cap-harness") and "--init-mode" not in cmd
    assert "LIBERO_CONFIG_PATH" not in env


def test_the_task_sandbox_is_shown_the_staged_docs_and_never_a_page_that_lost_its_cut(tmp_path, service):
    """ASPIRE's benchmark pages name its worked programs per task: an actor that could read one would be handed
    a strategy for its task. The configs grant the staged copies only, and a page without its cut is refused."""
    T = _task_module()
    root = tmp_path / "ASPIRE"
    (tmp_path / "rows.jsonl").write_text(json.dumps(ROW) + "\n" + json.dumps(LIBERO_ROW) + "\n")
    staged = tmp_path / "staged"
    done = subprocess.run([sys.executable, str(EXAMPLE / "agent" / "stage_docs.py"), "--aspire-root", str(root),
                           "--tasks", str(tmp_path / "rows.jsonl"), "--out", str(staged)],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in staged.iterdir()) == ["api-reference.md", "libero-pro.md", "robosuite.md",
                                                        "run-artifacts.md"]
    assert all("examples/" not in p.read_text() and "microwave" not in p.read_text() for p in staged.iterdir())
    (staged / "libero-pro.md").write_text("# the staged page\n")
    work = _workspace(service, LIBERO_ROW)
    T.prepare(LIBERO_ROW, work, {"aspire_root": str(root), "aspire_docs": str(staged)}, tmp_path)
    assert (work / "docs" / "libero-pro.md").read_text() == "# the staged page\n"     # the staged copy, as it is
    (root / "docs" / "libero-pro.md").write_text("# LIBERO-Pro\n\n## Worked programs\n| `close_microwave.py` |\n")
    with pytest.raises(ValueError, match="Task examples"):
        _sim_module().workspace_docs(root, "libero-pro")
    # No Codex config of either example grants a page of the checkout's own docs, and each loads
    from hermit.config import Config
    configs = [p for folder in (EXAMPLE.parent, LIBERO) for p in sorted((folder / "aspire").glob("*.yaml"))]
    assert len(configs) == 7
    for path in configs:
        data = Config.load(path).data
        read = [str(p) for p in (data.get("environment") or {}).get("read") or []]
        assert not any("/docs/" in p for p in read), path
        if "_direct" not in path.name:
            assert "$ASPIRE_DOCS" in read and data["agent"]["params"]["aspire_docs"] == "$ASPIRE_DOCS", path
        for key in ("train", "val", "test"):                    # the splits prepare.py writes
            name = Path(data["data"][key]).name
            assert name in {"all.txt", "rerun.txt", "verify.txt", "smoke.txt", "smoke_rerun.txt", "libero.txt",
                            "libero_rerun.txt", "libero_90.txt", "libero_90_rerun.txt"}, (path, name)


def test_the_engine_takes_the_benchmarks_venv_and_refuses_one_it_does_not_run(tmp_path):
    sim = _sim_module()
    root = fake_aspire(tmp_path)
    engine = sim.Engine(root, benchmark="libero-pro")
    assert engine.cli == root.resolve() / ".venv-libero" / "bin" / "cap-harness"
    assert engine._env()["LIBERO_CONFIG_PATH"] == str(root.resolve() / ".libero")
    assert sim.init_args("libero-pro") == ["--init-mode", "seeded"] and sim.init_args(None) == []
    assert sim.Engine(root).cli.parent.parent.name == ".venv-robosuite"
    shutil.rmtree(root / ".venv-libero")
    with pytest.raises(FileNotFoundError, match="bootstrap_libero.sh"):
        sim.Engine(root, benchmark="libero-pro")
    with pytest.raises(ValueError, match="behavior"):
        sim.Engine(root, benchmark="behavior")


def test_the_libero_tasks_are_liberos_five_suites_with_aspires_seeds():
    P = _load("aspire_libero_prepare", LIBERO / "utils" / "prepare.py")
    assert sum(P.SUITES.values()) == 130 and P.SUITES["libero_90"] == 90
    seeds, verify = P.seeds_from(P.PRESETS, [])
    assert seeds == {"debug_seeds": "51-65", "unseen_seeds": "66-80", "eval_seeds": "1-50"} and verify == "51-55"
    assert P.seeds_from(P.PRESETS, ["--preset", "smoke", "--eval", "1-5"])[0]["eval_seeds"] == "1-5"
    with pytest.raises(SystemExit):
        P.seeds_from(P.PRESETS, ["--eval", "1-60"])              # held-out seeds a session could run


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


@needs_bwrap
def test_the_service_follows_no_link_the_session_puts_in_its_workspace(tmp_path, service):
    """A run is written by the service into its own directory and moved into the workspace: a link the session put
    where the run's files go is replaced, not followed, and a program that is a link is not read."""
    T = _task_module()
    work = _workspace(service, ROW)
    T.prepare(ROW, work, {"aspire_root": str(tmp_path / "ASPIRE")}, tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("the host's own file\n")
    (work / "out").mkdir()
    (work / "out" / "run.json").symlink_to(victim)               # where cap-harness writes
    (work / "out" / "run-1").symlink_to(tmp_path)                # a directory of the run's
    (work / "p.py").write_text("result = {}\n")
    ran = _sim(work, "run", "--seed", "101", "--program", "p.py", "--output-root", "out")
    assert victim.read_text() == "the host's own file\n" and not (tmp_path / "outcome.json").exists()
    assert ran.returncode == 2 and "a link in its path" in ran.stderr
    (work / "out" / "run-1").unlink()
    ran = _sim(work, "run", "--seed", "101", "--program", "p.py", "--output-root", "out")
    assert ran.returncode == 0 and victim.read_text() == "the host's own file\n"
    record = json.loads((work / "out" / "run.json").read_text())
    assert not (work / "out" / "run.json").is_symlink() and record["program"] == str(work / "p.py")
    rows = (work / "out" / "results.jsonl").read_text()
    assert json.loads(rows)["run_dir"] == str(work / "out" / "run-1")     # the workspace's paths, not the service's
    assert str(work / "p.py") in ran.stdout and str(service.service.scratch) not in ran.stdout
    secret = tmp_path / "secret.py"
    secret.write_text("KEY = 'host'\n")
    (work / "q.py").symlink_to(secret)
    refused = _sim(work, "validate", "--program", "q.py")
    assert refused.returncode == 2 and "no program at q.py" in refused.stderr
    assert list(service.service.scratch.iterdir()) == []                 # the service's copies are gone
    import urllib.request
    service.service.nonce = "n-1"
    with urllib.request.urlopen(service.url + "/health") as reply:         # launch.sh knows its own service by it
        assert json.loads(reply.read())["nonce"] == "n-1"
    service.service.stop()
    late = _sim(work, "run", "--seed", "101", "--program", "p.py", "--output-root", "out")
    assert late.returncode == 1 and "stopping" in late.stderr          # no run starts once the service stops


def test_bin_sim_quotes_the_interpreter(tmp_path, service, monkeypatch):
    T = _task_module()
    work = _workspace(service, ROW)
    monkeypatch.setattr(T.sys, "executable", "/opt/hermit env/bin/python")
    T.prepare(ROW, work, {"aspire_root": str(tmp_path / "ASPIRE")}, tmp_path)
    assert "exec '/opt/hermit env/bin/python' " in (work / "bin" / "sim").read_text()


@needs_bwrap
def test_a_program_that_escaped_the_validator_still_cannot_reach_the_host(tmp_path, service):
    """cap-harness runs in a sandbox of its own (sim.confine): a program in it writes only its run's own output and
    reads ASPIRE's checkout, not the host's files, another session's workspace or the judge's runs."""
    T = _task_module()
    work = _workspace(service, ROW)
    T.prepare(ROW, work, {"aspire_root": str(tmp_path / "ASPIRE")}, tmp_path)
    secret, stage2 = tmp_path / "secret.txt", tmp_path / "out" / "stage2" / "robosuite-x" / "stage2.json"
    secret.write_text("host secret\n")
    stage2.parent.mkdir(parents=True)
    stage2.write_text("{}")
    other = _workspace(service, ROW, "0badc0de")
    (other / "fix_code.py").write_text("result = {'theirs': 1}\n")
    solution = tmp_path / "ASPIRE" / "examples" / "robosuite" / "cube_lifting_seed1.py"     # a worked solution
    solution.parent.mkdir(parents=True)
    solution.write_text("result = {'worked': 1}\n")
    history = tmp_path / "ASPIRE" / ".git" / "HEAD"                                # the git history holds it too
    history.parent.mkdir()
    history.write_text("ref: refs/heads/main\n")
    native = tmp_path / "ASPIRE" / "outputs" / "run-1" / "source" / "program.py"   # a run of ASPIRE's own CLI
    native.parent.mkdir(parents=True)
    native.write_text("result = {'native': 1}\n")
    targets = {"WRITE": [tmp_path / "planted.txt", tmp_path / "ASPIRE" / "planted.py", work / "planted.txt"],
               "READ": [secret, stage2, other / "fix_code.py", solution, history, native,
                        tmp_path / "ASPIRE" / "docs" / "api-reference.md"]}
    (work / "p.py").write_text("result = {}\n" + "".join(f"# {w} {p}\n" for w, ps in targets.items() for p in ps))
    ran = _sim(work, "run", "--seed", "101", "--program", "p.py", "--output-root", "out")
    tried = json.loads((work / "out" / "run.json").read_text())["tried"]
    assert ran.returncode == 0, ran.stderr
    assert not (tmp_path / "planted.txt").exists() and not (tmp_path / "ASPIRE" / "planted.py").exists()
    assert tried[str(work / "planted.txt")] != "done" and not (work / "planted.txt").exists()   # its workspace too
    assert all(tried[str(p)] != "done" for p in (secret, stage2, other / "fix_code.py", solution, history, native))
    assert tried[str(tmp_path / "ASPIRE" / "docs" / "api-reference.md")] == "done"      # the checkout it runs from


@needs_bwrap
def test_the_validator_runs_on_aspires_own_venv_inside_its_sandbox(tmp_path, monkeypatch):
    """ASPIRE's venv interpreter is a link to its base installation: confined, it keeps the venv (whose cap_harness
    the validator imports), for the service's `bin/sim validate` and the judge's Engine alike."""
    root = fake_aspire(tmp_path)
    venv = root / ".venv-robosuite"
    shutil.move(str(venv / "bin" / "cap-harness"), str(tmp_path / "cap-harness"))
    shutil.rmtree(venv)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    shutil.move(str(tmp_path / "cap-harness"), str(venv / "bin" / "cap-harness"))
    site = next(venv.glob("lib/python3*/site-packages"))
    (site / "cap_harness").mkdir()
    (site / "cap_harness" / "__init__.py").write_text("")
    (site / "cap_harness" / "runtime.py").write_text(
        "import ast\nclass _ProgramValidator(ast.NodeVisitor):\n    def visit_Import(self, node):\n"
        "        if any(a.name != 'math' for a in node.names):\n            raise ValueError('only math')\n")
    assert (venv / "bin" / "python").is_symlink()
    S = _load("aspire_sim_service_venv", EXAMPLE / "agent" / "sim_service.py")
    service = S.Service(root, {ROW["id"]: ROW}, [tmp_path / "sessions"], S.Ledger(tmp_path / "ledger.json"), {}, 2)
    work = tmp_path / "sessions" / "rollout" / f"{ROW['id']}-1a2b" / "workspaces" / "w" / "work"
    work.mkdir(parents=True)
    assert service.register({"workspace": str(work), "task": ROW["id"]})["ok"]
    (work / "good.py").write_text("import math\nresult = {}\n")
    (work / "bad.py").write_text("import os\nresult = {}\n")
    good = service.run({"workspace": str(work), "cmd": "validate", "program": "good.py"})
    bad = service.run({"workspace": str(work), "cmd": "validate", "program": "bad.py"})
    assert good["code"] == 0 and "program accepted" in good["stdout"], good
    assert bad["code"] != 0 and "only math" in bad["stderr"], bad
    sim = _sim_module()
    engine = sim.Engine(root)
    assert engine.validate(work / "good.py") is None and "only math" in engine.validate(work / "bad.py")


def _sim_module():
    sys.path.insert(0, str(EXAMPLE / "agent"))
    try:
        import sim
        return sim
    finally:
        sys.path.remove(str(EXAMPLE / "agent"))


@needs_bwrap
def test_the_engine_reads_no_link_and_no_run_dir_outside_its_run(tmp_path):
    """The judge's and the direct actor's Engine read what a confined program could write: once it has ended, links
    in its output are removed and a run_dir outside the run is not read, so neither a host file nor another run's
    success reaches the records."""
    sim = _sim_module()
    root = fake_aspire(tmp_path)
    elsewhere = tmp_path / "another-run" / "seed_0001"
    elsewhere.mkdir(parents=True)
    (elsewhere / "outcome.json").write_text('{"task_success": true, "program_ok": true}')
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")
    out = tmp_path / "stage2" / "runs"
    program = tmp_path / "stage2" / "program.py"
    program.parent.mkdir()
    program.write_text(f"result = {{}}\n# FAIL\n# LINK records.json {secret}\n# RUNDIR {elsewhere}\n")
    records = sim.Engine(root, gpu=[]).run(program, suite="cube_lifting", seeds=[1, 2], out=out)
    assert [r["task_success"] for r in records] == [False, False] and "not in this run's output" in records[0]["error"]
    assert secret.read_text() == "host secret" and not (out / "records.json").is_symlink()
    program.write_text(f"result = {{}}\n# FAIL\n# LINK run-1/outcome.json {elsewhere / 'outcome.json'}\n")
    records = sim.Engine(root, gpu=[]).run(program, suite="cube_lifting", seeds=[1], out=tmp_path / "again")
    assert records[0]["task_success"] is False and not (tmp_path / "again" / "run-1" / "outcome.json").exists()
    # a directory made unlistable keeps no link from being removed: it is made listable, or the run fails
    program.write_text(f"result = {{}}\n# FAIL\n# LINK records.json {secret}\n# CHMOD . 0o111\n")
    sim.Engine(root, gpu=[]).run(program, suite="cube_lifting", seeds=[1], out=tmp_path / "locked")
    assert secret.read_text() == "host secret" and not (tmp_path / "locked" / "records.json").is_symlink()


def _fake_gpu(tmp_path, monkeypatch) -> list[int]:
    """GPU 8 as hermit's sandbox sees devices (sandbox.DEV): a machine without one can still build the command."""
    import hermit.sandbox as sandbox_module
    dev = tmp_path / "dev"
    dev.mkdir(exist_ok=True)
    for name in ("nvidiactl", "nvidia8"):
        (dev / name).touch()
    monkeypatch.setattr(sandbox_module, "DEV", str(dev))
    return [8]


@needs_bwrap
def test_the_simulators_sandbox_shows_the_renderer_and_reaches_remote_services_by_proxy(tmp_path, monkeypatch):
    """An EGL vendor library extracted by hand (its ICD and its libraries, file by file, its library found as GLVND
    finds it) and ASPIRE_SIM_READ are shown; without a GPU an NVIDIA-only ICD selection is dropped; a service on
    another host is reached through an allowlist proxy for that host only."""
    sim = _sim_module()
    home = tmp_path / "home"                       # the ICD and the libraries extracted where other files are too
    (home / "lib").mkdir(parents=True)
    (home / "credentials").write_text("key")
    icd = home / "10_nvidia.json"
    icd.write_text(json.dumps({"ICD": {"library_path": str(home / "lib" / "libEGL_nvidia.so.0")}}))
    for name in ("libEGL_nvidia.so.0", "libnvidia-eglcore.so.580", "notes.txt"):
        (home / "lib" / name).write_text("")
    extra = tmp_path / "extra"
    extra.mkdir()
    env = {"PATH": os.environ["PATH"], "__EGL_VENDOR_LIBRARY_FILENAMES": str(icd),
           "ASPIRE_SIM_READ": str(extra), "CAP_HARNESS_SAM3_URL": "http://provider.internal:9000"}
    held, args, inside = sim.confine(["true"], root=fake_aspire(tmp_path), env=env,
                                     gpu=_fake_gpu(tmp_path, monkeypatch))
    try:
        ro = set(args[i + 1] for i, a in enumerate(args) if a == "--ro-bind")
        assert {str(icd), str(home / "lib" / "libEGL_nvidia.so.0"), str(home / "lib" / "libnvidia-eglcore.so.580"),
                str(extra)} <= ro                  # the driver's files, file by file, and the explicit grant
        assert not ro & {str(home), str(home / "lib"), str(home / "credentials"), str(home / "lib" / "notes.txt")}
        assert inside["__EGL_VENDOR_LIBRARY_FILENAMES"] == str(icd)          # with a GPU (below: gpu=[8]) it stays
        assert inside["HTTPS_PROXY"].startswith("http://127.0.0.1:") and held.held[0].allow == ["provider.internal"]
    finally:
        held.close()
    # an ICD naming its library bare: GLVND finds it on LD_LIBRARY_PATH, and so is it shown
    icd.write_text(json.dumps({"ICD": {"library_path": "libEGL_nvidia.so.0"}}))
    held, args, inside = sim.confine(["true"], root=tmp_path / "ASPIRE", gpu=_fake_gpu(tmp_path, monkeypatch),
                                     env={**env, "LD_LIBRARY_PATH": str(home / "lib")})
    held.close()
    assert str(home / "lib" / "libEGL_nvidia.so.0") in [args[i + 1] for i, a in enumerate(args) if a == "--ro-bind"]
    # without a GPU an NVIDIA-only ICD selection is dropped, so EGL finds Mesa's software renderer
    held, args, inside = sim.confine(["true"], root=tmp_path / "ASPIRE", env=env)
    held.close()
    assert "__EGL_VENDOR_LIBRARY_FILENAMES" not in inside


@needs_bwrap
def test_the_service_lets_a_run_write_only_its_output_and_delivers_no_replaced_output(tmp_path, service, monkeypatch):
    """The run's own directory (its program) is read-only to it and only its output writable; an output directory
    the program replaced (by a link to a host directory) is not delivered."""
    T = _task_module()
    work = _workspace(service, ROW)
    T.prepare(ROW, work, {"aspire_root": str(tmp_path / "ASPIRE")}, tmp_path)
    (work / "p.py").write_text("result = {}\n")
    sim = type(service.service).run.__globals__["sim"]
    grants, real = [], sim.confine
    monkeypatch.setattr(sim, "confine", lambda cmd, **k: grants.append(k) or real(cmd, **k))
    assert _sim(work, "run", "--seed", "101", "--program", "p.py", "--output-root", "o").returncode == 0
    own = Path(grants[-1]["readable"][0])
    assert grants[-1]["writable"] == [own / "out"]
    host = tmp_path / "host-dir"
    host.mkdir()
    (host / "id_rsa").write_text("key")

    def replaced(cmd, env, timeout, own, out=None, gpu=()):
        shutil.rmtree(out)
        out.symlink_to(host)
        return 0, "", ""
    monkeypatch.setattr(service.service, "_execute", replaced)
    ran = _sim(work, "run", "--seed", "102", "--program", "p.py", "--output-root", "o2")
    assert ran.returncode == 2 and "replaced while it ran" in ran.stderr
    assert (host / "id_rsa").read_text() == "key" and not (work / "o2").exists()


@needs_bwrap
def test_a_program_named_pyvenv_cfg_grants_nothing(tmp_path, service):
    """A session names its program: one called pyvenv.cfg that says `home = <dir>` is the run's own file, not a venv
    of the sandbox's, so <dir> stays out of the simulator's sandbox."""
    T = _task_module()
    work = _workspace(service, ROW)
    T.prepare(ROW, work, {"aspire_root": str(tmp_path / "ASPIRE")}, tmp_path)
    operator = tmp_path / "operator"
    operator.mkdir()
    (operator / "credentials").write_text("key")
    (work / "pyvenv.cfg").write_text(f'marker = """\nhome = {operator}\n"""\nresult = {{}}\n'
                                     f"# READ {operator / 'credentials'}\n")
    ran = _sim(work, "run", "--seed", "101", "--program", "pyvenv.cfg", "--output-root", "out")
    assert ran.returncode == 0, ran.stderr
    assert json.loads((work / "out" / "run.json").read_text())["tried"][str(operator / "credentials")] != "done"


def test_the_service_refuses_to_start_on_a_gpu_the_machine_lacks(tmp_path):
    """A wrong ASPIRE_SIM_GPU stops the service at its start, not each session's run after its budget is spent."""
    (tmp_path / "tasks.jsonl").write_text(json.dumps(ROW) + "\n")
    done = subprocess.run([sys.executable, str(EXAMPLE / "agent" / "sim_service.py"), "--aspire-root",
                           str(fake_aspire(tmp_path)), "--tasks", str(tmp_path / "tasks.jsonl"), "--workspaces",
                           str(tmp_path / "w"), "--state", str(tmp_path / "state"), "--gpu", "999", "--port", "0"],
                          capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert done.returncode != 0 and ("/dev/nvidia999" in done.stderr or "bubblewrap" in done.stderr)


@needs_bwrap
def test_the_simulators_sandbox_reaches_only_the_services_ports(tmp_path):
    """The judge runs sim.Engine in hermit's process, whose environment names its own loopback endpoints: only the
    services' ports (ASPIRE's, and a CAP_HARNESS_*_URL's) are forwarded into cap-harness's sandbox."""
    sim = _sim_module()
    root = fake_aspire(tmp_path)
    box, args, _ = sim.confine(["true"], root=root, env={
        "PATH": os.environ["PATH"], "HA_TOOL_URL": "http://127.0.0.1:4999/mcp",
        "ASPIRE_SIM_URL": "http://127.0.0.1:8120", "CAP_HARNESS_SAM3_URL": "http://127.0.0.1:9114",
        "CAP_HARNESS_CUROBO_URL": "http://localhost"})                           # no port: its scheme's
    box.close()
    forwarded = sorted(int(args[i + 1].split("=")[0]) for i, a in enumerate(args) if a == "--forward")
    assert forwarded == [80, 8114, 8115, 8116, 8118, 9114]
    for where in ({"hidden": [root / "runs"]}, {"writable": [root / "scratch" / "run"]}):    # runs stay outside it
        with pytest.raises(ValueError, match="inside ASPIRE's checkout"):
            sim.confine(["true"], root=root, env={"PATH": os.environ["PATH"]}, **where)


@needs_bwrap
def test_a_run_takes_the_registrations_max_steps_and_stops_at_the_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_API_KEY", "k")       # the host's secrets: not for a generated program
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "8")         # the host's numbering: the sandbox's GPUs count from 0
    svc = _Service(tmp_path, fake_aspire(tmp_path), run_timeout=2)
    try:
        work = _workspace(svc, ROW)
        assert _register(svc.url, {"workspace": str(work), "task": ROW["id"], "max_steps": 300})["ok"]
        (work / "bin").mkdir()
        shutil.copy2(EXAMPLE / "agent" / "sim_cli.py", work / "bin" / "sim_cli.py")
        (work / "bin" / "sim.json").write_text(json.dumps({"workspace": str(work), "url": svc.url}))
        sim = lambda *args: subprocess.run([sys.executable, str(work / "bin" / "sim_cli.py"), *args],
                                           capture_output=True, text=True, cwd=work)
        (work / "p.py").write_text("result = {}\n")
        ran = sim("run", "--seed", "101", "--program", "p.py", "--output-root", "o", "--max-steps", "5000")
        record = json.loads((work / "o" / "run.json").read_text())
        assert ran.returncode == 0 and record["max_steps"] == "300"
        assert "TEST_PROVIDER_API_KEY" not in record["env"] and "PATH" in record["env"]
        assert "CUDA_VISIBLE_DEVICES" not in record["env"]
        assert not Path(record["home"]).is_relative_to(work) and not Path(record["home"]).exists()   # its own, gone
        assert list((tmp_path / "sandbox").glob("cap-harness-*.json"))      # the run's sandbox record
        (work / "slow.py").write_text("SLEEP\n")
        started = time.time()
        slow = sim("run", "--seed", "102", "--program", "slow.py", "--output-root", "s")
        assert slow.returncode == 124 and "stopped after 2 s" in slow.stderr and time.time() - started < 30
    finally:
        svc.close()


def test_the_judge_runs_stage_two_on_the_rows_seeds_and_a_verifier_only_when_verified(tmp_path, monkeypatch):
    J = _load("aspire_judge", EXAMPLE / "judge.py")
    runs, engines = [], []

    class Engine:
        def __init__(self, *a, **k):
            engines.append(k)

        def validate(self, program):
            assert program.is_absolute() and program.is_file()      # cap-harness runs in ASPIRE's checkout
            return "ValueError: only math" if "import os" in program.read_text() else None

        def run(self, program, *, suite, seeds, out, task_id=0):
            runs.append(seeds)
            return [{"task_success": s % 2 == 1} for s in seeds]
    monkeypatch.setattr(J.sim, "Engine", Engine)
    monkeypatch.setenv("ASPIRE_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASPIRE_OUT", "out")                     # relative, as a user may give it
    out = lambda facts: SimpleNamespace(extra={"task": facts}, output=None)
    actor = {"role": "actor", "fix_code": "result = {}\n", "unseen": {"passed": 1, "total": 1}}
    assert J.score(ROW, out(actor))["score"] == 2 / 3 and runs[-1] == [1, 2, 3]   # the row's held-out seeds
    assert J.score(ROW, out({**actor, "fix_code": "import os\n"}))["score"] == 0.0
    assert J.score(ROW, out({**actor, "max_steps": 300}))["score"] == 2 / 3 and engines[-1]["max_steps"] == 300
    assert engines[-1]["benchmark"] == "robosuite"
    assert J.score(LIBERO_ROW, out(actor))["score"] == 2 / 3 and engines[-1]["benchmark"] == "libero-pro"
    work = str(tmp_path / "w")
    verified = {"role": "verifier", "fix_code": "result = {}\n", "verification": {"verdict": "verified"},
                "workspace": work}
    ledger = tmp_path / "out" / "sim-service" / "ledger.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({work: {"programs": [hashlib.sha256(b"result = {}\n").hexdigest()]}}))
    assert J.score(VERIFY, out(verified))["score"] == 3 / 5 and runs[-1] == [101, 102, 103, 104, 105]
    assert J.score(VERIFY, out({**verified, "verification": {"verdict": "void"}}))["score"] == 0.0
    third = J.score(VERIFY, out({**verified, "fix_code": "result = {'v': 3}\n"}))   # beyond its two programs
    assert third["score"] == 0.0 and "attempt budget" in third["feedback"]
    direct = {k: v for k, v in verified.items() if k != "workspace"}  # the direct actor's: it runs its own versions
    assert J.score(VERIFY, out({**direct, "fix_code": "result = {'v': 3}\n"}))["score"] == 3 / 5
    assert J.score(ROW, out({"role": "actor"}))["score"] == 0.0                 # no program: not scored


def test_prepare_refuses_held_out_seeds_a_session_could_run():
    for args in (["--eval", "1-200"], ["--unseen", "120-130"], ["--eval", "1-100", "--verify", "50-54"]):
        done = subprocess.run([sys.executable, str(EXAMPLE / "prepare.py"), *args], capture_output=True, text=True)
        assert done.returncode == 2 and "overlap" in done.stderr, args


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
