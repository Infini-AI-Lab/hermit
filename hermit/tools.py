"""The Evolver's tools: declaration, assembly from three sources (template, task, run), metering, delivery.

check_spec is always present. Template tools come from .hermit/adapter.yaml `evolver_tools`, task tools from the run config's
`evolver_access.tools`, run tools are `budget` and (when the run config has an `llm` section) `llm`. Every call is
logged to the run's tool log. The run's model reaches an Evolver as a Base Agent too (`ctx.agent`, a metered
`direct`; hermit.agent.evolver.base.ask), and the `llm` tool is that agent for programs outside the process. Delivery: in-process (`ctx.tools`) for class Evolvers, and an HTTP endpoint that the
CLI agents (MCP) and external programs (JSON) use: hermit.agent.base.tool_server.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import inspect
import json
from pathlib import Path
from typing import Callable

from hermit.agent.base.tool import Tool, ToolError, _clip   # noqa: F401 (ToolError: raised by the Trainer's tools)
from hermit.agent.evolver.base import ask
from hermit.agent.base.tool_server import ToolServer   # noqa: F401 (the Trainer's endpoint)


FunctionTool = Tool          # the Trainer's tools are ordinary Tools (hermit.agent.base.tool)


def _load_entry(entrypoint: str, base: Path) -> Callable:
    filename, _, function = entrypoint.partition(":")
    path = (Path(base) / filename).resolve()
    spec = importlib.util.spec_from_file_location(f"_tool_{path.stem}_{abs(hash(path))}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, function, None)
    if not callable(fn):
        raise ValueError(f"Tool entrypoint is not callable: {entrypoint}")
    return fn


def declared(decl: dict, base: Path, source: str, bound: dict, runner=None) -> FunctionTool:
    """A tool declared like an adapter tool: name, description, entrypoint, input_schema, output_schema (none, or
    an empty one, asks for a JSON object back).
    Keyword arguments the function declares among `bound` (for example `_adapter`) are filled by the framework.
    With a `runner` (the `tool` role's sandbox, isolate.ToolRunner) the code is never imported here: every call runs
    in a sandboxed worker."""
    if runner is not None:
        def call(**arguments):
            return runner(decl["entrypoint"], base, arguments, bound)
        return FunctionTool(decl["name"], decl["description"], call, source=source,
                            input_schema=decl.get("input_schema"), output_schema=decl.get("output_schema") or None)
    fn = _load_entry(decl["entrypoint"], base)
    params = inspect.signature(fn).parameters
    extra = {k: v for k, v in bound.items() if k in params}

    def call(**arguments):
        return fn(**arguments, **extra)
    return FunctionTool(decl["name"], decl["description"], call, source=source,
                        input_schema=decl.get("input_schema"), output_schema=decl.get("output_schema") or None)


class MeteredModel:
    """The run's model as the Evolver's Base Agent calls it: every call is refused once the Trainer stops the
    Evolver (`cancel`) or the budget is spent, charged to the budget's evolver share, kept in `metered` and logged
    to `log_path` as an `llm` call."""

    def __init__(self, client, budget, metered: list | None = None, cancel=None, log_path: Path | None = None):
        self.client, self.budget, self.metered, self.cancel, self.log_path = client, budget, metered, cancel, log_path

    @property
    def model(self) -> str:
        return self.client.model

    @property
    def total(self):
        return self.client.total

    def chat(self, messages: list, **kwargs):
        record = {"time": datetime.now(timezone.utc).isoformat(), "tool": "llm", "source": "run",
                  "arguments": _clip({"messages": messages, **kwargs})}
        try:
            if self.cancel is not None and self.cancel.is_set():
                raise ToolError("the call was stopped by the Trainer (budget or timeout)")
            self.budget.check()
            message, usage = self.client.chat(messages, **kwargs)
            self.budget.add("evolver", usage)
            if self.metered is not None:
                self.metered.append(usage)
            record.update(status="ok", result=_clip({"content": message.get("content") or "",
                                                     "reasoning": message.get("reasoning_content") or "",
                                                     "finish_reason": message.get("finish_reason"),
                                                     "usage": usage.__dict__}))
            return message, usage
        except Exception as exc:
            record.update(status="error", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if self.log_path is not None:
                with open(self.log_path, "a") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def evolver_agent(client, budget, metered: list | None = None, cancel=None, log_path: Path | None = None):
    """The Evolver's `direct` Base Agent (`ctx.agent`): the run's model, metered."""
    from hermit.agent.base.direct import Direct
    return Direct({}, MeteredModel(client, budget, metered, cancel, log_path))


def assemble(*, adapter: Path, root: Path, template_decls: list, task_decls: list, task_base: Path,
             budget, agent=None, python: str | None = None, runner=None,
             within: list[Path] = ()) -> dict[str, FunctionTool]:
    """One toolset for one call. `adapter` is the workspace copy the tools act on; `root` the template's files;
    `within` are the learner's other writable directories (scratch, state), where an Evolver keeps a candidate.
    `agent` is the call's metered `direct` Base Agent (evolver_agent); the `llm` tool calls it."""
    from hermit.spec import check_spec
    tools: dict[str, FunctionTool] = {}
    checkable = [Path(p).resolve() for p in (adapter, *within)]

    def check(path: str | None = None) -> dict:
        target = Path(path) if path else adapter
        # The learner's own work only: its working copy, or a candidate it wrote in its scratch or state directory.
        if not any(target.resolve().is_relative_to(c) for c in checkable):
            raise ValueError("check_spec checks your working copy of the adapter, or a copy under your scratch or "
                             "state directory")
        return check_spec(target, root, python=python).to_dict()
    tools["check_spec"] = FunctionTool(
        "check_spec", "Check whether the adapter (default: your working copy) conforms to its template's "
        "specification and deploys. Returns {passed, issues: [{path, check, problem}]}.", check, source="template",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False})
    bound = {"_adapter": adapter, "_root": root}
    for decl in template_decls or []:
        tools[decl["name"]] = declared(decl, root, "template", bound, runner)
    for decl in task_decls or []:
        tools[decl["name"]] = declared(decl, task_base, "task", bound, runner)

    def remaining() -> dict:
        return {"remaining": budget.remaining(), "spent": budget.snapshot()}
    tools["budget"] = FunctionTool("budget", "The run's remaining hard budget (usd, tokens, hours) and spending so far.",
                                   remaining, source="run",
                                   input_schema={"type": "object", "additionalProperties": False})
    if agent is not None:
        def llm(messages: list, max_tokens: int | None = None, temperature: float | None = None) -> dict:
            return ask(agent, messages, max_tokens=max_tokens, temperature=temperature)
        tools["llm"] = FunctionTool(
            "llm", f"Call the run's model ({agent.model.model}) with chat messages; metered against the budget.", llm,
            source="run", input_schema={"type": "object", "required": ["messages"], "additionalProperties": False,
                                        "properties": {"messages": {"type": "array"},
                                                       "max_tokens": {"type": "integer"},
                                                       "temperature": {"type": "number"}}})
    return tools
