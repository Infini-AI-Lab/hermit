"""Task tools for the built-in agents: plain Python functions marked with @tool.

    from hermit.agent.acting import tool

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
import json
from pathlib import Path
import sys
from typing import Callable

from hermit.agent.base.tool import Tool

MARK = "__harness_tool__"


def tool(description: str | Callable | None = None, parameters: dict | None = None, *,
         name: str | None = None) -> Callable:
    """Mark a function (or an agent's method: hermit.agent.acting.react.ReactAgent) as a task tool. `description`
    defaults to its docstring; `parameters` is the JSON Schema of its arguments (an object), by default read from its
    signature (`schema_of`). `@tool` alone marks it with both defaults."""
    if callable(description):                     # @tool, without arguments
        return tool()(description)
    if parameters is not None and parameters.get("type") != "object":
        raise ValueError("A tool's parameters schema must be an object")

    def mark(fn):
        text = description or (inspect.getdoc(fn) or "").split("\n\n")[0].strip() or fn.__name__
        setattr(fn, MARK, {"name": name or fn.__name__, "description": text,
                           "parameters": parameters or schema_of(fn)})
        return fn
    return mark


JSON_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array",
              "dict": "object"}


def schema_of(fn: Callable) -> dict:
    """The JSON Schema of a function's arguments from its signature: each parameter's annotation (str, int, float,
    bool, list, dict, or that type or None) its type, the ones without a default required. `self` and `_task` (the
    task row, bound by the framework) are not arguments."""
    properties, required = {}, []
    for p in inspect.signature(fn).parameters.values():
        if p.name in ("self", "_task") or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        hint = p.annotation if isinstance(p.annotation, str) else getattr(p.annotation, "__name__", "")
        hint = hint.replace(" ", "").replace("|None", "").replace("Optional[", "").rstrip("]")
        hint = hint.split("[")[0]
        properties[p.name] = {"type": JSON_TYPES[hint]} if hint in JSON_TYPES else {}
        if p.default is p.empty:
            required.append(p.name)
    return {"type": "object", "properties": properties, **({"required": required} if required else {})}


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


def as_tools(found: list[tuple[dict, Callable]], task: dict) -> list[Tool]:
    """Loaded @tool functions as Tools for one task: `_task` bound, any JSON result, read as its JSON text."""
    return [Tool(spec["name"], spec["description"], _bound(fn, task), source="task",
                 input_schema=spec["parameters"], output_schema={}, validate=False,
                 render=lambda result: json.dumps(result, ensure_ascii=False)) for spec, fn in found]


def _bound(fn: Callable, task: dict) -> Callable:
    return lambda **arguments: invoke(fn, arguments, task)


def invoke(fn: Callable, arguments: dict, task: dict):
    """Call a task tool with the model's arguments; `_task` is bound when the function declares it."""
    if "_task" in inspect.signature(fn).parameters:
        arguments = {**arguments, "_task": task}
    return fn(**arguments)
