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
import types
import typing
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
    bool, list, dict, or that type or None) its type (a list's item type its items), the ones without a default
    required. `self` and `_task` (the
    task row, bound by the framework) are not arguments."""
    properties, required = {}, []
    for p in inspect.signature(fn).parameters.values():
        if p.name in ("self", "_task") or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        properties[p.name] = _property(p.annotation)
        if p.default is p.empty:
            required.append(p.name)
    return {"type": "object", "properties": properties, **({"required": required} if required else {})}


def _property(annotation) -> dict:
    """An argument's schema: its JSON type, a list's items too (a provider refuses an array without them); {} for an
    annotation that names none of the JSON types."""
    hint = _type_name(annotation)
    if hint not in JSON_TYPES:
        return {}
    if hint != "list":
        return {"type": JSON_TYPES[hint]}
    item = _item(annotation)
    return {"type": "array", "items": _property(item) if item is not None else {}}


def _item(annotation):
    """The item type of a list annotation (`list[str]`, `list[str] | None`, `"list[int]"`), or None."""
    if isinstance(annotation, str):
        text = annotation.replace("Optional[", "").replace(" ", "")
        start = text.find("list[")
        if start < 0:
            return None
        inner, depth = "", 0
        for ch in text[start + 5:]:
            if ch == "]" and depth == 0:
                break
            depth += (ch == "[") - (ch == "]")
            inner += ch
        return inner or None
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        rest = [a for a in args if a is not type(None)]
        return _item(rest[0]) if len(rest) == 1 else None
    return args[0] if origin is list and args else None


def _type_name(annotation) -> str:
    """The plain type an annotation names, its None dropped: `int | None`, `Optional[int]` and `"int | None"` are
    `int`; `list[str]` is `list`."""
    if isinstance(annotation, str):                     # a postponed annotation (from __future__ import annotations)
        parts = [s.strip() for s in annotation.replace("Optional[", "").rstrip("]").split("|")]
        parts = [s for s in parts if s not in ("None", "")]
        return parts[0].split("[")[0] if len(parts) == 1 else ""
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        rest = [a for a in args if a is not type(None)]
        return _type_name(rest[0]) if len(rest) == 1 else ""
    return getattr(origin or annotation, "__name__", "")


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
