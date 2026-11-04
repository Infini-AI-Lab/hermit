"""AgentEvolver `tools` and `hide`: the session's tool server serves only the allowed tools, the hiding sandbox
covers paths, and a Codex run sees no Trainer endpoint."""
import json
import os

import pytest

from hermit.config import Config
from hermit.sandbox import FRAMEWORK, HidingSandbox, hidden_paths
from hermit.trainer.trainer import Trainer
from test_training import make_task


def test_hidden_paths_and_hiding_sandbox(tmp_path):
    paths = hidden_paths(["module:yaml", str(tmp_path)])
    assert FRAMEWORK / "examples" in paths and FRAMEWORK / ".git" in paths and tmp_path in paths
    assert any(p.name == "yaml" for p in paths)
    with pytest.raises(RuntimeError, match="no installed package"):
        hidden_paths(["module:no_such_package_xyz"])

    class Inner:
        def wrap(self, command, **kw):
            return ["bwrap", "--ro-bind", "/", "/", "--", *command], {"X": "1"}
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    keep = tmp_path / "task-info"
    keep.mkdir()
    args, env = HidingSandbox(Inner(), [tmp_path, secret], keep=[keep]).wrap(["true"])
    cut = args.index("--")
    # the secret lies in the covered directory already; the task description is bound back on top of it
    assert args[4:cut] == ["--tmpfs", str(tmp_path), "--ro-bind", str(keep), str(keep)]
    assert env == {"X": "1"} and args[-1] == "true"
    assert HidingSandbox(Inner(), []).wrap(["true"])[0][-1] == "true"


FAKE_CODEX = r'''#!/usr/bin/env python3
"""A codex stand-in: lists the tools its MCP server was pointed at and reports them through report.md."""
import json, re, sys, urllib.request
args = sys.argv[1:]
import os, tomllib
server = tomllib.load(open(os.environ["CODEX_HOME"] + "/config.toml", "rb"))["mcp_servers"]["evolver_tools"]
url = server["url"][:-len("/mcp")]                  # the tool server's JSON entrance beside its MCP one
prompt = args[-1]
output = re.search(r"output=(\S+)", prompt).group(1)
tools = [t["name"] for t in json.loads(urllib.request.urlopen(url + "/tools").read())]
gated = (server["default_tools_approval_mode"] == "approve" and "Tools: check_spec (template)" in prompt
         and "http://" not in prompt)
open(output + "/report.md", "w").write(f"tools={','.join(tools)} gated={gated}\n")
print(json.dumps({"type": "thread.started", "thread_id": "sess-1"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2}}))
'''


def test_agent_evolver_gates_the_tools(tmp_path, monkeypatch):
    path = make_task(tmp_path, sandbox={"enabled": False, "network": "open"})
    evolver = tmp_path / "gated"
    evolver.mkdir()
    (evolver / "codex").write_text(FAKE_CODEX)
    (evolver / "codex").chmod(0o755)
    (evolver / "START.md").write_text("output={output}\n{message}")
    (evolver / "evolver.yaml").write_text("class: hermit.agent.evolver.agent:AgentEvolver\n"
                                          "params: {backend: codex, web_search: false, prompt: START.md, home: [], tools: [check_spec]}\n")
    monkeypatch.setenv("PATH", f"{evolver}{os.pathsep}{os.environ['PATH']}")
    cfg = Config.load(path, ["evolver.name=gated", "trainer.epochs=1", "data.step_size=6"])
    Trainer(cfg, log=lambda *_: None).train()
    steps = [json.loads(l) for l in (tmp_path / "runs/toy-run/steps.jsonl").read_text().splitlines()]
    assert steps[0]["evolver"]["summary"] == "tools=check_spec gated=True"


@pytest.mark.skipif(not __import__("shutil").which("bwrap"), reason="needs bubblewrap")
def test_hidden_paths_are_covered_under_every_name_and_the_runs_own_places_stay(tmp_path):
    """A hidden directory is covered under the name it was given and its real one (a workspace reached through a
    link shows it under both); the places the session is given to work in, when they lie below a hidden directory
    (a run kept under the framework's examples/), stay visible, and what is hidden below them stays covered."""
    import subprocess
    from hermit.sandbox import Sandbox
    examples = tmp_path / "examples"                      # stands for the framework's examples/: task data, a run
    (examples / "data").mkdir(parents=True)
    (examples / "data" / "answers.txt").write_text("42\n")
    run = examples / "task" / "runs" / "r"
    for area in ("state", "adapter", "material"):
        (run / area).mkdir(parents=True)
    (run / "adapter" / "ADAPTER.md").write_text("mine\n")
    (run / "material" / "secret.txt").write_text("hidden from this call\n")
    real = tmp_path / "real"
    (real / "records").mkdir(parents=True)
    (real / "records" / "log.txt").write_text("hidden\n")
    (tmp_path / "alias").symlink_to(real)
    alias = tmp_path / "alias"
    box = Sandbox(network="open", writable=[run / "state", run / "adapter", alias],
                  readable=[run / "material"])
    hiding = HidingSandbox(box, [examples, run / "material", alias / "records"])

    def shows(path) -> str:
        args, env = hiding.wrap(["sh", "-c", f"cat {path} 2>/dev/null || echo ABSENT"], env=dict(os.environ))
        return subprocess.run(args, env=env, capture_output=True, text=True, timeout=60).stdout.strip()
    assert shows(examples / "data" / "answers.txt") == "ABSENT"              # the hidden tree's own files
    assert shows(run / "adapter" / "ADAPTER.md") == "mine"                   # the run's places below it stay
    assert shows(run / "material" / "secret.txt") == "ABSENT"               # hidden below them: covered again
    assert shows(alias / "records" / "log.txt") == "ABSENT"                  # under the link's name
    assert shows(real / "records" / "log.txt") == "ABSENT"                   # and under the real one


@pytest.mark.skipif(not __import__("shutil").which("bwrap"), reason="needs bubblewrap")
def test_a_hidden_link_to_a_place_the_sandbox_does_not_show_still_lets_the_call_run(tmp_path):
    """A session can leave a link where it writes; a method that hides every entry there hides the link too. The
    link's own name is not mounted over (inside, it leads nowhere the sandbox shows): the call still starts, and
    what it points to stays out of reach."""
    import subprocess
    from hermit.sandbox import Sandbox
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s\n")
    (scratch / "planted").symlink_to(outside)
    (scratch / "mine.txt").write_text("mine\n")
    hiding = HidingSandbox(Sandbox(network="open", writable=[scratch]), [scratch / "planted"])
    args, env = hiding.wrap(["sh", "-c", f"cat {scratch / 'mine.txt'}; cat {outside / 'secret.txt'} 2>/dev/null "
                                         f"|| echo ABSENT"], env=dict(os.environ))
    out = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.split() == ["mine", "ABSENT"], out.stderr


@pytest.mark.skipif(not __import__("shutil").which("bwrap"), reason="needs bubblewrap")
def test_links_among_hidden_paths_and_the_places_a_call_is_given(tmp_path):
    """A hidden entry that is a link no session could have left (outside every place one writes) has what it leads
    to covered, inside a place the session reads or writes too; one a session could have left covers nothing, so a
    link to the system leaves the call running; a hidden path is covered under the link's name of a place the
    sandbox shows it by; a place given by a link's name below a hidden directory comes back under both names; a
    place the call is given and keeps stays visible when it is also hidden."""
    import subprocess
    from hermit.sandbox import Sandbox
    ws = tmp_path / "ws"
    (ws / "answers").mkdir(parents=True)
    (ws / "answers" / "a.txt").write_text("wans\n")
    (ws / "mine.txt").write_text("mine\n")
    data = tmp_path / "data"                               # a place the session reads
    (data / "answers").mkdir(parents=True)
    (data / "answers" / "a.txt").write_text("rans\n")
    conf = tmp_path / "conf"                               # configured links, where no session writes
    conf.mkdir()
    (conf / "to-data").symlink_to(data / "answers")
    (conf / "to-ws").symlink_to(ws / "answers")
    for i, target in enumerate(["/usr", "/lib64" if os.path.exists("/lib64") else "/lib"]):
        (ws / f"planted{i}").symlink_to(target)            # what a session could leave where it writes
    real = tmp_path / "real"                               # shown by the sandbox under a link's name
    real.mkdir()
    (real / "secret.txt").write_text("s\n")
    (tmp_path / "alias").symlink_to(real)
    hid = tmp_path / "hid"                                 # a place given by a link's name below a hidden directory
    (hid / "run" / "ws_real").mkdir(parents=True)
    (hid / "run" / "ws_real" / "f.txt").write_text("f\n")
    (hid / "run" / "wslink").symlink_to(hid / "run" / "ws_real")
    (hid / "other.txt").write_text("o\n")
    task = tmp_path / "task_info"
    task.mkdir()
    (task / "t.md").write_text("task\n")
    box = Sandbox(network="open", writable=[ws, hid / "run" / "wslink"], readable=[data, tmp_path / "alias"])
    hiding = HidingSandbox(box, [conf / "to-data", conf / "to-ws", ws / "planted0", ws / "planted1", real,
                                 hid, task], keep=[task])

    def shows(*paths) -> list[str]:
        script = "; ".join(f"cat {p} 2>/dev/null || echo ABSENT" for p in paths)
        args, env = hiding.wrap(["sh", "-c", script], env=dict(os.environ), readable=[task])
        out = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return out.stdout.split()
    assert shows(data / "answers" / "a.txt", ws / "answers" / "a.txt", ws / "mine.txt") == ["ABSENT", "ABSENT", "mine"]
    assert shows(tmp_path / "alias" / "secret.txt", real / "secret.txt") == ["ABSENT", "ABSENT"]
    assert shows(hid / "run" / "wslink" / "f.txt", hid / "run" / "ws_real" / "f.txt", hid / "other.txt") == \
        ["f", "f", "ABSENT"]
    assert shows(task / "t.md") == ["task"]
    # a session that moved a hidden directory away and left a link at its name: what the link leads to is covered
    (ws / "a" / "secret").mkdir(parents=True)
    (ws / "a" / "secret" / "x.txt").write_text("moved\n")
    hiding.hidden.append(ws / "a" / "secret")
    (ws / "a").rename(ws / "b")
    (ws / "a").symlink_to(ws / "b")
    assert shows(ws / "a" / "secret" / "x.txt", ws / "b" / "secret" / "x.txt") == ["ABSENT", "ABSENT"]


@pytest.mark.skipif(not __import__("shutil").which("bwrap"), reason="needs bubblewrap")
def test_what_a_session_leaves_never_widens_or_stops_the_next_call(tmp_path):
    """Links a session leaves where it writes, back into the place itself, and a pyvenv.cfg naming them, neither
    show anything nor make the hiding grow (the call starts at once); a place given with a link to it inside another
    place given below a hidden directory comes back once, through that place."""
    import subprocess
    import time
    from hermit.sandbox import Sandbox
    w = tmp_path / "w"
    w.mkdir()
    private = tmp_path / "private"
    (private / "h").mkdir(parents=True)
    (private / "h" / "x").write_text("private\n")
    (w / "f").symlink_to(private)
    lines = [f"home = {w / 'f' / 'bin'}"]
    for i in range(8):
        (w / f"g{i}").symlink_to(w)
        lines.append(f"home = {w / f'g{i}' / 'bin'}")
    (w / "pyvenv.cfg").write_text("\n".join(lines) + "\n")
    start = time.time()
    args, env = HidingSandbox(Sandbox(network="open", writable=[w]), [private / "h"]).wrap(
        ["sh", "-c", f"cat {private}/h/x 2>/dev/null || echo ABSENT; echo STARTED"], env=dict(os.environ))
    assert time.time() - start < 10 and args.count("--tmpfs") < 20
    out = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.split() == ["ABSENT", "STARTED"], out.stderr
    hid = tmp_path / "H"
    (hid / "run" / "ws_real").mkdir(parents=True)
    (hid / "run" / "ws_real" / "f.txt").write_text("f\n")
    (hid / "run" / "wslink").symlink_to(hid / "run" / "ws_real")
    (hid / "other.txt").write_text("o\n")
    args, env = HidingSandbox(Sandbox(network="open"), [hid]).wrap(
        ["sh", "-c", f"cat {hid}/run/wslink/f.txt; cat {hid}/other.txt 2>/dev/null || echo ABSENT"],
        env=dict(os.environ), writable=[hid / "run", hid / "run" / "wslink"])
    out = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.split() == ["f", "ABSENT"], out.stderr


@pytest.mark.skipif(not __import__("shutil").which("bwrap"), reason="needs bubblewrap")
def test_nested_places_links_and_system_paths_under_hiding(tmp_path):
    """A place the session is given below a hidden directory stays, and what is hidden inside it stays covered; a
    hidden entry that is a link to the session's own workspace leaves the workspace as it is; a hidden file under
    /usr (bound whole by the sandbox) is covered."""
    import subprocess
    from hermit.sandbox import Sandbox
    hidden = tmp_path / "examples"
    work = hidden / "run" / "ws"
    work.mkdir(parents=True)
    (work / "mine.txt").write_text("mine\n")
    (work / "secret.txt").write_text("s\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "planted").symlink_to(work)
    system = next((p for p in sorted(__import__("pathlib").Path("/usr/share").rglob("*"))[:5000]
                   if p.is_file() and not p.is_symlink() and os.access(p, os.R_OK) and p.stat().st_size > 0), None)
    hiding = HidingSandbox(Sandbox(network="open", writable=[work, scratch]),
                           [hidden, work / "secret.txt", scratch / "planted", *([system] if system else [])])

    def shows(path) -> str:
        args, env = hiding.wrap(["sh", "-c", f"head -c 64 {path} 2>/dev/null | head -1 | grep . || echo ABSENT"],
                                env=dict(os.environ))
        out = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()
    assert shows(work / "mine.txt") == "mine"
    assert shows(work / "secret.txt") == "ABSENT"
    if system is not None:
        assert shows(system) == "ABSENT"
