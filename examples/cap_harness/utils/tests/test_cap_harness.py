"""The cap-harness engine (ASPIRE's harness) shared by Robosuite and LIBERO: its task functions (the workspace and its
registration), the simulator service's rules as bin/sim meets them (a real service on a loopback port, over a fake
checkout: cap_fakes.py), the engine's sandbox, the judge (a stand-in engine), the docs it stages, and the tasks each
benchmark writes with its helpers; no simulator, no model, no network."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent


def _fakes():
    spec_ = importlib.util.spec_from_file_location("cap_fakes", HERE / "cap_fakes.py")
    module = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(module)
    return module


F = _fakes()
tmp_path, service = F.tmp_path, F.service                 # fixtures
EXAMPLE, ROW, LIBERO_ROW, BUDGET = F.EXAMPLE, F.ROW, F.LIBERO_ROW, F.BUDGET
_load, fake_aspire, needs_bwrap, _Service, _workspace = F._load, F.fake_aspire, F.needs_bwrap, F._Service, F._workspace
_register, _sim, _sim_module, _fake_gpu = F._register, F._sim, F._sim_module, F._fake_gpu
LIBERO = ROOT / "examples" / "libero"
ROBOSUITE = ROOT / "examples" / "robosuite"


def _task_module():
    return _load("cap_task", EXAMPLE / "agent" / "task.py")


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


def test_prepare_registers_the_session_and_the_service_holds_its_rules(tmp_path, service):
    T = _task_module()
    work = _workspace(service, ROW)
    params = {"aspire_root": str(tmp_path / "ASPIRE")}
    T.prepare(ROW, work, params, tmp_path)
    for name in ("docs/api-reference.md", "docs/task-exploration.md", "scripts/scene_snapshot.py", "bin/sim",
                 "bin/sim.json"):
        assert (work / name).is_file(), name
    assert not (work / "task.md").exists() and not (work / "docs" / "evosearch-iteration.md").exists()   # a method's
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
    s = T.settings(LIBERO_ROW, params)
    assert T.task_dir(s) == "libero-pro/libero_90/task_33" and s["doc"] == "libero-pro.md"
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
    stage = lambda: subprocess.run([sys.executable, str(EXAMPLE / "agent" / "stage_docs.py"), "--aspire-root",
                                    str(root), "--tasks", str(tmp_path / "rows.jsonl"), "--out", str(staged)],
                                   capture_output=True, text=True)
    before = {p.name: p.stat().st_ino for p in staged.iterdir()}
    assert stage().returncode == 0 and {p.name: p.stat().st_ino for p in staged.iterdir()} == before   # in place
    (staged / "extra").mkdir()                                  # anything else there: staged anew, without it
    (staged / "notes.bin").write_bytes(b"\xff\xfe")
    assert stage().returncode == 0
    assert sorted(p.name for p in staged.iterdir()) == ["api-reference.md", "libero-pro.md", "robosuite.md",
                                                        "run-artifacts.md"]
    (staged / "libero-pro.md").write_text("# the staged page\n")
    work = _workspace(service, LIBERO_ROW)
    T.prepare(LIBERO_ROW, work, {"aspire_root": str(root), "aspire_docs": str(staged)}, tmp_path)
    assert (work / "docs" / "libero-pro.md").read_text() == "# the staged page\n"     # the staged copy, as it is
    (root / "docs" / "libero-pro.md").write_text("# LIBERO-Pro\n\n## Worked programs\n| `close_microwave.py` |\n")
    with pytest.raises(ValueError, match="Task examples"):
        _sim_module().workspace_docs(root, "libero-pro")
    # No Codex config of a benchmark on this engine grants a page of the checkout's own docs; each loads, reads its
    # tasks through the engine's row view and is judged by the engine's judge
    from hermit.config import Config
    configs = [p for folder in (ROBOSUITE / "aspire", LIBERO / "aspire", LIBERO / "prompt_only")
               for p in sorted(folder.glob("*.yaml"))]
    assert len(configs) == 19
    for path in configs:
        config = Config.load(path)
        data = config.data
        read = [str(p) for p in (data.get("environment") or {}).get("read") or []]
        assert not any("/docs/" in p for p in read), path
        assert config.path(data["data"]["view"].split(":")[0]) == (EXAMPLE / "agent" / "fields.py").resolve(), path
        assert config.path(data["judge"]["path"]) == (EXAMPLE / "benchmark" / "judge.py").resolve(), path
        if "_direct" not in path.name:
            assert "$ASPIRE_DOCS" in read and data["agent"]["params"]["aspire_docs"] == "$ASPIRE_DOCS", path
            assert any(config.path(p) == (EXAMPLE / "agent").resolve() for p in read if "$" not in p), path
            if data["agent"]["name"] == "codex":    # Codex reads AGENTS.md (the adapter's context) only up to
                assert data["agent"]["params"]["project_doc_max_bytes"] >= 65536, path    # this: past it, refused
        if path.parent.name == "prompt_only":            # no method's pages: none granted, none staged
            assert not any("evosearch" in p or "-prompt.md" in p for p in read), path


def test_the_engine_takes_the_benchmarks_venv_and_refuses_one_it_does_not_run(tmp_path, monkeypatch):
    sim = _sim_module()
    root = fake_aspire(tmp_path)
    engine = sim.Engine(root, benchmark="libero-pro")
    assert engine.cli == root.resolve() / ".venv-libero" / "bin" / "cap-harness"
    assert engine._env()["LIBERO_CONFIG_PATH"] == str(root.resolve() / ".libero")
    assert sim.init_args("libero-pro") == ["--init-mode", "seeded"] and sim.init_args(None) == []
    assert sim.Engine(root).cli.parent.parent.name == ".venv-robosuite"
    commands = []                                  # the command line of a sweep, the judge's Stage 2 included

    def recorded(cmd, **_):
        commands.append(cmd)
        raise RuntimeError("recorded")

    monkeypatch.setattr(sim, "confine", recorded)
    for each in (engine, sim.Engine(root)):
        with pytest.raises(RuntimeError, match="recorded"):
            each.run(tmp_path / "p.py", suite="s", seeds=[1, 2], out=tmp_path / "o")
    at = commands[0].index("--init-mode")
    assert commands[0][at:at + 2] == ["--init-mode", "seeded"] and "--init-mode" not in commands[1]
    shutil.rmtree(root / ".venv-libero")
    with pytest.raises(FileNotFoundError, match="bootstrap_libero.sh"):
        sim.Engine(root, benchmark="libero-pro")
    with pytest.raises(ValueError, match="behavior"):
        sim.Engine(root, benchmark="behavior")


def test_the_libero_tasks_are_liberos_five_suites_and_liberos_pros_eighty_with_aspires_seeds(tmp_path, monkeypatch):
    """The tasks and splits prepare.py writes, read as their rows (the engine's fields.py): what the simulator service,
    the judge and every method's configs read."""
    from hermit.config import Config
    from hermit.task import Benchmark
    P = _load("libero_prepare", LIBERO / "utils" / "benchmark" / "prepare.py")
    Fd = _load("cap_fields", EXAMPLE / "agent" / "fields.py")
    assert sum(P.SUITES.values()) == 130 and P.SUITES["libero_90"] == 90
    pro = [f"libero_{family}_{kind}" for family in ("object", "spatial", "goal", "10") for kind in ("swap", "task")]
    assert list(P.PRO_SUITES) == pro and set(P.PRO_SUITES.values()) == {10}
    monkeypatch.setattr(P, "HERE", tmp_path)
    monkeypatch.setattr(sys, "argv", ["prepare.py"])
    P.main()
    found = Fd.task_rows(tmp_path / "tasks")                          # each task directory as its row
    rows = list(found.values())
    tasks = [found[i] for i in (tmp_path / "splits" / "all.txt").read_text().split()]
    assert len(tasks) == 210 and len(rows) == 420 and len({r["id"] for r in rows}) == 420    # a suite and its
    assert all(r["benchmark"] == "libero-pro" for r in rows)                  # perturbations share their task ids
    assert list(found["libero_goal-t03"]) == ["id", "benchmark", "suite", "task_id", "debug_seeds", "unseen_seeds",
                                              "eval_seeds"]                  # the row's keys, in its order
    assert found["rerun-libero_goal-t03"] == {**found["libero_goal-t03"], "id": "rerun-libero_goal-t03",
                                              "rerun_of": "libero_goal-t03"}       # the same task, its own id
    assert not any(r["id"].startswith("verify-") for r in rows)           # a method's own rows are its own
    assert (tmp_path / "tasks" / "libero_goal-t03" / "instruction.md").is_file()
    assert all((r["debug_seeds"], r["unseen_seeds"], r["eval_seeds"]) == ("51-65", "66-80", "1-50") for r in tasks)
    split = lambda name: (tmp_path / "splits" / name).read_text().split()
    by = lambda *suites: [r["id"] for r in tasks if r["suite"] in suites]
    assert split("all.txt") == [r["id"] for r in tasks]
    assert split("libero.txt") == by("libero_object", "libero_spatial", "libero_goal", "libero_10")
    assert split("libero_90.txt") == by("libero_90") and split("libero_pro.txt") == by(*pro)
    assert split("libero_pro_long.txt") == by("libero_10_swap", "libero_10_task")
    assert split("smoke.txt") == split("libero.txt")[:2] and split("smoke_pro.txt") == split("libero_pro.txt")[:2]
    assert split("libero_sample.txt") == [f"{s}-t{n:02d}" for s in ("libero_object", "libero_spatial", "libero_goal")
                                          for n in (0, 5)]
    for path in sorted((LIBERO / "prompt_only").glob("*.yaml")):         # a method that reads the tasks as written
        config = Config.load(path)
        data = config.data["data"]
        assert config.path(data["tasks"]) == (LIBERO / "utils" / "benchmark" / "tasks").resolve(), path
        monkeypatch.setitem(data, "tasks", str(tmp_path / "tasks"))           # the tasks prepared here
        for key in ("train", "val", "test"):
            if data.get(key):
                monkeypatch.setitem(data, key, str(tmp_path / "splits" / Path(data[key]).name))
        assert {t.id: t.given() for t in Benchmark.from_config(config).tasks.values()} == found, path
        train, test = split(Path(data["train"]).name), split(Path(data["test"]).name)
        assert train and set(train) <= {r["id"] for r in tasks}, path
        assert test == [f"rerun-{i}" for i in train], path        # the reruns are of the tasks it trained on
    seeds = P.seeds_from(P.PRESETS, [])
    assert seeds == {"debug_seeds": "51-65", "unseen_seeds": "66-80", "eval_seeds": "1-50"}
    assert P.seeds_from(P.PRESETS, ["--preset", "smoke", "--eval", "1-5"])["eval_seeds"] == "1-5"
    with pytest.raises(SystemExit):
        P.seeds_from(P.PRESETS, ["--eval", "1-60"])              # held-out seeds a session could run


def test_the_neutral_libero_prompt_names_the_task_the_seeds_and_the_scored_program_and_nothing_of_a_method(
        tmp_path, service):
    """prompt_only/'s acting prompt and workspace: LIBERO's prompt and the engine's workspace, nothing of a method."""
    P = _load("libero_task", LIBERO / "utils" / "agent" / "task.py")
    T = _task_module()
    root = tmp_path / "ASPIRE"                                         # the service fixture's ASPIRE tree
    params = {"aspire_root": str(root)}                                 # as prompt_only's configs set them
    row = {**LIBERO_ROW, "suite": "libero_object_task", "task_id": 4, "id": "libero_object_task-t04"}
    text = P.prompt(row, params, tmp_path)
    assert "Suite `libero_object_task`, task id `4`" in text and "get_task_context()" in text
    assert "**51-53**" in text and "**66**" in text and "3 held-out seeds" in text
    scored = f"{T.task_dir(T.settings(row, params))}/fix_code.py"
    assert f"Your program is `{scored}`" in text                     # the file the engine's collect scores
    unseen = f"--output-root {scored.rsplit('/', 1)[0]}/debug/holdout"     # where the engine's collect reads it
    assert unseen in text and "docs/libero-pro.md" in text and "simulator steps" not in text
    assert "at most 600 simulator steps" in P.prompt(row, {**params, "max_steps": 600}, tmp_path)
    assert not any(word in text for word in ("skill", "library", "coordinator", "fix-loop", "evolutionary"))
    work = _workspace(service, LIBERO_ROW)
    T.prepare(LIBERO_ROW, work, params, tmp_path)
    assert (work / "bin" / "sim").is_file() and (work / "docs" / "libero-pro.md").is_file()
    assert (work / "docs" / "task-exploration.md").is_file()                 # the scene procedure stays
    assert not (work / "docs" / "evosearch-iteration.md").exists() and not (work / "task.md").exists()
    # The scored program is fix_code.py in the task's folder, whatever else the session left there
    s = T.settings(LIBERO_ROW, params)
    (work / T.task_dir(s)).mkdir(parents=True, exist_ok=True)
    (work / T.task_dir(s) / "fix_code.py").write_text("# fix\n")
    (work / T.task_dir(s) / "findings.md").write_text("notes\n")
    (work / "evosearch").mkdir()
    (work / "evosearch" / "evosearch_best_code.py").write_text("# best\n")
    facts = T.collect(LIBERO_ROW, work, tmp_path / "log-a", {**params, "prune_media": False})
    assert facts["scored_program"] == f"{T.task_dir(s)}/fix_code.py" and facts["fix_code"] == "# fix\n"
    assert facts["findings"] == "notes\n" and facts["unseen"] is None and "evosearch" not in facts
    assert (tmp_path / "log-a" / "files" / "fix_code.py").read_text() == "# fix\n"


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
    mismatched = _workspace(service, BUDGET, "12345678")        # a rollout registers only the task its name carries
    answer = _register(service.url, {"workspace": str(mismatched), "task": ROW["id"]})
    assert not answer["ok"] and "is not one of task" in answer["error"]


def test_a_row_with_an_attempt_budget_runs_that_many_programs(tmp_path, service):
    T = _task_module()
    work = _workspace(service, BUDGET)
    T.prepare(BUDGET, work, {"aspire_root": str(tmp_path / "ASPIRE")}, tmp_path)
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
    page = tmp_path / "ASPIRE" / "docs" / "libero-pro.md"                          # names the worked programs per task
    demos = tmp_path / "ASPIRE" / "third_party" / "LIBERO-PRO" / "libero" / "datasets" / "demo.hdf5"
    demos.parent.mkdir(parents=True)
    demos.write_text("demonstration\n")
    harness = tmp_path / "ASPIRE" / ".venv-robosuite" / "bin" / "cap-harness"
    targets = {"WRITE": [tmp_path / "planted.txt", tmp_path / "ASPIRE" / "planted.py", work / "planted.txt"],
               "READ": [secret, stage2, other / "fix_code.py", solution, history, native, page, demos, harness]}
    (work / "p.py").write_text("result = {}\n" + "".join(f"# {w} {p}\n" for w, ps in targets.items() for p in ps))
    ran = _sim(work, "run", "--seed", "101", "--program", "p.py", "--output-root", "out")
    tried = json.loads((work / "out" / "run.json").read_text())["tried"]
    assert ran.returncode == 0, ran.stderr
    assert not (tmp_path / "planted.txt").exists() and not (tmp_path / "ASPIRE" / "planted.py").exists()
    assert tried[str(work / "planted.txt")] != "done" and not (work / "planted.txt").exists()   # its workspace too
    assert all(tried[str(p)] != "done"
               for p in (secret, stage2, other / "fix_code.py", solution, history, native, page, demos))
    assert tried[str(harness)] == "done"                                             # the checkout it runs from


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


def test_the_judge_runs_the_program_on_the_rows_held_out_seeds_within_its_budget(tmp_path, monkeypatch):
    J = _load("cap_judge", EXAMPLE / "benchmark" / "judge.py")
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
    actor = {"fix_code": "result = {}\n", "unseen": {"passed": 1, "total": 1}}
    assert J.score(ROW, out(actor))["score"] == 2 / 3 and runs[-1] == [1, 2, 3]   # the row's held-out seeds
    assert J.score(ROW, out({**actor, "fix_code": "import os\n"}))["score"] == 0.0
    assert J.score(ROW, out({**actor, "max_steps": 300}))["score"] == 2 / 3 and engines[-1]["max_steps"] == 300
    assert engines[-1]["benchmark"] == "robosuite"
    assert J.score(LIBERO_ROW, out(actor))["score"] == 2 / 3 and engines[-1]["benchmark"] == "libero-pro"
    assert J.score(ROW, out(actor))["feedback"] == "2/3 held-out seeds; unseen-check 1/1"
    # A row with an attempt budget counts only a program its budget ran (the service's ledger), under its own label
    work = str(tmp_path / "w")
    budget = {**BUDGET, "eval_seeds": "101-105", "score_label": "verifier"}
    ran = {"fix_code": "result = {}\n", "workspace": work}
    ledger = tmp_path / "out" / "sim-service" / "ledger.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({work: {"programs": [hashlib.sha256(b"result = {}\n").hexdigest()]}}))
    scored = J.score(budget, out(ran))
    assert scored["score"] == 3 / 5 and runs[-1] == [101, 102, 103, 104, 105]
    assert scored["feedback"] == "3/5 verifier seeds"
    third = J.score(budget, out({**ran, "fix_code": "result = {'v': 3}\n"}))   # beyond its two programs
    assert third["score"] == 0.0 and "attempt budget" in third["feedback"]
    direct = {k: v for k, v in ran.items() if k != "workspace"}       # a session that runs its own programs
    assert J.score(budget, out({**direct, "fix_code": "result = {'v': 3}\n"}))["score"] == 3 / 5
    assert J.score(ROW, out({}))["score"] == 0.0                                # no program: not scored
    withheld = J.score(ROW, out({**actor, "not_scored": "verifier verdict void"}))   # a method's rule
    assert withheld == {"score": 0.0, "feedback": "verifier verdict void", "seeds": 3}


def test_the_robosuite_tasks_are_task_directories_read_back_as_the_rows_they_were(tmp_path, monkeypatch):
    """prepare.py writes each task and its rerun as a task directory (Harbor's format, no network, judged on the
    host); the engine's fields.py, every config's data.view, reads each back as exactly the row it wrote."""
    from hermit.task import Benchmark
    P = _load("robosuite_prepare", ROBOSUITE / "utils" / "benchmark" / "prepare.py")
    Fd = _load("cap_fields", EXAMPLE / "agent" / "fields.py")
    monkeypatch.setattr(P, "HERE", tmp_path)
    monkeypatch.setattr(sys, "argv", ["prepare.py", "--preset", "smoke"])
    P.main()
    found = Fd.task_rows(tmp_path / "tasks")
    seeds = {"debug_seeds": "101-103", "unseen_seeds": "126", "eval_seeds": "1-3"}
    task = {"id": "robosuite-cube_stack", "suite": "cube_stack", "task_id": 0}
    assert json.dumps(found["robosuite-cube_stack"]) == json.dumps({**task, **seeds})     # its keys in order, types
    assert json.dumps(found["rerun-robosuite-cube_stack"]) == json.dumps(
        {**task, "id": "rerun-robosuite-cube_stack", "rerun_of": "robosuite-cube_stack", **seeds})
    split = lambda name: (tmp_path / "splits" / name).read_text().split()
    assert len(found) == 14 and split("smoke.txt") == ["robosuite-cube_lifting", "robosuite-cube_stack"]
    assert split("smoke_rerun.txt") == ["rerun-robosuite-cube_lifting", "rerun-robosuite-cube_stack"]
    assert split("rerun.txt") == [f"rerun-{i}" for i in split("all.txt")] and len(split("all.txt")) == 7
    loaded = Benchmark.load(tmp_path / "tasks", view=Fd.task_row).tasks["robosuite-cube_stack"]
    assert loaded.private["tests"] is None and loaded.private["needs"]["network"]["agent"] == "no-network"


def test_prepare_refuses_held_out_seeds_a_session_could_run():
    for args in (["--eval", "1-200"], ["--unseen", "120-130"], ["--dev", "1-5"]):
        done = subprocess.run([sys.executable, str(ROBOSUITE / "utils" / "benchmark" / "prepare.py"), *args],
                              capture_output=True, text=True)
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
