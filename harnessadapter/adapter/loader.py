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
    inside(root, manifest["context"])     # ADAPTER.md must exist
    if manifest.get("agents"):
        agents = manifest["agents"]
        if isinstance(agents, str):
            directory = (root / agents).resolve()
            if not directory.is_relative_to(root) or not directory.is_dir():
                raise ValueError("agents must name a directory of subagent Markdown files inside the adapter")
        elif isinstance(agents, dict) and isinstance(agents.get("entrypoint"), str):
            filename, separator, function = agents["entrypoint"].partition(":")
            if not separator or not function.isidentifier():
                raise ValueError("Invalid agent exporter entrypoint")
            inside(root, filename)
        else:
            raise ValueError("agents must be a directory of subagent files or a Python exporter")
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
        return inside(self.root, self.manifest["context"]).read_text()

    def agent_definitions(self):
        declaration = self.manifest.get("agents")
        if not declaration:
            return []
        result = self._subagent_files(declaration) if isinstance(declaration, str) else self._export(declaration)
        if not isinstance(result, list):
            raise ValueError("Agent exporter must return a list")
        names = set()
        for role in result:
            if not isinstance(role.get("name"), str) or role["name"] in names or not role.get("description"):
                raise ValueError("Invalid or duplicate exported role")
            names.add(role["name"])
            if "body" not in role:
                role["body"] = inside(self.root, role["instructions"]).read_text()
        return result

    def _subagent_files(self, directory: str) -> list:
        """One Markdown file per subagent: YAML frontmatter with name and description, the body is its instructions
        (the same format as Claude Code's subagent files)."""
        import yaml
        out = []
        for path in sorted((self.root / directory).glob("*.md")):
            text = path.read_text()
            if not text.startswith("---\n") or "\n---" not in text[4:]:
                raise ValueError(f"{path.relative_to(self.root)}: a subagent file starts with YAML frontmatter")
            end = text.index("\n---", 4)
            head = yaml.safe_load(text[4:end]) or {}
            body = text[end + 4:].lstrip("-").lstrip("\n")
            if not isinstance(head, dict) or not head.get("name") or not head.get("description"):
                raise ValueError(f"{path.relative_to(self.root)}: frontmatter needs name and description")
            out.append({"name": str(head["name"]), "description": str(head["description"]),
                        "instructions": path.relative_to(self.root).as_posix(), "body": body})
        return out

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
