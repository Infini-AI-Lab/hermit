"""Stand-ins for the cap-harness engine's tests and the tests of a method on it (tests/methods/aspire/): a fake
ASPIRE checkout whose cap-harness writes a run's files without a simulator (FAKE_HARNESS), the simulator service on a
loopback port over it, a session's workspace where the framework puts it, and the task rows of each benchmark. Loaded
by path (`_load`), never imported from a conftest."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

UTILS = Path(__file__).resolve().parents[1]           # examples/cap_harness/utils
EXAMPLE = UTILS                                       # the engine's agent/ and benchmark/ folders


@pytest.fixture
def tmp_path():
    """A directory outside /tmp: the template check runs under bubblewrap with an empty tmpfs at /tmp."""
    base = Path.home() / ".cache" / "hermit-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


def _load(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    try:
        spec_ = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(path.parent))


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
# A row with an attempt budget: at most two programs (by content) beyond a single run on the first seed
BUDGET = {"id": "robosuite-cube_stack", "suite": "cube_stack", "task_id": 0, "debug_seeds": "101-105",
          "eval_seeds": "1-3", "max_programs": 2}


class _Service:
    """The simulator service on a free loopback port, over a fake checkout whose cap-harness writes a run's files
    without a simulator (FAKE_HARNESS), serving ROW, BUDGET, LIBERO_ROW and the `rows` given."""

    def __init__(self, tmp: Path, root: Path, rows=(), **options):
        import threading
        from http.server import ThreadingHTTPServer
        S = _load("cap_sim_service", EXAMPLE / "agent" / "sim_service.py")
        self.sessions = tmp / "sessions"
        self.sessions.mkdir()
        served = {r["id"]: r for r in (ROW, BUDGET, LIBERO_ROW, *rows)}
        self.service = service = S.Service(root, served, [self.sessions],
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
    work.mkdir(parents=True)
    return work


def _register(url: str, body: dict) -> dict:
    import urllib.request
    request = urllib.request.Request(url + "/register", data=json.dumps(body).encode())
    with urllib.request.urlopen(request) as reply:
        return json.loads(reply.read())


def _sim(work: Path, *args):
    return subprocess.run([str(work / "bin" / "sim"), *args], capture_output=True, text=True, cwd=work)


def _sim_module():
    sys.path.insert(0, str(EXAMPLE / "agent"))
    try:
        import sim
        return sim
    finally:
        sys.path.remove(str(EXAMPLE / "agent"))


def _fake_gpu(tmp_path, monkeypatch) -> list[int]:
    """GPU 8 as hermit's sandbox sees devices (sandbox.DEV): a machine without one can still build the command."""
    import hermit.sandbox as sandbox_module
    dev = tmp_path / "dev"
    dev.mkdir(exist_ok=True)
    for name in ("nvidiactl", "nvidia8"):
        (dev / name).touch()
    monkeypatch.setattr(sandbox_module, "DEV", str(dev))
    return [8]
