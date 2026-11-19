"""The one agent abstraction (docs/framework/agent-layers.md): environments, the single model call in Layer 1, the
ReAct agent's hooks and step-by-step driving, @tool methods, and what a method needs of the acting agent. Stand-ins:
a scripted chat model; bubblewrap where a test needs the run's sandbox (skipped without it)."""
import json
from pathlib import Path
import shutil
import sys

import pytest

from hermit.agent.base.interface import Unsupported, make
from hermit.environment import Container, Limits, Local, Sandboxed, as_environment, placed
from hermit.agent.model.usage import Usage


class Model:
    """A chat model whose replies are scripted; it records each request."""
    model = "scripted"

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def chat(self, messages, **kwargs):
        self.requests.append({"messages": [dict(m) for m in messages], **kwargs})
        message = dict(self.replies.pop(0)) if self.replies else {"content": "done"}
        message.setdefault("role", "assistant")
        return message, Usage("scripted", input_tokens=3, output_tokens=2, calls=1)

    @property
    def total(self):
        n = len(self.requests)
        return Usage("scripted", input_tokens=3 * n, output_tokens=2 * n, calls=n)


def call(name, arguments, id="c1"):
    return {"content": None, "tool_calls": [{"id": id, "type": "function",
                                             "function": {"name": name, "arguments": json.dumps(arguments)}}]}


# ---------------------------------------------------------------------------------------------- environments
def test_a_local_environment_runs_commands_and_moves_files_inside_its_workspace(tmp_path):
    env = Local(tmp_path / "w")
    env.workspace.mkdir()
    (tmp_path / "in.txt").write_text("hello")
    env.upload(tmp_path / "in.txt", "sub/in.txt")
    done = env.exec(["sh", "-c", "cat in.txt; echo $GREETING >&2"], cwd="sub", env={"GREETING": "hi"})
    assert (done.code, done.output) == (0, "hello" + "hi\n")
    env.download("sub/in.txt", tmp_path / "out.txt")
    assert (tmp_path / "out.txt").read_text() == "hello"
    with pytest.raises(ValueError, match="outside the workspace"):
        env.path("../escape")
    assert env.exec(["sh", "-c", "sleep 5"], timeout=0.2).timed_out


def test_a_path_is_a_local_environment_and_placed_picks_the_runs_sandbox(tmp_path):
    assert isinstance(as_environment(tmp_path), Local) and as_environment(None) is None
    env = Local(tmp_path)
    assert as_environment(env) is env
    with pytest.raises(TypeError):
        as_environment(3)
    assert placed(tmp_path).isolation == "none"
    box = object()
    sandboxed = placed(tmp_path, box, Limits(timeout_s=9))
    assert (sandboxed.isolation, sandboxed.sandbox, sandboxed.limits.timeout_s) == ("sandbox", box, 9)
    assert Container(tmp_path, settings={"image": "x"}).container == {"image": "x"}
    with pytest.raises(ValueError, match="needs the run's sandbox"):
        Sandboxed(tmp_path, sandbox=None)
    narrowed = env.with_limits(Limits(timeout_s=1))
    assert narrowed.limits.timeout_s == 1 and env.limits.timeout_s is None      # the original is unchanged


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="needs bubblewrap")
def test_a_sandboxed_environment_writes_only_its_workspace(tmp_path):
    from hermit.sandbox import Sandbox
    work, outside = tmp_path / "w", tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    env = Sandboxed(work, sandbox=Sandbox(enabled=True, enforce="required", network="isolated"))
    assert env.exec(["sh", "-c", "echo ok > mine.txt"]).code == 0 and (work / "mine.txt").read_text() == "ok\n"
    refused = env.exec(["sh", "-c", f"echo no > {outside}/theirs.txt"])
    assert refused.code != 0 and not (outside / "theirs.txt").exists()


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="needs bubblewrap")
def test_a_sandboxed_command_reads_its_mounts_read_only_within_its_clock(tmp_path):
    from hermit.environment import Mount
    from hermit.sandbox import Sandbox
    data, work = tmp_path / "data", tmp_path / "w"
    data.mkdir()
    work.mkdir()
    (data / "fact.txt").write_text("42")
    env = Sandboxed(work, Limits(readable=[Mount(data, Path("docs"))], timeout_s=1),
                    sandbox=Sandbox(enabled=True, enforce="required", network="isolated"))
    assert env.exec(["cat", "docs/fact.txt"]).output == "42"
    assert env.exec(["sh", "-c", "echo x > docs/new.txt"]).code != 0 and not (data / "new.txt").exists()
    assert env.exec(["sleep", "10"]).timed_out


def test_a_cli_agent_takes_its_isolation_from_the_environment_never_from_its_settings(tmp_path):
    for key, value in (("isolation", "sandbox"), ("sandbox", object()), ("container", {"image": "x"})):
        with pytest.raises(ValueError, match=f"{key} belong to the call's environment"):
            make("codex", {"web_search": False, key: value}).run("go", environment=tmp_path)
    with pytest.raises(Unsupported, match="give it an environment"):
        make("codex", {"web_search": False}).run("go")


# ---------------------------------------------------------------------------------------------- Layer 1
def test_a_single_model_call_is_layer_one_not_an_agent():
    from hermit.agent.model.direct import Direct
    model = Model({"content": "hi", "finish_reason": "stop"})
    reply = Direct({"max_tokens": 8}, model).run("q", session=[{"role": "system", "content": "s"}])
    assert (reply.reply, reply.finish_reason, reply.session[-1]["role"]) == ("hi", "stop", "assistant")
    assert model.requests[0]["max_tokens"] == 8 and model.requests[0]["messages"][0]["role"] == "system"
    with pytest.raises(ValueError, match="not a Base Agent"):
        make("direct")
    with pytest.raises(TypeError):
        Direct({}, model).run("q", environment=Path("."))         # it works in no environment


# ---------------------------------------------------------------------------------------------- the ReAct loop
def test_react_hooks_change_the_loop_without_copying_it():
    from hermit.agent.base.react import React
    from hermit.agent.base.tool import Tool
    ran = []
    echo = Tool("echo", "Echo.", lambda text: ran.append(text) or text, validate=False, output_schema={}, render=str)

    class Capped(React):
        def before_tool(self, loop, call):
            return "refused" if loop.calls_made > 1 else None

        def after_tool(self, loop, call, content):
            return content.upper()

        def stopped(self, loop, message):
            return loop.turns >= 3

    model = Model(call("echo", {"text": "a"}), call("echo", {"text": "b"}, "c2"), call("echo", {"text": "c"}, "c3"))
    result = Capped({"max_turns": 9}, model).run("go", tools=[echo])
    assert ran == ["a"]                                           # the second and third calls never ran
    tools = [m["content"] for m in result.session if m["role"] == "tool"]
    assert tools == ["A", "refused", "refused"] and result.extra["turns"] == 3


def test_a_driver_runs_the_external_calls_itself_step_by_step():
    from hermit.agent.base.react import React
    model = Model(call("submit", {"answer": "42"}), {"content": "thanks"})
    agent = React({}, model)
    loop = agent.start("go", tools=[], external=["submit"])
    agent.step(loop)
    assert [c["function"]["name"] for c in loop.pending] == ["submit"] and not loop.done
    agent.step(loop)                                              # waits for the driver
    assert len(model.requests) == 1
    agent.give(loop, "c1", "accepted")
    agent.step(loop)
    assert loop.done and loop.result().reply == "thanks"
    assert model.requests[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "accepted"}
    with pytest.raises(ValueError, match="no pending call"):
        agent.give(loop, "c9", "late")


def test_a_messages_calls_run_in_the_order_asked_when_one_of_them_is_external():
    """A local call after an external one waits for its result (it may read what the external call changed), and the
    turn's ends are judged after the last result: an external submission can end the run."""
    from hermit.agent.base.react import React
    from hermit.agent.base.tool import Tool
    state = {"position": 0}
    look = Tool("look", "Where am I?", lambda: str(state["position"]), validate=False, output_schema={}, render=str)

    class Game(React):
        def stopped(self, loop, message):
            return any(m.get("content") == "submitted" for m in loop.messages if m["role"] == "tool")

    calls = [{"id": i, "type": "function", "function": {"name": n, "arguments": "{}"}}
             for i, n in (("m", "move"), ("l", "look"), ("s", "submit"))]
    model = Model({"content": None, "tool_calls": calls})
    agent = Game({}, model)
    loop = agent.start("go", tools=[look], external=["move", "submit"])
    agent.step(loop)
    assert [c["id"] for c in loop.pending] == ["m"]                   # look waits behind the move
    state["position"] = 1
    agent.give(loop, "m", "moved")
    assert [c["id"] for c in loop.pending] == ["s"]
    agent.give(loop, "s", "submitted")
    tools = [(m["tool_call_id"], m["content"]) for m in loop.messages if m["role"] == "tool"]
    assert tools == [("m", "moved"), ("l", "1"), ("s", "submitted")]
    assert loop.done and len(model.requests) == 1                     # no model call after the submission


def test_an_external_call_goes_through_the_same_hooks_as_one_the_loop_runs():
    """before_tool can answer an external call instead of letting it wait (a cap reached), and the result a driver
    gives is cut at tool_result_chars and passed through after_tool."""
    from hermit.agent.base.react import React

    class Capped(React):
        def before_tool(self, loop, call):
            return "cap reached" if loop.calls_made > 1 else None

        def after_tool(self, loop, call, content):
            return f"[{content}]"

    model = Model(call("submit", {"a": 1}), call("submit", {"a": 2}, "c2"), {"content": "done"})
    agent = Capped({"tool_result_chars": 4}, model)
    loop = agent.start("go", external=["submit"])
    agent.step(loop)
    agent.give(loop, "c1", "accepted-and-more")
    agent.step(loop)                                              # the second call is capped, never pending
    assert not loop.pending
    agent.step(loop)
    tools = [m["content"] for m in loop.messages if m["role"] == "tool"]
    assert tools == ["[acce]", "cap reached"] and loop.done


def test_a_benchmark_agent_is_a_react_subclass_with_tool_methods_and_hooks(tmp_path, monkeypatch):
    from hermit.agent.acting.react import ReactAgent, tool
    from hermit.agent.model import llm
    model = Model(call("lookup", {"key": "x"}), {"content": "<answer>X</answer>"})
    monkeypatch.setattr(llm.ChatClient, "from_spec", classmethod(lambda cls, spec, **kw: model))
    seen = {}

    class Benchmark(ReactAgent):
        @tool
        def lookup(self, key: str, _task=None) -> dict:
            """Look a key up."""
            seen["task"] = _task["id"]
            seen["env"] = self.environment
            return {"value": key.upper()}

        def environment_for(self, task):
            return Local(tmp_path)

        def system_prompt(self, task, context):
            return f"Benchmark rules. {context}"

        def answer(self, result, task):
            return result.reply.removeprefix("<answer>").removesuffix("</answer>")

    agent = Benchmark({"model": {"base_url": "http://x.invalid/v1", "model": "m"}, "logs": "logs"}, tmp_path)
    out = agent.run({"id": "t1", "question": "what is x?"}, None)
    assert out.output == "X" and seen["task"] == "t1" and seen["env"].workspace == tmp_path
    first = model.requests[0]
    assert first["messages"][0]["content"] == "Benchmark rules. "
    spec = first["tools"][0]["function"]
    assert spec["name"] == "lookup" and spec["description"] == "Look a key up."
    assert spec["parameters"] == {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}
    assert agent.environment is None                              # each task's agent is a copy
    assert Benchmark.supports == ("context", "context_tool", "tools")


def test_tool_reads_its_schema_from_the_signature():
    from hermit.agent.acting.tools import MARK, tool

    @tool
    def search(query: str, limit: int = 8, exact: "bool | None" = None, _task=None) -> list:
        """Search the corpus.

        Long explanation."""
    assert getattr(search, MARK) == {"name": "search", "description": "Search the corpus.", "parameters": {
        "type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"},
                                         "exact": {"type": "boolean"}}, "required": ["query"]}}
    with pytest.raises(ValueError, match="must be an object"):
        tool("x", {"type": "array"})


# ---------------------------------------------------------------------------------------------- what a method needs
def _config(tmp_path, agent: dict, evolver: str, runner_host=True):
    import yaml
    from hermit.config import Config
    (tmp_path / "tasks.jsonl").write_text('{"id": "a", "question": "q"}\n')
    (tmp_path / "train.txt").write_text("a\n")
    data = {"data": {"tasks": "tasks.jsonl", "train": "train.txt"}, "agent": agent, "template": "prompt-only@1.0",
            "judge": {"path": "judge.py"}, "evolver": {"name": evolver},
            "environment": {"runner": "host"} if runner_host else {}, "llm": None}
    (tmp_path / "run.yaml").write_text(yaml.safe_dump(data))
    return Config.load(tmp_path / "run.yaml")


def test_a_method_that_needs_tools_is_refused_an_agent_that_takes_none(tmp_path):
    from hermit.trainer.trainer import adapter_needs_errors
    (tmp_path / "agent.py").write_text(
        "from hermit.agent.acting import Agent\n\nclass Plain(Agent):\n    supports = ('context',)\n\n"
        "class Silent(Agent):\n    pass\n")
    plain = _config(tmp_path, {"name": "agent.py:Plain"}, "mwm")
    assert adapter_needs_errors(plain, ["environment", "tools"]) == [
        "the Evolver needs its adapter to give the acting agent tools, which agent agent.py:Plain does not take "
        "(it takes context)"]
    assert adapter_needs_errors(plain, ["llm"]) == []                       # needs nothing of the agent
    assert adapter_needs_errors(_config(tmp_path, {"name": "agent.py:Silent"}, "mwm"), ["tools"]) == []
    react = _config(tmp_path, {"name": "react", "params": {"model": {"base_url": "u", "model": "m"}}}, "ahe_adapter")
    assert "skills, agents, hooks" in adapter_needs_errors(react, ["skills", "agents", "hooks"])[0]
    codex = _config(tmp_path, {"name": "codex", "params": {"web_search": False}}, "ahe_adapter")
    assert adapter_needs_errors(codex, ["skills", "agents", "hooks"]) == []
    # a task's own class that would run only in its sandbox is never imported here, so it is not checked
    sandboxed = _config(tmp_path, {"name": "agent.py:Plain"}, "mwm", runner_host=False)
    assert adapter_needs_errors(sandboxed, ["tools"]) == []


def _declared_supports(file: Path, name: str):
    """A task class's or function's `supports`, read from its source without importing it (None: not declared)."""
    import ast
    tree = ast.parse(file.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and any(getattr(t, "id", None) == "supports" for t in stmt.targets):
                    return tuple(ast.literal_eval(stmt.value))
            return None
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "supports" and
                                                getattr(t.value, "id", None) == name for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    return None


def test_no_shipped_config_runs_a_method_on_an_agent_that_lacks_what_it_needs():
    """Every example config of a method that needs something of the acting agent names an agent that takes it."""
    import yaml
    from hermit.agent.acting.agent import supports_of
    from hermit.agent.evolver.loader import ADAPTER_NEEDS, find
    root = Path(__file__).resolve().parents[2]
    checked = 0
    for path in sorted((root / "examples").rglob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        evolver = (data or {}).get("evolver") if isinstance(data, dict) else None
        if not isinstance(evolver, dict) or not isinstance(data.get("agent"), dict):
            continue
        try:
            needs = set(find(str(evolver.get("name")), path.parent).needs) & ADAPTER_NEEDS
        except Exception:                                   # an eval-only placeholder, a custom Evolver
            continue
        if not needs:
            continue
        agent = data["agent"]
        name = str(agent.get("name") or agent.get("base"))
        supports = supports_of(name, path.parent, agent.get("params"), may_import=False)
        if supports is None and ":" in name:
            file, _, symbol = name.partition(":")
            supports = _declared_supports((path.parent / file).resolve(), symbol)
        assert supports is not None and needs <= set(supports), f"{path.relative_to(root)}: {name} lacks {needs}"
        checked += 1
    assert checked >= 10                                    # MWM, ASPIRE, ReHarness and AHE's examples


def test_harbor_takes_what_its_acting_agent_takes(tmp_path):
    from hermit.agent.acting.agent import supports_of
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    for acting, expected in (({"cli": "codex"}, "all"), ({"cli": "answer"}, ("context", "context_tool")),
                             ({"name": "oracle"}, ())):
        supports = supports_of("harbor", tmp_path, {"tasks_dir": str(tasks), "acting_agent": acting},
                               may_import=False)
        assert (len(supports) == 6 and "hooks" in supports) if expected == "all" else supports == expected, acting


def test_a_download_keeps_a_link_as_the_link_it_is(tmp_path):
    env = Local(tmp_path / "w")
    env.workspace.mkdir()
    (tmp_path / "secret.txt").write_text("outside")
    (env.workspace / "link").symlink_to(tmp_path / "secret.txt")
    out = env.download("link", tmp_path / "out" / "link")
    assert out.is_symlink() and Path(out.readlink()) == tmp_path / "secret.txt"


def test_a_local_command_stays_within_the_environments_limits(tmp_path):
    from hermit.environment import Mount, Unsupported
    data = tmp_path / "data"
    data.mkdir()
    (data / "fact.txt").write_text("42")
    work = tmp_path / "w"
    work.mkdir()
    env = Local(work, Limits(readable=[Mount(data, Path("docs"))], timeout_s=0.3))
    assert env.exec(["cat", "docs/fact.txt"]).output == "42"            # the mount is in place
    assert env.exec(["sleep", "5"]).timed_out                              # the limits' clock
    for limits in (Limits(hidden=[tmp_path / "x"]), Limits(network=[]), Limits(readable=[Mount(data, Path("/abs"))])):
        with pytest.raises(Unsupported):
            Local(work, limits).exec(["true"])


def test_a_timed_out_command_leaves_no_child_running(tmp_path):
    import os
    import time
    env = Local(tmp_path)
    done = env.exec(["sh", "-c", "sleep 30 & echo $! > child.pid; wait"], timeout=0.5)
    child = int((tmp_path / "child.pid").read_text())
    time.sleep(0.2)
    assert done.timed_out
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_a_runtime_optional_annotation_gives_its_type():
    from typing import Optional
    from hermit.agent.acting.tools import schema_of

    def lookup(key: str, limit: int | None = None, exact: Optional[bool] = None, tags: list[str] | None = None):
        ...
    assert schema_of(lookup)["properties"] == {"key": {"type": "string"}, "limit": {"type": "integer"},
                                               "exact": {"type": "boolean"}, "tags": {"type": "array", "items": {"type": "string"}}}


def test_a_cli_endpoints_sessions_keep_their_isolation_whether_or_not_they_are_logged(tmp_path):
    """llm: {cli: ..., isolation: container} runs each session in a container, with the run's log directory or
    without it; isolation: sandbox in the run's sandbox."""
    from hermit.agent.base.endpoint import cli_endpoint
    from hermit.sandbox import Sandbox
    container = {"image": "img:1", "network": "net", "proxy": "http://px:3128"}
    for log_dir in (None, tmp_path / "sessions"):
        spec = {"cli": "claude", "model": "m", "login": "claude", "isolation": "container", "container": container}
        with cli_endpoint(spec, log_dir) as endpoint:
            placed_in = endpoint.place(tmp_path / "w")
            assert (placed_in.isolation, placed_in.container) == ("container", container)
            assert not {"isolation", "container", "sandbox"} & set(endpoint.agent.settings)
        box = Sandbox(enabled=False, role="llm")
        with cli_endpoint({"cli": "claude", "model": "m", "login": "claude"}, log_dir, sandbox=box) as endpoint:
            assert endpoint.place(tmp_path / "w").sandbox is box


def test_a_sandboxed_command_is_refused_a_limit_its_sandbox_cannot_keep(tmp_path):
    """What the run's sandbox grants (a readable path, an allowed host, a forwarded port) it shows to every command,
    and a sandbox that fell back to running unisolated keeps no limit: such a limit is refused, never ignored."""
    from hermit.environment import Unsupported

    class Box:
        enabled, network, allow, forward, sockets, binds = True, "isolated", (), (), {}, ()
        readable, writable = (), ()

        def __init__(self, **kw):
            self.__dict__.update(kw)

        def wrap(self, command, **kw):
            return list(command), dict(kw["env"])        # best effort without bubblewrap: the command as it is
    work = tmp_path / "w"
    work.mkdir()
    data = tmp_path / "datasets"
    for box, limits in ((Box(readable=[data]), Limits(hidden=[data / "answers.json"])),
                        (Box(allow=["example.com"]), Limits(network=[])),
                        (Box(forward=[8080]), Limits(network=[])),
                        (Box(), Limits(hidden=[tmp_path / "secret"])),          # nothing shows it, but no bwrap ran
                        (Box(), Limits(network=[]))):
        with pytest.raises(Unsupported):
            Sandboxed(work, limits, sandbox=box).exec(["true"])
    assert Sandboxed(work, sandbox=Box()).exec(["true"]).code == 0          # no limit: nothing to keep


def test_a_local_mount_replaces_an_earlier_link_and_refuses_the_workspaces_own_file(tmp_path):
    from hermit.environment import Mount, Unsupported
    old, new, work = tmp_path / "old", tmp_path / "new", tmp_path / "w"
    for d, text in ((old, "old"), (new, "new")):
        d.mkdir()
        (d / "fact.txt").write_text(text)
    work.mkdir()
    Local(work, Limits(readable=[Mount(old, Path("docs"))])).exec(["true"])
    assert Local(work, Limits(readable=[Mount(new, Path("docs"))])).exec(["cat", "docs/fact.txt"]).output == "new"
    (work / "notes").write_text("mine")
    with pytest.raises(Unsupported, match="its own file"):
        Local(work, Limits(readable=[Mount(new, Path("notes"))])).exec(["true"])


def test_a_timed_out_command_leaves_no_child_even_in_a_session_of_its_own(tmp_path):
    import os
    import time
    script = ("import subprocess, sys; "
              "p = subprocess.Popen(['sleep', '30'], start_new_session=True); "
              "open('child.pid', 'w').write(str(p.pid)); print('started', flush=True); p.wait()")
    t = time.time()
    done = Local(tmp_path).exec([sys.executable, "-c", script], timeout=1)
    assert done.timed_out and time.time() - t < 25            # not the 30 s the child would hold the output open
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "child.pid").read_text()), 0)


def test_an_upload_keeps_a_link_to_a_folder_as_the_link_it_is(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "big.bin").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "assets")
    env = Local(tmp_path / "w")
    env.workspace.mkdir()
    out = env.upload(tmp_path / "link", "link")
    assert out.is_symlink() and not (env.workspace / "assets").exists()


def test_a_limited_network_is_refused_where_the_sandbox_has_a_proxy(tmp_path):
    from hermit.environment import Unsupported

    class Box:
        enabled, network, allow, forward, sockets, binds, readable, writable = True, "isolated", (), (), {}, (), (), ()
        proxy_url = "http://127.0.0.1:3128"
    with pytest.raises(Unsupported, match="proxy"):
        Sandboxed(tmp_path, Limits(network=[]), sandbox=Box()).exec(["true"])


def test_a_list_argument_says_what_its_items_are():
    """A provider refuses an array schema without `items`."""
    from typing import Optional
    from hermit.agent.acting.tools import schema_of

    def lookup(paths: list[str], ids: Optional[list[int]] = None, raw: list = (), later: "list[str] | None" = None):
        ...
    assert schema_of(lookup)["properties"] == {
        "paths": {"type": "array", "items": {"type": "string"}}, "ids": {"type": "array", "items": {"type": "integer"}},
        "raw": {"type": "array", "items": {}}, "later": {"type": "array", "items": {"type": "string"}}}


def test_harbor_reads_its_acting_agent_as_the_runner_merges_it(tmp_path):
    from hermit.agent.acting.agent import supports_of
    (tmp_path / "tasks").mkdir()
    (tmp_path / "benchmark.yaml").write_text("acting_agent: {cli: answer}\n")
    given = {"tasks_dir": str(tmp_path / "tasks"), "benchmark": "benchmark.yaml"}
    assert supports_of("harbor", tmp_path, given, may_import=False) == ("context", "context_tool")
    assert "hooks" in supports_of("harbor", tmp_path, {**given, "acting_agent": None}, may_import=False)  # the default


def test_an_endpoint_entered_after_it_was_started_serves_once_and_stops_on_close():
    """cli_endpoint starts its endpoint and callers enter it with `with`: a second serving loop would outlive close()
    and spin on the closed socket, slowing the whole process."""
    import threading
    import time
    from hermit.agent.base.endpoint import ChatEndpoint
    from hermit.agent.model.direct import Direct
    before = {t for t in threading.enumerate() if t.name == "hermit-chat-endpoint"}
    with ChatEndpoint(Direct({}, Model())).start():
        pass
    time.sleep(1.0)                                              # serve_forever's poll interval is 0.5 s
    assert not {t for t in threading.enumerate() if t.name == "hermit-chat-endpoint" and t.is_alive()} - before


def test_a_react_subclass_counts_its_own_model_calls_and_shapes_the_follow_up_answer(tmp_path, monkeypatch):
    """What a subclass's call_model spends on another client is the run's, and answer() shapes the answer tag's
    follow-up too; the tag is looked for in the model's reply, not in what answer() made of it."""
    from hermit.agent.acting.react import ReactAgent
    from hermit.agent.model import llm
    own = Model()                                                 # the params' client: the follow-up only
    other = Model({"content": "<answer>7</answer>"}, {"content": "no tag"})
    monkeypatch.setattr(llm.ChatClient, "from_spec", classmethod(lambda cls, spec, **kw: own))

    class Routed(ReactAgent):
        def call_model(self, loop, messages, specs):
            return other.chat(messages)

        def answer(self, result, task):
            return result.reply.removeprefix("<answer>").removesuffix("</answer>")

    params = {"model": {"base_url": "http://x.invalid/v1", "model": "m"}, "logs": "logs", "answer_tag": "answer"}
    out = Routed(params, tmp_path).run({"id": "t1", "question": "q"}, None)
    assert out.output == "7" and len(own.requests) == 0           # the reply had the tag: no follow-up
    assert out.tokens == 5 and out.api_speed["model_calls"] == 1  # the other client's call counted
    own.replies = [{"content": "<answer>8</answer>"}]
    out = Routed(params, tmp_path).run({"id": "t2", "question": "q"}, None)
    assert out.output == "8" and len(own.requests) == 1           # no tag: one follow-up, through answer()
    assert out.tokens == 10 and out.api_speed["model_calls"] == 2


def test_a_folder_upload_never_writes_through_a_link_in_the_workspace(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    (host / "host.txt").write_text("original")
    env = Local(tmp_path / "w")
    (env.workspace / "out").mkdir(parents=True)
    (env.workspace / "out" / "link").symlink_to(host)             # a link the agent left, to a folder outside
    (env.workspace / "out" / "file").symlink_to(host / "host.txt")
    src = tmp_path / "src"
    (src / "link").mkdir(parents=True)
    (src / "link" / "host.txt").write_text("uploaded")
    (src / "file").write_text("uploaded")
    env.upload(src, "out")
    assert (host / "host.txt").read_text() == "original"
    assert (env.workspace / "out" / "link" / "host.txt").read_text() == "uploaded"
    assert not (env.workspace / "out" / "link").is_symlink() and not (env.workspace / "out" / "file").is_symlink()


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="needs bubblewrap")
def test_a_sandboxed_command_can_start_in_a_folder_only_the_sandbox_shows(tmp_path):
    from hermit.environment import Mount
    from hermit.sandbox import Sandbox
    data, work = tmp_path / "data", tmp_path / "w"
    data.mkdir()
    work.mkdir()
    (data / "fact.txt").write_text("42")
    env = Sandboxed(work, Limits(readable=[Mount(data, Path("docs"))]),
                    sandbox=Sandbox(enabled=True, enforce="required", network="isolated"))
    assert not (work / "docs").exists()                           # bound inside only
    assert env.exec(["cat", "fact.txt"], cwd="docs").output == "42"
