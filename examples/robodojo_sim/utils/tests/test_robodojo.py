"""RoboDojo-Sim's example: the harness's XPolicyLab policy (utils/policy: the server-side Model, the confined
worker, the episode loop), the engine (utils/agent/dojo.py), the simulator service's rules, the judge and the task
rows. No Isaac Sim, no model, no network: RoboDojo is a stand-in checkout whose `scripts/robodojo.sh eval` runs the
installed policy's own Model and deploy.py against a five-step environment and writes `_result.json` where
RoboDojo writes it (FAKE_EVAL; the result's fields and path are RoboDojo's at commit f57e163, read from its
source, not recorded from a run)."""
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

UTILS = Path(__file__).resolve().parents[1]
EXAMPLE = UTILS.parent


def _load(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(path.parent))


@pytest.fixture
def tmp_path():
    """A directory outside /tmp: a program's worker runs under bubblewrap with an empty tmpfs at /tmp."""
    base = Path.home() / ".cache" / "hermit-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _bwrap_works() -> bool:
    if not shutil.which("bwrap"):
        return False
    return subprocess.run(["bwrap", "--unshare-user", "--unshare-net", "--ro-bind", "/", "/", "true"],
                          capture_output=True).returncode == 0


needs_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap: a program runs confined")

# RoboDojo's `robodojo.sh eval` without a simulator: the installed policy's Model (its worker included) driven by
# the installed deploy.py's loop over `--eval-num` episodes of five steps. An episode succeeds when the program
# commanded the left gripper to 1.0 on any step.
FAKE_EVAL = r'''
import argparse, importlib.util, json, os, pathlib, sys
ap = argparse.ArgumentParser()
ap.add_argument("cmd")
for flag in ("--policy-dir", "--task", "--ckpt", "--policy-env", "--eval-env", "--eval-num", "--env-cfg",
             "--action-type", "--seed", "--policy-gpu", "--env-gpu"):
    ap.add_argument(flag)
a = ap.parse_args()
root = pathlib.Path.cwd()
import numpy as np
def load(name):
    spec = importlib.util.spec_from_file_location(name, root / a.policy_dir / (name + ".py"))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
model = load("model").Model({"action_type": a.action_type, "env_cfg_type": a.env_cfg})
deploy = load("deploy")
class Client:
    def call(self, func_name, obs=None):
        return getattr(model, func_name)(obs) if obs is not None else getattr(model, func_name)()
class Env:
    def __init__(self, layout):
        self.layout, self.step, self.success = layout, 0, False
    def is_episode_end(self):
        return self.success or self.step >= 5
    def get_obs(self):
        return {"instruction": f"layout {self.layout}", "env_idx": 0, "additional_info": {"frequency": 25},
                "vision": {"cam_head": {"color": np.full((4, 6, 3), self.step, dtype=np.uint8)}},
                "state": {"left_arm_joint_state": np.arange(6, dtype=np.float32), "right_arm_joint_state": np.zeros(6),
                          "left_ee_joint_state": [0.0], "right_ee_joint_state": [0.0]}}
    def take_action(self, action):
        self.step += 1
        assert all(isinstance(v, np.ndarray) and v.dtype == np.float32 for v in action.values()), action
        if float(action.get("left_ee_joint_state", [0])[0]) == 1.0:
            self.success = True
details = {}
for layout in range(int(a.eval_num)):
    env = Env(layout)
    deploy.eval_one_episode(env, Client())
    details[str(layout)] = {"layout_id": layout, "success": env.success, "score": 1.0 if env.success else 0.0}
out = (root / "eval_result" / "RoboDojo" / a.task / "hermit_program" / a.env_cfg /
       f"{a.seed}_ckpt_name={a.ckpt},action_type={a.action_type}" / os.environ["ROBODOJO_RUN_ID"])
out.mkdir(parents=True)
(out / "_result.json").write_text(json.dumps({"success_rate": 0.0, "eval_time": 1, "score": 0.0, "details": details}))
(out / "episode_0000000_cam_head_fail.mp4").write_bytes(b"video")
'''

GOOD = "def act(obs):\n    return [{'left_ee_joint_state': [1.0]}]\n"


def fake_robodojo(tmp: Path) -> Path:
    root = tmp / "RoboDojo"
    (root / "scripts").mkdir(parents=True)
    (root / "XPolicyLab" / "policy").mkdir(parents=True)
    (root / "Assets" / "Eval_Layout").mkdir(parents=True)
    (root / "Assets" / "Eval_Layout" / "stack_bowls_0.json").write_text('{"bowl": [0.1, 0.2, 0.3]}')
    (root / "scripts" / "fake_eval.py").write_text(FAKE_EVAL)
    script = root / "scripts" / "robodojo.sh"
    script.write_text(f'#!/bin/bash\nexec "{sys.executable}" "$(dirname "$0")/fake_eval.py" "$@"\n')
    script.chmod(0o755)
    return root


def test_the_program_check_wants_one_act_and_a_chunk_is_checked_before_the_simulator_sees_it():
    pytest.importorskip("numpy")            # the policy server's Model and the stand-in simulator use it
    dojo = _load("dojo", UTILS / "agent" / "dojo.py")
    assert dojo.check_program(GOOD) is None
    assert "act" in dojo.check_program("def step(obs):\n    return []\n")
    assert "SyntaxError" in dojo.check_program("def act(obs:\n")
    assert "one argument" in dojo.check_program("def act(obs, extra):\n    return []\n")
    model = _load("hermit_program_model", UTILS / "policy" / "hermit_program" / "model.py")
    chunk = model.checked([{"left_arm_joint_state": [0] * 6, "left_ee_joint_state": 1}])
    assert chunk[0]["left_arm_joint_state"].shape == (6,) and chunk[0]["left_ee_joint_state"].tolist() == [1.0]
    for bad in ([], "x", [{}], [{"left_arm_joint_state": [0] * 5}], [{"left_ee_joint_state": [float("nan")]}],
                [{"left_arm_joint_state": [0] * 6, "right_ee_pose": [0] * 7}], [{"torque": [1]}],
                [{"left_ee_joint_state": [1]}] * (model.MAX_CHUNK + 1)):
        with pytest.raises(model.ProgramError):
            model.checked(bad)


def test_the_worker_gives_the_program_numpy_arrays_and_its_error_is_an_answer_not_a_crash(tmp_path):
    """The worker unconfined, driven by the policy server's own Worker class over its pipes."""
    np = pytest.importorskip("numpy")
    model = _load("hermit_program_model", UTILS / "policy" / "hermit_program" / "model.py")
    program = tmp_path / "policy.py"
    program.write_text("seen = []\n"
                       "def reset():\n    seen.clear()\n"
                       "def act(obs):\n"
                       "    image = obs['vision']['cam_head']['color']\n"
                       "    seen.append(1)\n"
                       "    print('this must not reach the channel')\n"
                       "    if obs['instruction'] == 'fail':\n        raise ValueError('boom')\n"
                       "    return [{'left_ee_joint_state': [float(image.shape[1])], "
                       "'right_ee_joint_state': [len(seen)]}]\n")
    worker = model.Worker({"argv": [sys.executable, str(UTILS / "policy" / "worker.py"), str(program)]})
    try:
        obs = {"instruction": "go", "vision": {"cam_head": {"color": np.zeros((4, 6, 3), dtype=np.uint8)}}}
        assert worker.call({"op": "reset"}, 30) == {"ok": True}
        first = worker.call({"op": "act", "obs": model.packed(obs)}, 30)
        assert first == {"ok": True, "actions": [{"left_ee_joint_state": [6.0], "right_ee_joint_state": [1]}]}
        failed = worker.call({"op": "act", "obs": model.packed({**obs, "instruction": "fail"})}, 30)
        assert failed["ok"] is False and "boom" in failed["error"]
        worker.call({"op": "reset"}, 30)                       # an episode's state is the program's to reset
        assert worker.call({"op": "act", "obs": model.packed(obs)}, 30)["actions"][0]["right_ee_joint_state"] == [1]
    finally:
        worker.close()


def test_the_engine_refuses_a_policy_that_is_not_the_harnesss_and_a_program_inside_the_checkout(tmp_path):
    dojo = _load("dojo", UTILS / "agent" / "dojo.py")
    root = fake_robodojo(tmp_path)
    engine = dojo.Engine(root)
    (tmp_path / "work").mkdir()
    program = tmp_path / "work" / "policy.py"
    program.write_text(GOOD)
    with pytest.raises(RuntimeError, match="not the harness's policy"):
        engine.run(program, task="stack_bowls", layout_seed=2, layouts=1, out=tmp_path / "out")
    installed = dojo.install(root)
    (installed / "deploy.py").write_text("def eval_one_episode(env, client):\n    env.success = True\n")
    with pytest.raises(RuntimeError, match="not the harness's policy"):     # an episode loop that grants success
        engine.run(program, task="stack_bowls", layout_seed=2, layouts=1, out=tmp_path / "out")
    dojo.install(root)
    inside = root / "policy.py"
    inside.write_text(GOOD)
    with pytest.raises(ValueError, match="inside RoboDojo's checkout"):
        engine.run(inside, task="stack_bowls", layout_seed=2, layouts=1, out=tmp_path / "out")


@needs_bwrap
def test_a_program_is_scored_by_what_it_does_and_reads_nothing_of_robodojos_checkout(tmp_path):
    """The reward-hacking boundary: RoboDojo's checkout holds the held-out layouts and the success conditions, and
    the policy server runs beside it. The program's worker sees none of it and has no network."""
    pytest.importorskip("numpy")            # the policy server's Model and the stand-in simulator use it
    dojo = _load("dojo", UTILS / "agent" / "dojo.py")
    root = fake_robodojo(tmp_path)
    dojo.install(root)
    engine = dojo.Engine(root)
    work = tmp_path / "work"
    work.mkdir()
    (work / "policy.py").write_text(GOOD)
    records = engine.run(work / "policy.py", task="stack_bowls", layout_seed=2, layouts=3, out=tmp_path / "good")
    assert [(r["layout_id"], r["task_success"]) for r in records] == [(0, True), (1, True), (2, True)]
    assert (tmp_path / "good" / "robodojo" / "_result.json").is_file()
    assert not list((root / "eval_result").rglob("_result.json"))          # nothing of a run stays in the checkout
    assert sorted(p.name for p in (tmp_path / "good" / "sandbox").iterdir())  # the worker's sandbox record
    secret = root / "Assets" / "Eval_Layout" / "stack_bowls_0.json"
    (work / "peek.py").write_text(
        "import socket\n"
        "def act(obs):\n"
        f"    try:\n        open({str(secret)!r}).read(); found = 1.0\n"
        "    except OSError:\n        found = 0.0\n"
        f"    try:\n        open({str(root / 'eval_result' / 'x')!r}, 'w').write('1'); found = 1.0\n"
        "    except OSError:\n        pass\n"
        "    try:\n        socket.create_connection(('1.1.1.1', 53), timeout=2); found = 1.0\n"
        "    except OSError:\n        pass\n"
        "    return [{'left_ee_joint_state': [found]}]\n")
    records = engine.run(work / "peek.py", task="stack_bowls", layout_seed=2, layouts=2, out=tmp_path / "peek")
    assert [r["task_success"] for r in records] == [False, False]
    assert not (root / "eval_result" / "x").exists()
    (work / "crash.py").write_text("def act(obs):\n    raise RuntimeError('no plan')\n")
    records = engine.run(work / "crash.py", task="stack_bowls", layout_seed=2, layouts=2, out=tmp_path / "crash")
    assert [r["task_success"] for r in records] == [False, False]          # held its joints to the horizon
    failed = dojo.failures(tmp_path / "crash")
    assert len(failed) == 2 and "no plan" in failed[0]["error"]


def test_the_rows_keep_development_and_held_out_layouts_in_different_sets(tmp_path, monkeypatch):
    P = _load("robodojo_prepare", UTILS / "prepare.py")
    assert sum(len(tasks) for tasks in P.DIMENSIONS.values()) == 42
    assert len({t for tasks in P.DIMENSIONS.values() for t in tasks}) == 42
    monkeypatch.setattr(P, "HERE", tmp_path)
    P.main([])
    rows = [json.loads(line) for line in (tmp_path / "tasks.jsonl").read_text().splitlines()]
    assert len(rows) == 42 and all(r["layout_set"] not in r["eval_layout_sets"] for r in rows)
    assert rows[0] == {"id": "robodojo-stack_bowls", "benchmark": "robodojo", "suite": "stack_bowls", "task_id": 0,
                       "dimension": "generalization", "layout_set": 2, "debug_seeds": "0-9",
                       "eval_layout_sets": [0, 1], "eval_layouts": 25}
    assert len((tmp_path / "splits" / "all.txt").read_text().split()) == 42
    with pytest.raises(SystemExit):
        P.main(["--dev-set", "0"])                           # the development set is a held-out set


def test_the_service_runs_only_the_sessions_development_layouts_from_the_first(tmp_path, monkeypatch):
    S = _load("robodojo_sim_service", UTILS / "agent" / "dojo_service.py")
    row = {"id": "robodojo-stack_bowls", "benchmark": "robodojo", "suite": "stack_bowls", "task_id": 0,
           "layout_set": 2, "debug_seeds": "0-2", "eval_layout_sets": [0, 1], "eval_layouts": 25}
    ran = []

    class Engine:
        root = tmp_path

        def validate(self, program):
            return None

        def run(self, program, *, task, layout_seed, layouts, out):
            ran.append((task, layout_seed, layouts))
            return [{"layout_id": n, "task_success": n == 0, "score": float(n == 0)} for n in range(layouts)]
    sessions = tmp_path / "sessions"
    service = S.Service(Engine(), {row["id"]: row}, [sessions], S.base.Ledger(tmp_path / "ledger.json"))
    work = sessions / "rollout" / f"{row['id']}-56d9fea2" / "workspaces" / "w" / "work"
    work.mkdir(parents=True)
    (work / "policy.py").write_text(GOOD)
    assert service.register({"workspace": str(work), "task": row["id"]}) == {"ok": True}
    request = lambda seeds: service.run({"workspace": str(work), "cmd": "run-batch", "program": "policy.py",
                                         "seeds": seeds, "output_root": "runs/v1"})
    done = request([0, 1])
    assert done["ok"] and "1/2 layouts succeeded" in done["stdout"] and ran == [("stack_bowls", 2, 2)]
    assert "not assigned to you" in request([0, 1, 2, 3])["error"]         # more than its development layouts
    assert "from the first" in request([1, 2])["stderr"] and len(ran) == 1
    other = {**row, "id": "robodojo-other", "benchmark": "libero-pro"}
    service.tasks[other["id"]] = other
    elsewhere = sessions / "rollout" / "robodojo-other-56d9fea2" / "w"
    elsewhere.mkdir(parents=True)
    with pytest.raises(ValueError, match="not a RoboDojo task"):
        service.register({"workspace": str(elsewhere), "task": other["id"]})


def test_the_judge_scores_the_program_on_the_held_out_sets_only(tmp_path, monkeypatch):
    J = _load("robodojo_judge", UTILS / "judge.py")
    ran = []

    class Engine:
        def __init__(self, root, **options):
            pass

        def run(self, program, *, task, layout_seed, layouts, out):
            ran.append((layout_seed, layouts))
            return [] if layout_seed == 1 else [{"layout_id": n, "task_success": n < 3, "score": 0.0}
                                                for n in range(layouts - 1)]     # one layout skipped as unstable
    monkeypatch.setattr(J.dojo, "Engine", Engine)
    monkeypatch.setenv("ROBODOJO_ROOT", str(tmp_path))
    monkeypatch.setenv("ROBODOJO_OUT", str(tmp_path / "out"))
    row = {"id": "robodojo-stack_bowls", "suite": "stack_bowls", "layout_set": 2, "eval_layout_sets": [0, 1],
           "eval_layouts": 5}
    out = lambda facts: SimpleNamespace(extra={"task": facts}, output=None)
    scored = J.score(row, out({"program": GOOD}))
    assert ran == [(0, 5), (1, 5)]                           # never the development set
    assert scored["passed"] == 3 and scored["layouts"] == 9 and scored["score"] == 3 / 9   # set 1 gave no result
    assert J.score(row, out({}))["score"] == 0.0 and J.score(row, out({"program": "x = 1\n"}))["score"] == 0.0
    assert len(ran) == 2                                     # neither started a simulator


def test_the_configs_load_and_show_the_task_sandbox_nothing_of_robodojo():
    from hermit.config import Config
    for path in sorted((EXAMPLE / "prompt_only").glob("*.yaml")):
        data = Config.load(path).data
        assert data["environment"]["read"] == ["../utils/agent"], path
        assert data["environment"]["env"] == ["ROBODOJO_SIM_URL"], path
