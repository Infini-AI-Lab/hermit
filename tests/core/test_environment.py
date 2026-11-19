"""The one agent abstraction (docs/framework/agent-layers.md): environments, the single model call in Layer 1, the
ReAct agent's hooks and step-by-step driving, @tool methods, and what a method needs of the acting agent. Stand-ins:
a scripted chat model; bubblewrap where a test needs the run's sandbox (skipped without it)."""
import json
from pathlib import Path
import shutil

import pytest

from hermit.agent.base.interface import Unsupported, make
from hermit.agent.environment import Container, Limits, Local, Sandboxed, as_environment, placed
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
    loop.give("c1", "accepted")
    agent.step(loop)
    assert loop.done and loop.result().reply == "thanks"
    assert model.requests[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "accepted"}
    with pytest.raises(ValueError, match="no pending call"):
        loop.give("c9", "late")


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


def test_every_method_needs_only_what_its_example_agents_take():
    """The methods' declared needs against the examples that run them: no shipped config is refused."""
    from hermit.agent.evolver.loader import find
    expected = {"mwm": {"tools"}, "aspire": {"context_tool"}, "reharness": {"context_tool"},
                "ahe_adapter": {"skills", "agents", "hooks"}}
    from hermit.agent.evolver.loader import ADAPTER_NEEDS
    for name, needs in expected.items():
        assert set(find(name, Path.cwd()).needs) & ADAPTER_NEEDS == needs
