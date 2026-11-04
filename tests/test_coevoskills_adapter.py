"""CoEvoSkills Adapter: the template (packages as the host's native skills), the packaging of an exported package, the
Evolver's setup and step (one upstream loop per question, partial failure, `keep`, container cleanup), the whole run
through the Trainer with a scripted worker that calls the Trainer's tools, the oracle, the upstream seams (the model on
the framework's tool server, the containers, the worker), the OfficeQA preparation and the example's configs. The
upstream loop itself needs Docker and a model; the example's smoke runs it."""
import asyncio
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest
import yaml

from hermit.agent.evolver.methods.coevoskills_adapter import packaging
from hermit.agent.evolver.methods.coevoskills_adapter.client import call_tool
from hermit.config import Config
from hermit.spec import check_spec

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "examples/officeqa/coevoskills_adapter"
TEMPLATE = ROOT / "hermit/adapter/templates/coevoskills_adapter"
METHOD = ROOT / "hermit/agent/evolver/methods/coevoskills_adapter"
DEFAULTS = yaml.safe_load((METHOD / "evolver.yaml").read_text())["params"]
PATH_MAP = {"/root/docs": "docs", "/root/answer.txt": "skill-answer.txt"}


def skill(name: str, text: str = "") -> str:
    return f'---\nname: {name}\ndescription: "Reusable workflow"\n---\nRead runtime inputs and compute.\n{text}'


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


def evolver_module():
    """The Evolver module as the Trainer loads it (hermit.agent.evolver.loader)."""
    from hermit.agent.evolver.loader import find, load_class
    load_class(find("coevoskills_adapter"))
    return sys.modules["_evolver_coevoskills_adapter_evolver"]


def exported(root: Path, text: str = "", name: str = "evo-example") -> Path:
    """What the loop exports: <root>/evo-example/{SKILL.md, scripts/run.py}; returns root."""
    (root / name / "scripts").mkdir(parents=True)
    (root / name / "SKILL.md").write_text(skill(name, text))
    (root / name / "scripts" / "run.py").write_text("print('run')\n")
    return root


# ---------- the template and the packaging ----------

def test_the_template_root_passes_and_the_host_installs_the_packages_as_skills(tmp_path):
    from hermit.agent.acting.workspace import deploy
    assert check_spec(TEMPLATE, TEMPLATE).passed
    adapter = tmp_path / "adapter"
    shutil.copytree(TEMPLATE, adapter)
    for tid in ("UID0017", "UID0021"):
        packaging.project(exported(tmp_path / tid), adapter / "skills", tid, PATH_MAP)
    assert sorted(p.name for p in (adapter / "skills").iterdir()) == ["uid0017-evo-example", "uid0021-evo-example"]
    assert check_spec(adapter, TEMPLATE).passed
    work = tmp_path / "work"
    deploy(adapter, work, "codex", context="context", serve_tools=False)
    assert (work / ".codex/skills/uid0017-evo-example/SKILL.md").read_text().startswith(
        "---\nname: uid0017-evo-example\n")                   # Codex finds it by its own skill discovery


@pytest.mark.parametrize("breakage", ["name differs from directory", "stray file", "nested package"])
def test_the_template_check_refuses_what_is_no_skill(tmp_path, breakage):
    adapter = tmp_path / "adapter"
    shutil.copytree(TEMPLATE, adapter)
    packaging.project(exported(tmp_path / "x"), adapter / "skills", "a", PATH_MAP)
    if breakage == "name differs from directory":
        (adapter / "skills/a-evo-example/SKILL.md").write_text(skill("evo-example"))
    elif breakage == "stray file":
        (adapter / "skills/notes.txt").write_text("x")
    else:
        shutil.copytree(adapter / "skills/a-evo-example", adapter / "skills/b/evo-example")
    assert not check_spec(adapter, TEMPLATE).passed


def test_packages_get_their_tasks_name(tmp_path):
    assert packaging.skill_name("UID_0017.x", "evo-a") == "uid-0017-x-evo-a"
    assert len(packaging.skill_name("q", "evo-" + "a" * 80)) == 64
    skills = tmp_path / "skills"
    record = packaging.project(exported(exported(tmp_path / "s", name="evo-b")), skills, "q1", PATH_MAP)
    assert record["skills"] == ["q1-evo-b", "q1-evo-example"]
    crlf = exported(tmp_path / "w")                       # Windows line endings: renamed all the same
    (crlf / "evo-example/SKILL.md").write_bytes(skill("evo-example").replace("\n", "\r\n").encode())
    packaging.project(crlf, tmp_path / "crlf", "q", PATH_MAP)
    assert (tmp_path / "crlf/q-evo-example/SKILL.md").read_text().startswith("---\nname: q-evo-example\n")
    with pytest.raises(ValueError, match="same skill name"):
        packaging.project(exported(exported(tmp_path / "u", name="evo-" + "c" * 70), name="evo-" + "c" * 71),
                          tmp_path / "other", "q", PATH_MAP)


def test_an_installed_package_runs_its_documented_command_from_the_adapter_folder(tmp_path):
    """The container's paths in a package (its own /app/environment/skills, the task's /root/docs) are rewritten to the
    deployment's convention, which the deployed context states: relative to the adapter's folder, from which the agent
    runs a command a skill gives."""
    from hermit.agent.acting.workspace import deploy
    source = tmp_path / "exported/evo-example"
    (source / "scripts").mkdir(parents=True)
    example = ('import sys\n'
               'sys.path.insert(0, "/app/environment/skills/evo-example/scripts")\n'
               'from utils import solve\n'
               'solve("/root/docs", "/root/answer.txt")\n')
    original = skill("evo-example", "```python\n" + example + "```\n")
    (source / "SKILL.md").write_text(original)
    (source / "scripts/utils.py").write_text(
        "from pathlib import Path\n"
        "def solve(docs, output):\n"
        "    Path(output).write_text((Path(docs)/\"input.txt\").read_text().upper())\n")
    adapter = tmp_path / "adapter"
    shutil.copytree(TEMPLATE, adapter)
    record = packaging.project(source.parent, adapter / "skills", "UID0017", PATH_MAP)
    work = tmp_path / "work"
    deploy(adapter, work, "codex", context="context", serve_tools=False)
    from hermit.hosts.inputs import folder_note
    folder = work / "hermit" / packaging.TEMPLATE
    assert f"cd hermit/{packaging.TEMPLATE} && ..." in folder_note(work, {"id": packaging.TEMPLATE})
    (work / "docs").mkdir()
    (work / "docs/input.txt").write_text("runtime input")
    command = (work / ".codex/skills/uid0017-evo-example/SKILL.md").read_text().split("```python\n")[1].split("```")[0]
    assert "skills/uid0017-evo-example/scripts" in command and "../../docs" in command
    subprocess.run([sys.executable, "-c", command], cwd=folder, check=True)
    assert (work / "skill-answer.txt").read_text() == "RUNTIME INPUT"
    assert (source / "SKILL.md").read_text() == original
    assert sum(f["path_replacements"] for f in record["files"]) == 4            # three paths and the name
    (source / "scripts/utils.py").write_text('value="/root/docs_backup"\nnumber=478\n')   # similar names untouched
    packaging.project(source.parent, tmp_path / "other", "UID0017", PATH_MAP)
    assert (tmp_path / "other/uid0017-evo-example/scripts/utils.py").read_text() == \
        'value="/root/docs_backup"\nnumber=478\n'


# ---------- the setup and the step ----------

def context(tmp_path, ids, params=None, agent=True):
    from types import SimpleNamespace
    from hermit.agent.evolver.base import Access, Progress, StepContext, Workspace
    names = ("adapter", "learning_material", "task_info", "history", "state", "scratch", "output")
    for name in names:
        (tmp_path / name).mkdir(parents=True)
    shutil.copytree(TEMPLATE, tmp_path / "adapter", dirs_exist_ok=True)
    ws = Workspace(**{name: tmp_path / name for name in names})
    tasks = ws.learning_material / "current/tasks"
    tasks.mkdir(parents=True)
    for tid in ids:
        (tasks / f"{tid}.json").write_text(json.dumps({"id": tid, "input": {"question": "public task"},
                                                       "judge": {"feedback": "PRIVATE-GOLD"}, "output": "PRIVATE-OUT"}))
    (tmp_path / "packages/q").mkdir(parents=True)
    (tmp_path / "packages/q/task.toml").write_text("")
    (tmp_path / "io.py").write_text("def deliver(output):\n    return {'/root/answer.txt': str(output)}\n")
    model = SimpleNamespace(model=SimpleNamespace(model="the-runs-model")) if agent else None
    params = {**DEFAULTS, "workers": 2, "task_packages": str(tmp_path / "packages"), "deliver": "io.py:deliver",
              "path_map": PATH_MAP, **(params or {})}
    return StepContext(workspace=ws, access=Access(), tools={}, params=params, progress=Progress(1, 1, False),
                       base=tmp_path, tool_url="http://127.0.0.1:9", agent=model)


def scripted_worker(outcomes, seen):
    """A stand-in for the worker process: `outcomes[tid]` is (exit code, upstream's final `passed`)."""
    def run(command, env, ctx, log, cwd):
        raw = Path(command[-1]).read_text()
        assert "PRIVATE-" not in raw                                  # no judge feedback, output or trace reaches it
        request = json.loads(raw)
        seen.append(request)
        code, passed = outcomes[request["task_id"]]
        out = Path(request["output"])
        skills = exported(out / "task/environment/skills", request["task_id"])
        if code == 0:
            (out / "result.json").write_text(json.dumps({"skills": str(skills), "passed": passed}))
        return code
    return run


@pytest.fixture
def no_docker(monkeypatch):
    E = evolver_module()
    monkeypatch.setattr(E, "remove_containers", lambda trial, task_dir: None)
    return E


def test_a_step_runs_each_question_and_installs_its_packages(tmp_path, monkeypatch, no_docker):
    E = no_docker
    ctx = context(tmp_path, ["q1", "q2", "q3"])
    seen = []
    monkeypatch.setattr(E, "_run", scripted_worker({"q1": (0, True), "q2": (1, None), "q3": (0, False)}, seen))
    original = (ctx.workspace.adapter / "ADAPTER.md").read_bytes()
    result = E.CoEvoSkillsAdapterEvolver().step(ctx)
    assert result.status == "done" and "2/3 questions" in result.summary and "loop failed on q2" in result.summary
    skills = ctx.workspace.adapter / "skills"
    assert sorted(p.name for p in skills.iterdir()) == ["q1-evo-example", "q3-evo-example"]   # keep: all
    assert (skills / "q1-evo-example/SKILL.md").read_text().endswith("q1")
    assert (ctx.workspace.adapter / "ADAPTER.md").read_bytes() == original
    assert check_spec(ctx.workspace.adapter, TEMPLATE).passed
    assert {r["task_id"] for r in seen} == {"q1", "q2", "q3"} and all(r["path_map"] == PATH_MAP for r in seen)
    assert all(r["model_name"] == "the-runs-model" and r["deliver"] == "io.py:deliver" for r in seen)
    assert len({r["trial_name"] for r in seen}) == 3 and len({r["task_dir"] for r in seen}) == 3
    assert json.loads((ctx.workspace.state / "installed.json").read_text()) == {"q1": ["q1-evo-example"],
                                                                              "q3": ["q3-evo-example"]}
    outcome = json.loads((ctx.workspace.output / "questions.json").read_text())[0]
    assert outcome["skills"] == ["q1-evo-example"] and not Path(outcome["exported"]).is_absolute()
    seed = Path(seen[0]["seed"])
    assert seed.is_dir() and not (seed / "skills").exists()                   # every probe starts without packages
    # A later step on the same question replaces its packages and leaves the others'.
    monkeypatch.setattr(E, "_run", scripted_worker({"q1": (0, True)}, []))
    shutil.rmtree(ctx.workspace.learning_material / "current/tasks")
    (ctx.workspace.learning_material / "current/tasks").mkdir()
    (ctx.workspace.learning_material / "current/tasks/q1.json").write_text(json.dumps({"id": "q1"}))
    shutil.rmtree(ctx.workspace.output)
    ctx.workspace.output.mkdir()
    assert E.CoEvoSkillsAdapterEvolver().step(ctx).status == "done"
    assert sorted(p.name for p in skills.iterdir()) == ["q1-evo-example", "q3-evo-example"]


def test_keep_passed_installs_only_what_upstreams_final_verdict_passed(tmp_path, monkeypatch, no_docker):
    E = no_docker
    ctx = context(tmp_path, ["q1", "q2", "q3"], {"keep": "passed"})
    monkeypatch.setattr(E, "_run", scripted_worker({"q1": (0, True), "q2": (0, False), "q3": (0, None)}, []))
    assert E.CoEvoSkillsAdapterEvolver().step(ctx).status == "done"
    assert [p.name for p in (ctx.workspace.adapter / "skills").iterdir()] == ["q1-evo-example"]


def test_a_package_that_cannot_be_installed_leaves_the_steps_others(tmp_path, monkeypatch, no_docker):
    """A package whose skill name another question's skill already has is refused; the step's other questions are
    installed, and a loop that ended in an error but left a valid package is installed with the error recorded."""
    E = no_docker
    ctx = context(tmp_path, ["q1", "q2"])
    (ctx.workspace.state / "installed.json").write_text(json.dumps({"other": ["q1-evo-example"]}))
    run = scripted_worker({"q1": (0, True), "q2": (0, None)}, [])

    def worker(command, env, c, log, cwd):
        code = run(command, env, c, log, cwd)
        request = json.loads(Path(command[-1]).read_text())
        if request["task_id"] == "q2":                       # upstream raised after leaving its best snapshot
            result = Path(request["output"]) / "result.json"
            result.write_text(json.dumps({**json.loads(result.read_text()), "loop_error": "AgentTimeoutError: ..."}))
        return code
    monkeypatch.setattr(E, "_run", worker)
    result = E.CoEvoSkillsAdapterEvolver().step(ctx)
    assert result.status == "done" and "not installable: q1" in result.summary
    assert [p.name for p in (ctx.workspace.adapter / "skills").iterdir()] == ["q2-evo-example"]
    q1, q2 = json.loads((ctx.workspace.output / "questions.json").read_text())
    assert "another question's skill" in q1["install_error"] and not q1["installed"]
    assert q2["installed"] and q2["loop_error"].startswith("AgentTimeoutError")


def test_a_file_that_is_not_utf8_text_is_copied_as_it_is(tmp_path):
    source = exported(tmp_path / "s")
    (source / "evo-example/notes.txt").write_bytes("caf\xe9 /root/docs".encode("latin-1"))
    packaging.project(source, tmp_path / "skills", "q", PATH_MAP)
    assert (tmp_path / "skills/q-evo-example/notes.txt").read_bytes() == "caf\xe9 /root/docs".encode("latin-1")


def test_keep_passed_with_nothing_passed_is_done_and_changes_nothing(tmp_path, monkeypatch, no_docker):
    E = no_docker
    ctx = context(tmp_path, ["q1"], {"keep": "passed"})
    monkeypatch.setattr(E, "_run", scripted_worker({"q1": (0, False)}, []))
    assert E.CoEvoSkillsAdapterEvolver().step(ctx).status == "done"
    assert not (ctx.workspace.adapter / "skills").exists() or not any((ctx.workspace.adapter / "skills").iterdir())


def test_an_install_the_template_check_refuses_puts_the_earlier_skills_back(tmp_path, monkeypatch, no_docker):
    """All or nothing: when the run's check_spec finds a problem in the new packages, the question's earlier skills
    are back in place and the record is unchanged."""
    E = no_docker
    ctx = context(tmp_path, ["q1"])
    monkeypatch.setattr(E, "_run", scripted_worker({"q1": (0, True)}, []))
    assert E.CoEvoSkillsAdapterEvolver().step(ctx).status == "done"
    first = (ctx.workspace.adapter / "skills/q1-evo-example/SKILL.md").read_text()

    def worker(command, env, c, log, cwd):
        request = json.loads(Path(command[-1]).read_text())
        out = Path(request["output"])
        skills = exported(out / "task/environment/skills", "second", name="evo-other")
        (out / "result.json").write_text(json.dumps({"skills": str(skills), "passed": True}))
        return 0
    monkeypatch.setattr(E, "_run", worker)
    refused = {"passed": False, "issues": [{"path": "skills/q1-evo-other/SKILL.md", "problem": "bad"}]}
    shutil.rmtree(ctx.workspace.output)
    ctx.workspace.output.mkdir()
    from dataclasses import replace
    result = E.CoEvoSkillsAdapterEvolver().step(replace(ctx, tools={"check_spec": lambda path: refused}))
    assert result.status == "failed" and "not installable: q1" in result.summary
    assert [p.name for p in (ctx.workspace.adapter / "skills").iterdir()] == ["q1-evo-example"]
    assert (ctx.workspace.adapter / "skills/q1-evo-example/SKILL.md").read_text() == first
    assert json.loads((ctx.workspace.state / "installed.json").read_text()) == {"q1": ["q1-evo-example"]}


def test_names_harbor_derives_from_a_task_id_have_no_dot(tmp_path, monkeypatch, no_docker):
    E = no_docker
    seen = []
    monkeypatch.setattr(E, "_run", scripted_worker({"q.1": (0, True)}, seen))
    E.CoEvoSkillsAdapterEvolver().step(context(tmp_path, ["q.1"]))
    assert "." not in seen[0]["trial_name"] and "." not in seen[0]["task_dir"]


def test_a_step_without_any_package_fails(tmp_path, monkeypatch, no_docker):
    E = no_docker
    monkeypatch.setattr(E, "_run", scripted_worker({"q1": (1, None)}, []))
    assert E.CoEvoSkillsAdapterEvolver().step(context(tmp_path, ["q1"])).status == "failed"


def test_the_questions_of_a_step_run_at_the_same_time(tmp_path, monkeypatch, no_docker):
    E = no_docker
    barrier = threading.Barrier(2, timeout=10)                # both workers must be running for either to finish
    inner = scripted_worker({"q1": (0, True), "q2": (0, True)}, [])

    def run(*args):
        barrier.wait()
        return inner(*args)
    monkeypatch.setattr(E, "_run", run)
    assert E.CoEvoSkillsAdapterEvolver().step(context(tmp_path, ["q1", "q2"])).status == "done"


def test_a_workers_leftover_containers_and_image_are_removed(tmp_path, monkeypatch):
    """Harbor names a container's Compose project after its session and the task's image after its directory; the
    Evolver removes a finished worker's (its trial, its oracle checks, its image) and nobody else's: not another run's
    oracle check of the same question, not question q1-2's."""
    E = evolver_module()
    log = tmp_path / "docker.log"
    fake = tmp_path / "bin/docker"
    fake.parent.mkdir()
    fake.write_text(f"#!{sys.executable}\nimport sys\nargs = sys.argv[1:]\n"
                    f"open({str(log)!r}, 'a').write(' '.join(args) + '\\n')\n"
                    "if args[0] == 'ps':\n"
                    "    print('c1 coevoskills-q1-abcd1234\\nc2 oracle-q1-abcd1234-9f\\nc3 oracle-q1-ffff0000-1'\n"
                    "          '\\nc4 oracle-q1-2-abcd1234-7\\nc5 someone-else\\nc6')\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    E.remove_containers("coevoskills-q1-abcd1234", "q1-abcd1234")
    assert log.read_text().splitlines()[1:] == ["rm -f -v c1 c2", "image rm -f hb__q1-abcd1234"]


def test_the_setup_refuses_what_a_step_would_fail_on(tmp_path, monkeypatch):
    E = evolver_module()
    monkeypatch.setattr(E, "verify", lambda root: root)
    E.CoEvoSkillsAdapterEvolver().setup(context(tmp_path / "ok", []))
    cases = [({"keep": "best"}, "keep"), ({"workers": 0}, "workers"), ({"path_map": ["x"]}, "path_map"),
             ({"task_packages": str(tmp_path / "none")}, "no task packages"),
             ({"task_packages": str(tmp_path)}, "no task packages"), ({"deliver": None}, "deliver")]
    for i, (params, message) in enumerate(cases):
        with pytest.raises(ValueError, match=message):
            E.CoEvoSkillsAdapterEvolver().setup(context(tmp_path / f"case{i}", [], params))
    with pytest.raises(ValueError, match="set `llm`"):
        E.CoEvoSkillsAdapterEvolver().setup(context(tmp_path / "nollm", [], agent=False))

    def no_checkout(root):
        raise RuntimeError(f"no CoEvoSkills checkout at {root}")
    monkeypatch.setattr(E, "verify", no_checkout)
    with pytest.raises(RuntimeError, match="no CoEvoSkills checkout"):
        E.CoEvoSkillsAdapterEvolver().setup(context(tmp_path / "noroot", []))


def test_verify_names_a_missing_or_wrong_checkout_and_harbor(tmp_path, monkeypatch):
    from hermit.agent.evolver.methods.coevoskills_adapter import upstream
    from hermit.agent.evolver.methods.coevoskills_adapter.upstream import verify
    pytest.importorskip("harbor")
    monkeypatch.setattr(upstream, "harbor_commit", lambda: "0" * 40)
    with pytest.raises(RuntimeError, match="upstream pins"):
        verify(tmp_path)
    monkeypatch.setattr(upstream, "harbor_commit", lambda: upstream.HARBOR_COMMIT)
    with pytest.raises(RuntimeError, match="no CoEvoSkills checkout"):
        verify(tmp_path)
    if os.environ.get("COEVOSKILLS_ROOT"):
        assert verify(os.environ["COEVOSKILLS_ROOT"]) == Path(os.environ["COEVOSKILLS_ROOT"]).resolve()


# ---------- the whole run, through the Trainer ----------

PIPELINE = '''
from pathlib import Path
from hermit.task import RunOutput

def run(task, adapter):
    texts = [p.read_text() for p in Path(adapter).glob("skills/*/SKILL.md")] if adapter else []
    answer = str(task["a"] + task["b"]) if any("add the numbers" in t for t in texts) else "?"
    log = Path(__file__).parent / ".logs" / task["id"]
    log.mkdir(parents=True, exist_ok=True)
    (log / "answer.txt").write_text(answer)
    return RunOutput(output=answer, log=log, cost_usd=0.0, wall_s=0.1, tokens=10)
'''
JUDGE = '''
def score(task, output):
    return {"score": float(output.output == str(task["a"] + task["b"])), "feedback": "PRIVATE"}
'''


class ScriptedModel:
    model = "scripted"

    def __init__(self):
        self.calls = []

    def chat(self, messages, **kwargs):
        from hermit.agent.evolver.base import Usage
        self.calls.append((messages, kwargs))
        return {"content": "Always add the numbers.", "finish_reason": "stop"}, Usage("scripted", 10, 0, 5, 1, 0.0)


def test_a_run_through_the_trainer_installs_and_ships_the_packages(tmp_path, monkeypatch):
    """`hermit train` as the example runs it: each question's worker (scripted here, in process) asks the run's `llm`
    for a Skill, checks the candidate with `check_spec` and runs it with `run_agent`, through the Trainer's tool
    endpoint; the packages are committed and the final adapter, evaluated frozen, answers the test questions."""
    import hermit.agent.evolver.agent
    import hermit.agent.evolver.methods.coevoskills_adapter.upstream as upstream
    from hermit.evaluate import evaluate
    from hermit.trainer.trainer import Trainer
    rows = [{"id": f"t{i}", "a": i, "b": 1} for i in range(6)]
    (tmp_path / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for name, ids in (("train", "t0 t1 t2 t3"), ("val", "t4"), ("test", "t5")):
        (tmp_path / f"{name}.txt").write_text(ids.replace(" ", "\n") + "\n")
    (tmp_path / "task-info").mkdir()
    (tmp_path / "task-info/README.md").write_text("Add two numbers.\n")
    (tmp_path / "pipeline.py").write_text(PIPELINE)
    (tmp_path / "judge.py").write_text(JUDGE)
    (tmp_path / "packages/q").mkdir(parents=True)
    (tmp_path / "packages/q/task.toml").write_text("")
    (tmp_path / "io.py").write_text("def deliver(output):\n    return {'/root/answer.txt': str(output)}\n")
    config = {"data": {"tasks": "tasks.jsonl", "train": "train.txt", "val": "val.txt", "test": "test.txt",
                       "step_size": 2, "shuffle": False},
              "task_info": "task-info", "agent": {"name": "pipeline.py:run", "concurrency": 2},
              "judge": {"path": "judge.py"}, "template": str(TEMPLATE),
              "evolver": {"name": "coevoskills_adapter",
                          "params": {"workers": 2, "task_packages": "packages", "deliver": "io.py:deliver"}},
              "sandbox": {"enabled": False, "network": "open"},
              "trainer": {"run_name": "toy", "output_dir": "runs", "epochs": 1, "budget": {"hours": 1}}}
    (tmp_path / "run.yaml").write_text(yaml.safe_dump(config))

    def worker(command, env, ctx, log, cwd):
        request = json.loads(Path(command[-1]).read_text())
        url, tid = request["tool_url"], request["task_id"]
        ask = {"messages": [{"role": "user", "content": f"Write a Skill for {tid}."}], "max_tokens": 16384}
        text = call_tool(url, "llm", ask)["content"]
        out = Path(request["output"])
        skills = exported(out / "task/environment/skills", text)
        candidate = Path(request["projection_root"]) / "candidate"
        shutil.copytree(request["seed"], candidate)
        packaging.project(skills, candidate / "skills", tid, request["path_map"])
        check = call_tool(url, "check_spec", {"path": str(candidate)})
        assert check["passed"], check
        run = call_tool(url, "run_agent", {"task_ids": [tid], "adapter": str(candidate)})["results"][0]
        (out / "result.json").write_text(json.dumps({"skills": str(skills), "passed": run["judge"]["score"] == 1}))
        return 0
    monkeypatch.setattr(hermit.agent.evolver.agent, "_run", worker)   # the Trainer loads the Evolver module afresh
    monkeypatch.setattr(upstream, "verify", lambda root: root)          # no upstream checkout in this test
    trainer = Trainer(Config.load(tmp_path / "run.yaml"), log=lambda *_: None)
    trainer.llm = model = ScriptedModel()
    trainer.train()
    run_dir = tmp_path / "runs" / "toy"
    steps = [json.loads(l) for l in (run_dir / "steps.jsonl").read_text().splitlines()]
    assert [s["status"] for s in steps] == ["done", "done"] and all(s["changed"] for s in steps)
    assert [s["rollout"]["mean_score"] for s in steps] == [0.0, 1.0]           # step 2's rollouts see step 1's
    assert len(model.calls) == 4 and all(kw.get("max_tokens") == 16384 for _, kw in model.calls)
    assert all(s["evolver"]["metered_tool_usage"] for s in steps)
    final = run_dir / "checkout" / "skills"
    assert sorted(p.name for p in final.iterdir()) == [f"t{i}-evo-example" for i in range(4)]
    assert evaluate(Config.load(tmp_path / "run.yaml"), "final", log=lambda *_: None)["mean_score"] == 1.0
    assert evaluate(Config.load(tmp_path / "run.yaml"), "root", log=lambda *_: None)["mean_score"] == 0.0


# ---------- the oracle ----------

def oracle_module(tmp_path, monkeypatch, tool, deliver=None):
    pytest.importorskip("harbor")
    from hermit.agent.evolver.methods.coevoskills_adapter import oracle
    seed = tmp_path / "seed"
    shutil.copytree(TEMPLATE, seed)
    monkeypatch.setattr(oracle, "CONFIG", {"seed": str(seed), "projection_root": str(tmp_path / "probes"),
                                           "task_id": "a", "tool_url": "http://tools", "path_map": PATH_MAP,
                                           "deliver": deliver})
    monkeypatch.setattr(oracle, "call_tool", tool)
    return oracle


def test_the_oracle_runs_the_acting_agent_and_returns_no_judge_to_the_loop(tmp_path, monkeypatch):
    calls = []

    def tool(url, name, payload):
        calls.append((url, name, payload))
        if name == "check_spec":
            return {"passed": True}
        return {"results": [{"status": "ok", "output": {"answer": "prediction"}, "judge": {"score": 0}}]}
    oracle = oracle_module(tmp_path, monkeypatch, tool)
    assert oracle.probe(exported(tmp_path / "candidate"))["output"] == {"answer": "prediction"}
    assert [c[1] for c in calls] == ["check_spec", "run_agent"] and calls[1][0] == "http://tools"
    assert calls[1][2]["task_ids"] == ["a"] and "judge" not in calls[1][2]
    projected = Path(calls[1][2]["adapter"])
    assert (projected / "skills/a-evo-example/SKILL.md").read_text() == skill("a-evo-example")


def test_the_oracle_hands_the_answer_to_the_hidden_test_as_deliver_says(tmp_path, monkeypatch):
    """The answer goes where `deliver` says, by Harbor's upload; a hand-over that fails is an error, never an empty
    answer the hidden test would fail."""
    from types import SimpleNamespace

    def tool(url, name, payload):
        return {"passed": True} if name == "check_spec" else \
            {"results": [{"status": "ok", "output": {"answer": "4'2"}, "judge": {"score": 0}}]}
    io = module(HERE / "oracle_io.py", "_coevoskills_adapter_oracle_io")
    oracle = oracle_module(tmp_path, monkeypatch, tool, io.deliver)
    uploads, codes = {}, [0]

    class Environment:
        async def download_dir(self, source, target):
            exported(Path(target))

        async def exec(self, command, timeout_sec):
            return SimpleNamespace(return_code=codes[0])

        async def upload_file(self, source, target):
            uploads[target] = Path(source).read_text()
    agent = object.__new__(oracle.FixedActorOracle)
    agent.logs_dir = tmp_path / "logs"
    asyncio.run(agent.run("instruction", Environment(), None))
    assert uploads == {"/root/answer.txt": "<answer>4'2</answer>"}
    codes[0] = 1
    with pytest.raises(RuntimeError, match="refused the folder"):
        asyncio.run(agent.run("instruction", Environment(), None))
    codes[0] = 0
    oracle.CONFIG["deliver"] = lambda output: {"relative/path": "x"}
    with pytest.raises(ValueError, match="absolute container path"):
        asyncio.run(agent.run("instruction", Environment(), None))


def test_a_frozen_candidate_can_be_projected_again(tmp_path, monkeypatch):
    oracle = oracle_module(tmp_path, monkeypatch, None)
    skills = exported(tmp_path / "source")
    (skills / "evo-example/SKILL.md").chmod(0o444)
    (skills / "evo-example").chmod(0o555)
    for _ in range(2):
        projected = oracle.projection(skills)
        assert (projected / "skills/a-evo-example/SKILL.md").is_file()
        shutil.rmtree(projected)
    (skills / "evo-example").chmod(0o755)


# ---------- the upstream seams ----------

def test_the_upstream_roles_call_the_runs_llm_on_the_frameworks_tool_server():
    pytest.importorskip("harbor")
    if not os.environ.get("COEVOSKILLS_ROOT"):
        pytest.skip("set COEVOSKILLS_ROOT to the pinned checkout: upstream's agents import its scripts/")
    from hermit.agent.evolver.methods.coevoskills_adapter.upstream import checkout
    checkout(os.environ["COEVOSKILLS_ROOT"])                  # as the worker does before it builds the roles
    from libs.terminus_agent.llms.base_llm import OutputLengthExceededError
    from libs.terminus_agent.llms.chat import Chat
    from hermit.agent.evolver.methods.coevoskills_adapter import model
    from hermit.tools import FunctionTool, ToolServer
    replies = iter([{"content": "first", "finish_reason": "stop", "usage": {"input_tokens": 3}},
                    {"content": "second", "finish_reason": "stop"},
                    {"content": "cut", "finish_reason": "length"},
                    {"content": " ", "finish_reason": "stop"}])
    seen = []

    def llm(messages, max_tokens=None, temperature=None):          # the Trainer's `llm` tool's signature
        seen.append({"messages": messages, "max_tokens": max_tokens, "temperature": temperature})
        return next(replies)
    with ToolServer({"llm": FunctionTool("llm", "the run's model", llm)}) as server:
        model.install(server.url)
        from libs.terminus_agent.agents.terminus_2 import harbor_terminus_2_skills, terminus_2
        assert harbor_terminus_2_skills.LiteLLM is model.ToolLLM and terminus_2.LiteLLM is model.ToolLLM
        role = model.ToolLLM(model_name="the-runs-model", temperature=0.7)
        chat = Chat(role)                                     # upstream's history keeping
        assert chat.chat("start", max_tokens=16384) == "first" and chat.chat("go on") == "second"
        assert seen[0] == {"messages": [{"role": "user", "content": "start"}], "max_tokens": 16384,
                           "temperature": 0.7}
        assert [m["content"] for m in seen[1]["messages"]] == ["start", "first", "go on"]
        assert seen[1]["max_tokens"] is None and role.last_usage == {}
        with pytest.raises(OutputLengthExceededError):
            role.call("long")
        with pytest.raises(RuntimeError, match="empty reply"):
            role.call("silent")


def test_the_task_containers_keep_the_prepared_image(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.trial.paths import TrialPaths
    from hermit.agent.evolver.methods.coevoskills_adapter.environment import FixtureDockerEnvironment
    env = tmp_path / "environment"
    env.mkdir()
    (env / "Dockerfile").write_text("FROM fixed-officeqa\n")
    paths = TrialPaths(trial_dir=tmp_path / "trial")
    paths.mkdir()
    fixture = FixtureDockerEnvironment(environment_dir=env, environment_name="officeqa", session_id="cleanup-test",
                                       trial_paths=paths, task_env_config=EnvironmentConfig(allow_internet=False))
    calls = []

    async def execute(self, **kwargs):
        return None

    async def compose(self, command, **kwargs):
        calls.append(command)
    monkeypatch.setattr(DockerEnvironment, "exec", execute)
    monkeypatch.setattr(FixtureDockerEnvironment, "_run_docker_compose_command", compose)
    asyncio.run(fixture.stop(delete=True))
    assert calls == [["down", "--volumes", "--remove-orphans"]]               # no --rmi all


def test_the_worker_refuses_a_directory_that_is_no_task_package(tmp_path, monkeypatch):
    pytest.importorskip("toml")
    pytest.importorskip("harbor")
    from hermit.agent.evolver.methods.coevoskills_adapter import worker
    source = tmp_path / "task"
    source.mkdir()
    (source / "task.toml").write_text('[metadata]\nbenchmark = "x"\n')
    monkeypatch.setattr(worker, "checkout", lambda _: tmp_path / "pinned")
    request = {"upstream_root": "pinned", "task_path": str(source), "output": str(tmp_path / "result")}
    with pytest.raises(ValueError, match="no Harbor task package"):
        asyncio.run(worker.run(request))
    assert not (tmp_path / "result/evolution").exists()


def test_the_worker_reads_upstreams_final_verdict(tmp_path):
    from hermit.agent.evolver.methods.coevoskills_adapter.worker import final_verdict
    assert final_verdict(tmp_path) == {"passed": None, "reward": None, "verdict": None}
    log = tmp_path / "coevoskills-q1-1/agent/evolution_run_log.json"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"gt_oracle_result": {"passed": True, "reward": 1.0, "source": "best_snapshot"}}))
    assert final_verdict(tmp_path) == {"passed": True, "reward": 1.0,
                                       "verdict": {"passed": True, "reward": 1.0, "source": "best_snapshot"}}


def test_the_upstream_evolution_agent_is_unchanged_and_finds_the_oracle(tmp_path, monkeypatch):
    import inspect
    pytest.importorskip("harbor")
    if not os.environ.get("COEVOSKILLS_ROOT"):
        pytest.skip("set COEVOSKILLS_ROOT to the pinned checkout for the upstream integration check")
    from hermit.agent.evolver.methods.coevoskills_adapter import environment, oracle
    from hermit.agent.evolver.methods.coevoskills_adapter.upstream import checkout
    checkout(os.environ["COEVOSKILLS_ROOT"])
    from libs.terminus_agent.agents.terminus_2 import harbor_terminus_2_evolution as upstream
    cls = upstream.HarborTerminus2Evolution
    before = {name: value for name, value in vars(cls).items() if inspect.isfunction(value)}
    original = upstream.DockerEnvironment
    try:
        environment.install()
        assert upstream.DockerEnvironment is environment.FixtureDockerEnvironment
        assert {name: value for name, value in vars(cls).items() if inspect.isfunction(value)} == before
        monkeypatch.setenv("COEVOSKILLS_CUSTOM_AGENT_IMPORT_PATHS", json.dumps({
            "hermit-actor": "hermit.agent.evolver.methods.coevoskills_adapter.oracle:FixedActorOracle"}))
        callback = object.__new__(cls)._create_oracle_agent("hermit-actor", tmp_path, "the-runs-model")
        assert isinstance(callback, oracle.FixedActorOracle)
    finally:
        upstream.DockerEnvironment = original


# ---------- the OfficeQA preparation ----------

@pytest.fixture
def officeqa_preparation(tmp_path, monkeypatch):
    pytest.importorskip("skillopt")
    import hermit.task
    from skillopt.envs.officeqa import tool_runtime
    prepare = module(HERE / "prepare.py", "_coevoskills_adapter_prepare")
    utils = tmp_path / "utils"
    (utils / "splits").mkdir(parents=True)
    (utils / "task-info").mkdir()
    docs = tmp_path / "corpus"
    docs.mkdir()
    (docs / "source.txt").write_text("PUBLIC CORPUS")
    answers = tmp_path / "answers.jsonl"
    answers.write_text("".join(json.dumps({"id": tid, "answer": "PRIVATE-" + tid}) + "\n"
                               for tid in ("train-a", "val-a", "test-a")))
    (utils / "data.json").write_text(json.dumps({"docs": str(docs), "answers": str(answers)}))
    (utils / "tasks.jsonl").write_text("".join(json.dumps({"id": tid, "question": "PUBLIC-" + tid}) + "\n"
                                               for tid in ("train-a", "val-a", "test-a")))
    (utils / "splits/train.txt").write_text("train-a\n")
    (utils / "task-info/README.md").write_text("PUBLIC WORKFLOW")
    monkeypatch.setattr(prepare, "UTILS", utils)
    monkeypatch.setattr(tool_runtime, "resolve_docs_roots", lambda _: [docs])
    monkeypatch.setattr(hermit.task, "load_callable", lambda *_: lambda row, _: (row["question"], []))
    return prepare, utils, tmp_path / "prepared"


def test_preparation_stages_only_the_training_questions_with_answers_in_the_hidden_tests(officeqa_preparation):
    toml = pytest.importorskip("toml")
    prepare, _, output = officeqa_preparation
    assert prepare.prepare(output, build_image=False) == 1
    assert sorted(p.name for p in (output / "packages").iterdir()) == ["train-a"]
    package_dir = output / "packages/train-a"
    task = toml.loads((package_dir / "task.toml").read_text())
    assert task["metadata"]["benchmark"] == "officeqa" and task["agent"]["timeout_sec"] == prepare.ORACLE_TIMEOUT_S
    instruction = (package_dir / "instruction.md").read_text()
    assert instruction.startswith("PUBLIC-train-a\n") and instruction.endswith(
        "Save the final answer to `/root/answer.txt` as `<answer>...</answer>`.\n")
    docs = sorted(p.name for p in (package_dir / "environment/doc").iterdir())
    assert docs == ["officeqa.md"] and (package_dir / "environment/doc/officeqa.md").read_text() == "PUBLIC WORKFLOW"
    assert json.loads((package_dir / "tests/answer.json").read_text()) == "PRIVATE-train-a"
    public = [output / "image", package_dir / "environment"]
    assert all("PRIVATE-" not in p.read_text() for root in public for p in root.rglob("*") if p.is_file())
    assert json.loads((output / "revision.json").read_text())["training_ids"] == ["train-a"]


@pytest.mark.parametrize("ids", ["train-a\ntrain-a\n", "unknown\n"])
def test_preparation_rejects_an_invalid_training_split(officeqa_preparation, ids):
    prepare, utils, output = officeqa_preparation
    (utils / "splits/train.txt").write_text(ids)
    with pytest.raises(ValueError, match="duplicate or unknown OfficeQA IDs"):
        prepare.prepare(output, build_image=False)
    assert not output.exists()


# ---------- the example's configs ----------

@pytest.mark.parametrize("name", ["run_codex.yaml", "smoke_codex.yaml", "run_codex_luna.yaml", "smoke_codex_luna.yaml"])
def test_the_configs_use_the_tasks_acting_agent_and_the_columns_harness(name):
    from hermit.agent.evolver.loader import find
    data = Config.load(HERE / name)
    params, evolver = data["agent"]["params"], {**DEFAULTS, **data["evolver"]["params"]}
    assert data["agent"]["name"] == "codex" and data["judge"]["path"] == "../utils/judge.py"
    assert [params[k] for k in ("prepare", "answer", "collect")] == [f"../utils/task.py:{k}" for k in
                                                                     ("prepare", "answer", "collect")]
    assert params["web_search"] is False and params["container"]["network"] == "oq-sandbox"
    assert params["container"]["proxy"].startswith("http://oq-egress")
    assert find(data["evolver"]["name"]).directory == METHOD and data.template_dir() == TEMPLATE
    assert data["data"]["step_size"] == evolver["workers"] and data["llm"]
    assert evolver["deliver"] == "oracle_io.py:deliver" and evolver["path_map"] == PATH_MAP
    rounds = math.ceil(data["data"]["step_size"] / evolver["workers"])           # each question's loop bounds a step
    assert data["trainer"]["call_timeout_min"] * 60 > rounds * evolver["task_timeout_s"]
