"""Load an adapter's exports (exports.yaml, with the identity in .hermit/adapter.yaml) and its Python tools, without
MCP dependencies.

An adapter is trusted executable code. Path and schema validation here are not
an OS sandbox. Evolution workspace isolation is enforced separately by the framework.
"""
import contextlib
import importlib.util
import inspect
import json
import os
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
    """The adapter's exports with its identity (id, name, version), validated."""
    from hermit.adapter.manifest import flatten, read_manifest
    root = root.resolve()
    manifest = flatten(read_manifest(root))
    if os.path.lexists(root / manifest["context"]):
        inside(root, manifest["context"])     # ADAPTER.md is optional; when present it is a file inside the adapter
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
    hooks = manifest.get("hooks")             # its file is read when deploying (hook_definitions)
    if hooks is not None and (not isinstance(hooks, str) or Path(hooks).is_absolute()
                              or not (root / hooks).resolve().is_relative_to(root)):
        raise ValueError("hooks must name a JSON file inside the adapter")
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
    context_tool = manifest.get("context_tool")
    if context_tool is not None and (not isinstance(context_tool, str) or context_tool not in names):
        raise ValueError(f"context_tool must name one of the declared tools, not {context_tool!r}")
    if context_tool is not None and manifest.get("plugin-portable", True):
        raise ValueError("an adapter with a context_tool needs a template that declares plugin-portable: false: its "
                         "per-task context cannot be installed as a workspace or plugin")
    return manifest


def hook_definitions(root: Path, manifest: dict) -> dict | None:
    """The declared lifecycle hooks: `hooks` names a JSON file inside the adapter in Claude Code's hook schema,
    {"hooks": {"<Event>": [{"matcher": ..., "hooks": [{"type": "command", "command": ...}]}]}}, which Codex reads
    too. None when nothing is declared or the file does not exist (yet: a learner may add it)."""
    declared = manifest.get("hooks")
    if declared is None:
        return None
    if not isinstance(declared, str) or Path(declared).is_absolute():
        raise ValueError("hooks must name a JSON file inside the adapter")
    path = (Path(root) / declared).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError("hooks must name a JSON file inside the adapter")
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        raise ValueError(f"{declared} is not JSON: {exc}") from None
    events = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(events, dict):
        raise ValueError(f'{declared} needs a top-level {{"hooks": {{"<Event>": [...]}}}} object')
    for event, groups in events.items():
        if not isinstance(groups, list) or not all(isinstance(g, dict) and isinstance(g.get("hooks"), list)
                                                   for g in groups):
            raise ValueError(f'{declared}: {event} must be a list of {{"hooks": [...]}} groups')
        for hook in (h for g in groups for h in g["hooks"]):
            if not isinstance(hook, dict) or not isinstance(hook.get("type"), str):
                raise ValueError(f"{declared}: every {event} hook is an object with a type")
            if hook["type"] == "command" and not (isinstance(hook.get("command"), str) and hook["command"].strip()):
                raise ValueError(f"{declared}: a command hook of {event} needs a command")
    return data


def agent_tools(manifest: dict) -> list[dict]:
    """The declared tools an agent (or a host's tool server) is offered: every declaration but the context tool,
    which the harness calls itself once per task."""
    hidden = manifest.get("context_tool")
    return [dict(t) for t in manifest.get("tools") or [] if t.get("name") != hidden]


def not_portable(manifest: dict, target: str) -> str | None:
    """Why `target` (a host workspace or a plugin) refuses the adapter, or None: its template declares
    `plugin-portable: false`, so the adapter works only through hermit (training, evaluation, a Session)."""
    if manifest.get("plugin-portable", True):
        return None
    why = (f"; its context tool `{manifest['context_tool']}` adds context per task, which the harness computes before "
           "the agent's first model call, and an installed workspace or plugin has no such moment"
           if manifest.get("context_tool") else "")
    return (f"{target} cannot carry the {manifest.get('id', 'adapter')} adapter: its template declares "
            f"plugin-portable: false{why}. Use it through hermit (train, eval or a Session).")


def join_context(text: str, result) -> str:
    """ADAPTER.md with a context tool's `text` appended, when it has one."""
    extra = result.get("text") if isinstance(result, dict) else None
    if not isinstance(extra, str) or not extra.strip():
        return text
    return text.rstrip() + "\n\n" + extra.strip() + "\n"


def _lenient_frontmatter(text: str) -> tuple[dict, str]:
    """A file's frontmatter read line by line, as the Codex-tree harnesses read an AGENT.md ({} without one), and
    its body: such a file never fails to load, so the deploy check accepts every subagent their own checks accept."""
    if not text.startswith("---\n") or text.find("\n---", 4) < 0:
        return {}, text
    end = text.find("\n---", 4)
    head = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and re.fullmatch(r"[A-Za-z_][\w-]*", key.strip()):
            value = value.strip()
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:     # a double-quoted YAML scalar
                try:
                    value = str(json.loads(value))
                except ValueError:
                    value = value.strip('"')
            else:
                value = value.strip("\"'")
            head[key.strip()] = value
    return head, text[end + 4:].lstrip("-").lstrip("\n")


def subagent_files(root: Path, directory: str) -> list:
    """One Markdown file per subagent. `<directory>/<name>.md`: YAML frontmatter with name and description, the body
    is its instructions (the same format as Claude Code's subagent files). `<directory>/<name>/AGENT.md`, the
    Codex-tree templates' layout, read as their harnesses read it: named by its directory, its description from its
    frontmatter (else its name). A README.md beside them documents the directory and is not a subagent."""
    import yaml
    out = []
    base = root / directory
    for path in sorted(p for p in base.glob("*.md") if p.name != "README.md"):
        text = path.read_text()
        if not text.startswith("---\n") or "\n---" not in text[4:]:
            raise ValueError(f"{path.relative_to(root)}: a subagent file starts with YAML frontmatter")
        end = text.index("\n---", 4)
        head = yaml.safe_load(text[4:end]) or {}
        body = text[end + 4:].lstrip("-").lstrip("\n")
        if not isinstance(head, dict) or not head.get("name") or not head.get("description"):
            raise ValueError(f"{path.relative_to(root)}: frontmatter needs name and description")
        out.append({"name": str(head["name"]), "description": str(head["description"]),
                    "instructions": path.relative_to(root).as_posix(), "body": body})
    for path in sorted(base.glob("*/AGENT.md")):
        head, body = _lenient_frontmatter(path.read_text(errors="replace"))
        name = path.parent.name
        out.append({"name": name, "description": head.get("description") or name,
                    "instructions": path.relative_to(root).as_posix(), "body": body})
    return out


class Adapter:
    def __init__(self, root: Path, resources: dict | None = None):
        from jsonschema import Draft202012Validator

        self.root = root.resolve()
        self.manifest = load_manifest(self.root)
        self.tools = {t["name"]: t for t in self.manifest.get("tools", [])}
        self.functions = {}
        self.resources = dict(resources or {})
        from hermit.runtime.python_sandbox import PythonSandbox
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
        if not os.path.lexists(Path(self.root) / self.manifest["context"]):
            return ""                                           # a learner may remove ADAPTER.md: no context
        return inside(self.root, self.manifest["context"]).read_text()

    async def acontext(self, query: str = "", task: dict | None = None) -> tuple[str, dict | None]:
        """The context for one task: ADAPTER.md, and below it the `text` the manifest's `context_tool` returns for
        {query, task} when the adapter declares one. Also returns that tool's whole result (None without one)."""
        text = self.context()
        name = self.manifest.get("context_tool")
        if not name:
            return text, None
        result = await self.call(name, {"query": query or "", "task": dict(task or {})})
        return join_context(text, result), result

    def agent_definitions(self):
        declaration = self.manifest.get("agents")
        if not declaration:
            return []
        result = subagent_files(self.root, declaration) if isinstance(declaration, str) else self._export(declaration)
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
