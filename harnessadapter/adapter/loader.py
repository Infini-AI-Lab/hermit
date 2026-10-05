"""Load an adapter's exports (adapter.yaml) and its Python tools, without MCP dependencies.

An adapter is trusted executable code. Path and schema validation here are not
an OS sandbox. Evolution workspace isolation is enforced separately by the framework.
"""
import contextlib
import importlib.util
import inspect
import json
from pathlib import Path
import re
import sys
import types
import uuid


def inside(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError("Adapter entries must be relative paths")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Missing or out-of-repo adapter entry: {relative}")
    return path


def load_manifest(root: Path) -> dict:
    """The adapter's exports with its identity (id, name, version), validated; read from adapter.yaml."""
    from harnessadapter.adapter.manifest import flatten, read_manifest
    root = root.resolve()
    manifest = flatten(read_manifest(root))
    context = manifest.get("context")
    if isinstance(context, str):
        inside(root, context)
    elif isinstance(context, dict) and isinstance(context.get("entrypoint"), str):
        filename, separator, function = context["entrypoint"].partition(":")
        if not separator or not function.isidentifier() or inside(root, filename).suffix != ".py":
            raise ValueError("Invalid context renderer entrypoint")
    elif context is not None:
        raise ValueError("context must be a Markdown path or a Python renderer declaration")
    if manifest.get("agents"):
        agents = manifest["agents"]
        if isinstance(agents, str):
            inside(root, agents)
        elif isinstance(agents, dict) and isinstance(agents.get("entrypoint"), str):
            filename, separator, function = agents["entrypoint"].partition(":")
            if not separator or not function.isidentifier():
                raise ValueError("Invalid agent exporter entrypoint")
            inside(root, filename)
        else:
            raise ValueError("agents must be a JSON path or a Python exporter")
    if manifest.get("skills") is not None:
        skills = (root / manifest["skills"]).resolve()
        if not skills.is_relative_to(root):
            raise ValueError("skills must be a directory inside the adapter")
    tools = manifest.get("tools", [])
    if not isinstance(tools, list):
        raise ValueError("tools must be a list")
    names = set()
    for tool in tools:
        name = tool.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or name in names:
            raise ValueError(f"Invalid or duplicate tool name: {name}")
        names.add(name)
        if not isinstance(tool.get("description"), str) or not tool["description"].strip():
            raise ValueError(f"Tool {name} requires a description")
        entry = tool.get("entrypoint", "")
        filename, separator, function = entry.partition(":")
        if not separator or not re.fullmatch(r"[A-Za-z_]\w*", function):
            raise ValueError(f"Invalid Python entrypoint: {entry}")
        if inside(root, filename).suffix != ".py":
            raise ValueError("Python entrypoints must reference .py files")
        for field in ("input_schema", "output_schema"):
            if not isinstance(tool.get(field), dict) or tool[field].get("type") != "object":
                raise ValueError(f"Tool {name} requires an object {field}")
    return manifest


class Adapter:
    def __init__(self, root: Path, resources: dict | None = None):
        from jsonschema import Draft202012Validator

        self.root = root.resolve()
        self.manifest = load_manifest(self.root)
        self.tools = {t["name"]: t for t in self.manifest.get("tools", [])}
        self.functions = {}
        self.resources = dict(resources or {})
        from harnessadapter.runtime.python_sandbox import PythonSandbox
        self.resources.setdefault("python_sandbox", PythonSandbox(self.root))
        self.namespace = "_adapter_" + uuid.uuid4().hex
        self.modules = {}
        package = types.ModuleType(self.namespace)
        package.__path__ = [str(self.root)]
        sys.modules[self.namespace] = package
        modules = {}
        for index, (name, tool) in enumerate(self.tools.items()):
            for field in ("input_schema", "output_schema"):
                schema = tool[field]
                # Keep the initial contract self-contained; no network/file schema resolution.
                def check_refs(value):
                    if isinstance(value, dict):
                        if "$ref" in value and not str(value["$ref"]).startswith("#"):
                            raise ValueError("Only local JSON Schema references are supported")
                        if "$dynamicRef" in value:
                            raise ValueError("Dynamic schema references are not supported")
                        for child in value.values():
                            check_refs(child)
                    elif isinstance(value, list):
                        for child in value:
                            check_refs(child)
                check_refs(schema)
                Draft202012Validator.check_schema(schema)
            filename, function = tool["entrypoint"].split(":", 1)
            path = inside(self.root, filename)
            if path not in modules:
                modules[path] = self._module(filename)
            fn = getattr(modules[path], function, None)
            if not callable(fn):
                raise ValueError(f"Entrypoint is not callable: {tool['entrypoint']}")
            self.functions[name] = fn

    def _module(self, filename):
        path = inside(self.root, filename)
        if path not in self.modules:
            # A private package namespace supports adapter-local relative imports
            # without mixing modules from different revisions in sys.modules.
            name = self.namespace + "." + ".".join(Path(filename).with_suffix("").parts)
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            with contextlib.redirect_stdout(sys.stderr):
                # Do not create bytecode caches inside an acting adapter.
                exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
            self.modules[path] = module
        return self.modules[path]

    def _export(self, declaration):
        filename, function = declaration["entrypoint"].split(":", 1)
        fn = getattr(self._module(filename), function)
        kwargs = {"_resources": self.resources} if "_resources" in inspect.signature(fn).parameters else {}
        with contextlib.redirect_stdout(sys.stderr):
            return fn(**kwargs)

    def context(self):
        declaration = self.manifest.get("context")
        if declaration is None:
            return ""
        if isinstance(declaration, str):
            return inside(self.root, declaration).read_text()
        result = self._export(declaration)
        if not isinstance(result, str):
            raise ValueError("Context renderer must return Markdown text")
        return result

    def agent_definitions(self):
        declaration = self.manifest.get("agents")
        if not declaration:
            return []
        result = (json.loads(inside(self.root, declaration).read_text())["agents"]
                  if isinstance(declaration, str) else self._export(declaration))
        if not isinstance(result, list):
            raise ValueError("Agent exporter must return a list")
        names = set()
        for role in result:
            if not isinstance(role.get("name"), str) or role["name"] in names or not role.get("description"):
                raise ValueError("Invalid or duplicate exported role")
            names.add(role["name"])
            inside(self.root, role["instructions"])
        return result

    async def call(self, name: str, arguments: dict):
        from jsonschema import Draft202012Validator

        if name not in self.tools:
            raise ValueError(f"Unknown tool: {name}")
        declaration = self.tools[name]
        Draft202012Validator(declaration["input_schema"]).validate(arguments)
        fn = self.functions[name]
        supplied = dict(arguments)
        if "_resources" in supplied:
            raise ValueError("_resources is reserved for framework bindings")
        if "_resources" in inspect.signature(fn).parameters:
            supplied["_resources"] = self.resources
        inspect.signature(fn).bind(**supplied)
        with contextlib.redirect_stdout(sys.stderr):
            result = fn(**supplied)
            if inspect.isawaitable(result):
                result = await result
        Draft202012Validator(declaration["output_schema"]).validate(result)
        json.dumps(result, allow_nan=False)
        return result
