"""ASPIRE: the aspire template (the skill library, its reader and its check), the coordinator's hook into the
Trainer (next_tasks), ASPIRE's acting sessions on the cap-harness engine (actor.py: the prompt, the workspace beyond the
engine's, the facts the coordinator and the judge read), its verifier rows (rows.py), its direct-chat actor
(direct.py) and its configs on Robosuite and LIBERO. The engine is the stand-in of its own tests
(examples/cap_harness/utils/tests/cap_fakes.py): no simulator, no model, no network."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

from hermit.agent.evolver.base import StepResult
from hermit.agent.evolver.methods.aspire import actor as A
from hermit.agent.evolver.methods.aspire import direct as D
from hermit.agent.evolver.methods.aspire import evolver as E
from hermit.agent.evolver.methods.aspire import rows as Rw
from hermit.spec import check_spec

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "hermit" / "adapter" / "templates" / "aspire"
ENGINE = ROOT / "examples" / "cap_harness" / "utils"
ROBOSUITE = ROOT / "examples" / "robosuite"
LIBERO = ROOT / "examples" / "libero"


def _fakes():
    spec_ = importlib.util.spec_from_file_location("cap_fakes", ENGINE / "tests" / "cap_fakes.py")
    module = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(module)
    return module


F = _fakes()
tmp_path = F.tmp_path                       # a fixture: a directory outside /tmp, for the template check's sandbox
_load, fake_aspire, _Service, _workspace, _sim = F._load, F.fake_aspire, F._Service, F._workspace, F._sim
ROW, LIBERO_ROW = F.ROW, F.LIBERO_ROW
# ASPIRE's verifier of ROW, as rows.py derives it
VERIFY = Rw.verifier(ROW, "101-105")
# The engine as ASPIRE's configs name it (agent.params.engine), relative to the config's folder
PARAMS = {"engine": "../../cap_harness/utils/agent/task.py"}
BASE = ROBOSUITE / "aspire"


@pytest.fixture
def service(tmp_path, monkeypatch):
    """The engine's simulator service, serving ASPIRE's verifier row besides the benchmarks' rows."""
    svc = _Service(tmp_path, fake_aspire(tmp_path), rows=(VERIFY,))
    monkeypatch.setenv("ASPIRE_SIM_URL", svc.url)
    yield svc
    svc.close()


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


def seed(tmp: Path) -> Path:
    adapter = tmp / "adapter"
    shutil.copytree(TEMPLATE, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    return adapter


def spec(path) -> dict:
    r = check_spec(Path(path), TEMPLATE, deploy=False)
    return {"passed": r.passed, "issues": [{"path": i.path, "problem": i.problem} for i in r.issues]}


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


def test_actor_reads_seeds_and_the_outermost_program_fence():
    sim = D.engine_module(ENGINE / "agent" / "sim.py")           # the engine the direct actor runs its programs on
    assert sim.seeds_of("101-103 7") == [101, 102, 103, 7]
    assert sim.seeds_of([1, "3-4"]) == [1, 3, 4]
    assert D.engine_module(ENGINE / "agent" / "sim.py") is sim      # loaded once
    reply = "diagnosis\n```python\ns = '''\n```python\nx\n```\n'''\nresult = {}\n```\ntrailing"
    assert D.program_of(reply).endswith("result = {}\n")
    assert D.program_of("no program") is None
    assert D.json_of('text ```json\n{"a": 1}\n```') == {"a": 1}


def test_the_direct_actor_keeps_its_models_settings_and_an_unknown_cost_unknown(monkeypatch):
    import threading
    from hermit.agent.model import llm
    A = D
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
    A = D
    sim = D.engine_module(ENGINE / "agent" / "sim.py")

    class Engine:
        def __init__(self, root, **k):
            self.root = Path(root)

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-reference.md").write_text("# api\n")

    def explore(run):                                           # one paid call, then an outage past the retries
        run.account([SimpleNamespace(input_tokens=10, output_tokens=5, cost_usd=0.5)], "explore", "r", "", "stop")
        raise RuntimeError("model unavailable: 503")
    monkeypatch.setattr(sim, "Engine", Engine)
    monkeypatch.setattr(A._Run, "explore", explore)
    actor = A.AspireActor({"aspire_root": str(tmp_path), "scratch": str(tmp_path / "scratch"),
                           "engine": str(ENGINE / "agent" / "sim.py")}, base=tmp_path)
    with pytest.raises(ValueError, match="agent.params.engine"):            # it names the engine it runs on
        A.AspireActor({"aspire_root": str(tmp_path)}, base=tmp_path).run(dict(ROW), None)
    monkeypatch.setattr(actor, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(actor, "session", lambda adapter: None)
    out = actor.run(dict(ROW), None)
    assert out.cost_usd == 0.5 and out.tokens == 15 and "503" in out.extra["session_failed"]
    from hermit.agent.base.interface import LoginFailed
    from hermit.trainer.rollout import run_one
    monkeypatch.setattr(A._Run, "explore", lambda run: (_ for _ in ()).throw(RuntimeError("hermit_login_failed: x")))
    with pytest.raises(LoginFailed):                            # a lost sign-in stops the run, not one task
        run_one(dict(ROW), None, lambda task, adapter: actor.run(task, adapter), lambda task, out: {"score": 0})


def test_the_actor_prompt_is_the_header_the_assignment_and_aspires_template(tmp_path):
    T = A
    params = {**PARAMS, "aspire_root": str(fake_aspire(tmp_path))}
    text = T.prompt(ROW, params, BASE)
    assert "fix-loop subagent" in text and "`$TASK_DIR` is `robosuite/cube_lifting/task_0`" in text
    assert "DEV_SEEDS: 101-103" in text and "UNSEEN_SEEDS: 126" in text and "HELDOUT_COUNT: 3" in text
    assert "## What You Are\nfix-loop text" in text and "BENCHMARK: <x>" not in text   # ours replaces theirs
    # Part 2, the evolutionary search, follows the fix loop with its own page and its template after its assignment
    assert text.index("fix-loop text") < text.index("# Part 2: the evolutionary search") < text.index("search text")
    assert "`TASK_DIR` is `evosearch/robosuite/cube_lifting/task_0`" in text
    assert "# Part 2" not in T.prompt(ROW, {**params, "evosearch": False}, BASE)
    verifier = T.prompt(VERIFY, params, BASE)
    assert "skill verifier" in verifier and "Development seeds: **101-105**" in verifier
    assert "You are verifying. Seeds 51-55." in verifier and "# Part 2" not in verifier
    libero = A.prompt(LIBERO_ROW, params, BASE)                  # LIBERO's tasks on the same engine
    assert "BENCHMARK: libero-pro" in libero and "`$TASK_DIR` is `libero-pro/libero_90/task_33`" in libero
    assert "the benchmark (libero-pro)" in libero and "`docs/libero-pro.md`" in libero and "robosuite" not in libero
    assert "`TASK_DIR` is `evosearch/libero-pro/libero_90/task_33`" in libero and "DEV_SEEDS: 51-53" in libero
    with pytest.raises(ValueError, match="agent.params.engine"):
        A.prompt(ROW, {"aspire_root": params["aspire_root"]}, BASE)
    # The library is in the context file of the session's CLI: Codex's AGENTS.md, Claude Code's CLAUDE.md
    assert "section of your AGENTS.md" in text and "CLAUDE.md" not in text
    claude = A.prompt(ROW, {**params, "backend": "claude"}, BASE)
    assert "section of your CLAUDE.md" in claude and claude.replace("CLAUDE.md", "AGENTS.md") == text


def test_aspires_workspace_is_the_engines_with_its_assignment_its_search_page_and_the_proposals(tmp_path, service):
    params = {**PARAMS, "aspire_root": str(tmp_path / "ASPIRE")}
    work = _workspace(service, ROW)
    A.prepare(ROW, work, params, BASE)
    for name in ("task.md", "docs/evosearch-iteration.md", "docs/api-reference.md", "docs/task-exploration.md",
                 "scripts/scene_snapshot.py", "bin/sim", "bin/sim.json"):
        assert (work / name).is_file(), name                       # ASPIRE's pages beside the engine's workspace
    assert (work / "task.md").read_text() == A.assignment(A.settings(ROW, params, BASE), A.engine(params, BASE))
    plain = _workspace(service, ROW, "0badc0de")
    A.prepare(ROW, plain, {**params, "evosearch": False}, BASE)
    assert not (plain / "docs" / "evosearch-iteration.md").exists()
    # The verifier sees the proposals the coordinator staged in its copy of the library, and runs two programs
    verifier = _workspace(service, VERIFY)
    (verifier / "hermit" / "aspire" / "skills").mkdir(parents=True)
    (verifier / "hermit" / "aspire" / "skills" / "proposed.md").write_text(ENTRY)
    A.prepare(VERIFY, verifier, params, BASE)
    assert (verifier / "proposed_skills.md").read_text() == ENTRY
    codes = []
    for name in ("a.py", "a.py", "c.py"):                       # the same path, rewritten: a new program
        (verifier / name).write_text(f"result = {{'v': {len(codes)}}}\n")
        codes.append(_sim(verifier, "run-batch", "--seeds", "101-105", "--program", name, "--output-root", "out"))
    assert "attempt budget" not in codes[0].stderr and "attempt budget" not in codes[1].stderr
    assert codes[2].returncode == 2 and "attempt budget is 2 programs" in codes[2].stderr


def test_aspires_facts_score_the_searchs_best_and_a_verifiers_program_only_when_verified(tmp_path):
    root = fake_aspire(tmp_path)
    params = {**PARAMS, "aspire_root": str(root), "prune_media": False}
    eng = A.engine(params, BASE)
    work = tmp_path / "work"
    s = A.settings(ROW, params, BASE)
    (work / A.task_dir(s, eng)).mkdir(parents=True)
    (work / A.task_dir(s, eng) / "fix_code.py").write_text("# fix\n")
    (work / A.task_dir(s, eng) / "skill_report.json").write_text('{"graded": []}')
    (work / A.evo_dir(s)).mkdir(parents=True)
    (work / A.evo_dir(s) / "evosearch_best_code.py").write_text("# best\n")
    facts = A.collect(ROW, work, tmp_path / "log-a", params, BASE)
    assert facts["scored_program"].endswith("/evosearch_best_code.py") and facts["fix_code"] == "# best\n"
    assert facts["role"] == "actor" and facts["skill_report"] == {"graded": []} and facts["evosearch"]["best_code"]
    assert (tmp_path / "log-a" / "files" / "skill_report.json").is_file()
    plain = A.collect(ROW, work, tmp_path / "log-b", {**params, "evosearch": False}, BASE)
    assert plain["fix_code"] == "# fix\n" and "evosearch" not in plain
    # A verifier's program counts only with its own verdict `verified`; without it nothing is scored
    vwork = tmp_path / "verify"
    vwork.mkdir()
    (vwork / "fix_code.py").write_text("# v\n")
    (vwork / "verification_report.json").write_text('{"verdict": "refuted"}')
    refuted = A.collect(VERIFY, vwork, tmp_path / "log-c", params, BASE)
    assert refuted["fix_code"] is None and refuted["not_scored"] == "verifier verdict refuted"
    (vwork / "verification_report.json").write_text('{"verdict": "verified"}')
    verified = A.collect(VERIFY, vwork, tmp_path / "log-d", params, BASE)
    assert verified["fix_code"] == "# v\n" and "not_scored" not in verified and verified["role"] == "verifier"


def test_the_direct_verifiers_program_counts_only_when_verified(tmp_path, monkeypatch):
    sim = D.engine_module(ENGINE / "agent" / "sim.py")

    class Engine:
        def __init__(self, root, **k):
            self.root = Path(root)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-reference.md").write_text("# api\n")
    passes = {"n": 0}

    def debug_loop(run, max_versions):
        records = [{"task_success": i < passes["n"], "program_ok": True} for i in range(len(run.debug))]
        version = D._Version("v00", "result = {}\n", records)
        run.versions.append(version)
        return version
    monkeypatch.setattr(sim, "Engine", Engine)
    monkeypatch.setattr(D._Run, "explore", lambda run: None)
    monkeypatch.setattr(D._Run, "debug_loop", debug_loop)
    actor = D.AspireActor({"aspire_root": str(tmp_path), "scratch": str(tmp_path / "scratch"),
                           "engine": str(ENGINE / "agent" / "sim.py")}, base=tmp_path)
    monkeypatch.setattr(actor, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(actor, "session", lambda adapter: None)
    out = actor.run(dict(VERIFY), None)                           # 0 of 5: refuted, its program withheld
    assert out.extra["task"]["not_scored"] == "verifier verdict refuted" and out.extra["task"]["fix_code"] is None
    passes["n"] = 4
    out = actor.run(dict(VERIFY), None)                           # 4 of 5: verified
    assert out.extra["task"]["verification"]["verdict"] == "verified" and out.extra["task"]["fix_code"]
    assert "not_scored" not in out.extra["task"]


def test_aspires_verifiers_are_derived_from_a_benchmarks_tasks(tmp_path, monkeypatch):
    """rows.py: every task of the benchmark (its splits/all.txt, in that order) gets its verifier beside it; the
    benchmark's tasks, reruns and splits are copied as they are."""
    P = _load("robosuite_prepare", ROBOSUITE / "utils" / "benchmark" / "prepare.py")
    monkeypatch.setattr(P, "HERE", tmp_path / "bench")
    monkeypatch.setattr(sys, "argv", ["prepare.py", "--preset", "smoke"])
    (tmp_path / "bench").mkdir()
    P.main()
    out = tmp_path / "aspire"
    writer = ENGINE / "benchmark" / "tasks.py"                 # the engine's task writer
    Rw.main(["--benchmark", str(tmp_path / "bench"), "--out", str(out), "--verify-seeds", "101-105",
             "--engine", str(writer)])
    Fd = _load("cap_fields", ENGINE / "agent" / "fields.py")
    found = Fd.task_rows(out / "tasks")
    task = {"id": "robosuite-cube_stack", "suite": "cube_stack", "task_id": 0}
    assert json.dumps(found["verify-robosuite-cube_stack"]) == json.dumps(
        {**task, "id": "verify-robosuite-cube_stack", "role": "verifier", "debug_seeds": "101-105",
         "eval_seeds": "101-105", "score_label": "verifier", "max_programs": 2, "max_versions": 2,
         "search": "debug", "findings": False})                    # its keys in order: the row the service reads
    bench = Fd.task_rows(tmp_path / "bench" / "tasks")
    assert {k: v for k, v in found.items() if not k.startswith("verify-")} == bench
    assert len(found) == 21 and (out / "tasks" / "verify-robosuite-cube_stack" / "instruction.md").is_file()
    order = (tmp_path / "bench" / "splits" / "all.txt").read_text().split()
    assert (out / "splits" / "verify.txt").read_text().split() == [f"verify-{i}" for i in order]
    for split in (tmp_path / "bench" / "splits").iterdir():
        assert (out / "splits" / split.name).read_text() == split.read_text()
    Rw.write(tmp_path / "bench", out, "101-105", writer)        # again: rewritten whole, the same
    assert Fd.task_rows(out / "tasks") == found
    with pytest.raises(ValueError, match="overlap the held-out seeds"):
        Rw.write(tmp_path / "bench", out, "1-5", writer)


def test_the_aspire_configs_read_its_verifiers_and_name_its_session_and_the_engine(tmp_path, monkeypatch):
    """Every ASPIRE config on Robosuite and LIBERO: its tasks are the benchmark's with ASPIRE's verifiers (tasks.sh),
    the coordinator's are the verifier rows, the test split the reruns of what it trained on, and the session's task
    functions are ASPIRE's over the engine's."""
    from hermit.config import Config
    from hermit.task import Benchmark
    from hermit.agent.acting import workspace as cli
    configs = sorted((ROBOSUITE / "aspire").glob("*.yaml")) + sorted((LIBERO / "aspire").glob("*.yaml"))
    assert len(configs) == 14
    prepared = {}
    for bench, seeds in ((ROBOSUITE, "101-105"), (LIBERO, "51-55")):
        P = _load(f"prepare_{bench.name}", bench / "utils" / "benchmark" / "prepare.py")
        monkeypatch.setattr(P, "HERE", tmp_path / bench.name)
        monkeypatch.setattr(sys, "argv", ["prepare.py", "--preset", "smoke"])
        (tmp_path / bench.name).mkdir()
        P.main()
        hook = (bench / "aspire" / "tasks.sh").read_text()
        assert f"--verify-seeds {seeds}" in hook and "--engine ../../cap_harness/utils/benchmark/tasks.py" in hook
        Rw.write(tmp_path / bench.name, tmp_path / bench.name / "aspire", seeds, ENGINE / "benchmark" / "tasks.py")
        prepared[bench] = tmp_path / bench.name / "aspire"
    for path in configs:
        config = Config.load(path)
        data, params = config.data["data"], config.data["agent"]["params"]
        here = prepared[path.parents[1]]
        assert config.path(data["tasks"]) == (path.parent / "tasks").resolve(), path
        for key in ("train", "val", "test"):
            assert config.path(data[key]).parent == (path.parent / "splits").resolve(), (path, key)
            monkeypatch.setitem(data, key, str(here / "splits" / Path(data[key]).name))
        monkeypatch.setitem(data, "tasks", str(here / "tasks"))
        tasks = Benchmark.from_config(config)
        split = lambda key: (here / "splits" / Path(data[key]).name).read_text().split()
        assert split("val") == [i for i in (here / "splits" / "verify.txt").read_text().split()], path
        assert split("test") == [f"rerun-{i}" for i in split("train")], path
        assert all(tasks.tasks[i].given()["role"] == "verifier" for i in split("val")), path
        if "_direct" in path.name:
            assert config.data["agent"]["name"] == "hermit.agent.evolver.methods.aspire.direct:AspireActor", path
            assert config.path(params["engine"]) == (ENGINE / "agent" / "sim.py").resolve(), path
            continue
        assert config.path(params["engine"]) == (ENGINE / "agent" / "task.py").resolve(), path
        for name in ("prompt", "prepare", "answer", "collect"):
            assert cli._task_function(params[name], config.base) is getattr(A, name), (path, name)
        read = [str(p) for p in (config.data.get("environment") or {}).get("read") or []]
        for name in A.ASPIRE_FILES:                               # its templates and the search's page, declared
            assert f"$ASPIRE_ROOT/{name}" in read, (path, name)
