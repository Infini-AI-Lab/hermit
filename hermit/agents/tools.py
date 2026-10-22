"""Task tools for the built-in agents: plain Python functions marked with @tool.

    from hermit.agents import tool

    @tool("Full-text search over the bulletins; filenames, line ranges and excerpts.",
          {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
           "required": ["query"]})
    def search(query: str, limit: int = 8) -> dict:
        ...

A function that declares a `_task` parameter receives the task row. Results are JSON (a dict, list, string or
number); an exception becomes an error message the model reads. The module is named in the pipeline's params
(`tools: environment/tools.py`, relative to the config) and loaded once per process.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
from pathlib import Path
import sys
from typing import Callable

MARK = "__harness_tool__"


def tool(description: str, parameters: dict | None = None, *, name: str | None = None) -> Callable:
    """Mark a function as a task tool. `parameters` is the JSON Schema of its arguments (an object)."""
    schema = parameters or {"type": "object", "properties": {}}
    if schema.get("type") != "object":
        raise ValueError("A tool's parameters schema must be an object")

    def mark(fn):
        setattr(fn, MARK, {"name": name or fn.__name__, "description": description, "parameters": schema})
        return fn
    return mark


def load_tools(spec: str, base: Path) -> list[tuple[dict, Callable]]:
    """The @tool functions of a module ('file.py' relative to `base`, or an importable module name), in
    definition order: (spec, function) pairs."""
    if spec.endswith(".py"):
        path = (Path(base) / spec).resolve()
        if not path.is_file():
            raise ValueError(f"tools module not found: {path}")
        mod_name = f"_task_tools_{path.stem}_{abs(hash(path))}"
        mod_spec = importlib.util.spec_from_file_location(mod_name, path)
        module = importlib.util.module_from_spec(mod_spec)
        sys.modules[mod_name] = module
        if str(path.parent) not in sys.path:
            sys.path.insert(0, str(path.parent))
        mod_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(spec)
    found = [(getattr(fn, MARK), fn) for _, fn in vars(module).items() if callable(fn) and hasattr(fn, MARK)]
    found.sort(key=lambda pair: getattr(pair[1], "__code__", None).co_firstlineno if hasattr(pair[1], "__code__") else 0)
    names = [spec["name"] for spec, _ in found]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate tool names in {spec}: {names}")
    return found


def openai_specs(tools: list[tuple[dict, Callable]]) -> list[dict]:
    return [{"type": "function", "function": {"name": s["name"], "description": s["description"],
                                              "parameters": s["parameters"]}} for s, _ in tools]


def invoke(fn: Callable, arguments: dict, task: dict):
    """Call a task tool with the model's arguments; `_task` is bound when the function declares it."""
    if "_task" in inspect.signature(fn).parameters:
        arguments = {**arguments, "_task": task}
    return fn(**arguments)
