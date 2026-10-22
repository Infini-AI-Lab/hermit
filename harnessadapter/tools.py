"""The Evolver's tools: declaration, assembly from three sources (template, task, run), metering, delivery.

check_spec is always present. Template tools come from adapter.yaml `evolver_tools`, task tools from the run config's
`evolver_access.tools`, run tools are `budget` and (when the run config has an `llm` section) `llm`. Every call is
logged to the run's tool log. Delivery: in-process (`ctx.tools`) for class Evolvers, and an HTTP endpoint that the
MCP proxy (`python -m harnessadapter.tools_mcp`) and external programs use.
"""
from __future__ import annotations

from datetime import datetime, timezone
import http.server
import secrets
import importlib.util
import inspect
import json
from pathlib import Path
import threading
from typing import Any, Callable

from jsonschema import Draft202012Validator


LOG_LIMIT = 4000


class ToolError(RuntimeError):
    pass


class FunctionTool:
    def __init__(self, name: str, description: str, fn: Callable[..., dict], *, source: str,
                 input_schema: dict | None = None, output_schema: dict | None = None):
        self.name, self.description, self.fn, self.source = name, description, fn, source
        self.input_schema = input_schema or {"type": "object"}
        self.output_schema = output_schema or {"type": "object"}
        Draft202012Validator.check_schema(self.input_schema)
        self.log_path: Path | None = None
        self.cancel = None

    def describe(self) -> dict:
        return {"name": self.name, "description": self.description, "source": self.source,
                "input_schema": self.input_schema, "output_schema": self.output_schema}

    def __call__(self, **arguments: Any) -> dict:
        record = {"time": datetime.now(timezone.utc).isoformat(), "tool": self.name, "source": self.source,
                  "arguments": _clip(arguments)}
        try:
            if self.cancel is not None and self.cancel.is_set():
                raise ToolError("the call was stopped by the Trainer (budget or timeout)")
            Draft202012Validator(self.input_schema).validate(arguments)
            result = self.fn(**arguments)
            if not isinstance(result, dict):
                raise ToolError(f"tool {self.name} returned {type(result).__name__}, not an object")
            record.update(status="ok", result=_clip(result))
            return result
        except Exception as exc:
            record.update(status="error", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if self.log_path is not None:
                with open(self.log_path, "a") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _clip(value):
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= LOG_LIMIT else text[:LOG_LIMIT] + "…"


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
    """A tool declared like an adapter tool: name, description, entrypoint, input_schema, output_schema.
    Keyword arguments the function declares among `bound` (for example `_adapter`) are filled by the framework.
    With a `runner` (the `tool` role's sandbox, isolate.ToolRunner) the code is never imported here: every call runs
    in a sandboxed worker."""
    if runner is not None:
        def call(**arguments):
            return runner(decl["entrypoint"], base, arguments, bound)
        return FunctionTool(decl["name"], decl["description"], call, source=source,
                            input_schema=decl.get("input_schema"), output_schema=decl.get("output_schema"))
    fn = _load_entry(decl["entrypoint"], base)
    params = inspect.signature(fn).parameters
    extra = {k: v for k, v in bound.items() if k in params}

    def call(**arguments):
        return fn(**arguments, **extra)
    return FunctionTool(decl["name"], decl["description"], call, source=source,
                        input_schema=decl.get("input_schema"), output_schema=decl.get("output_schema"))


def assemble(*, adapter: Path, root: Path, template_decls: list, task_decls: list, task_base: Path,
             budget, llm_client=None, python: str | None = None,
             metered: list | None = None, runner=None, within: list[Path] = ()) -> dict[str, FunctionTool]:
    """One toolset for one call. `adapter` is the workspace copy the tools act on; `root` the template's files;
    `within` are the learner's other writable directories (scratch, state), where an Evolver keeps a candidate."""
    from harnessadapter.spec import check_spec
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
    if llm_client is not None:
        def llm(messages: list, max_tokens: int | None = None, temperature: float | None = None) -> dict:
            budget.check()
            kwargs = {k: v for k, v in (("max_tokens", max_tokens), ("temperature", temperature)) if v is not None}
            message, usage = llm_client.chat(messages, **kwargs)
            budget.add("evolver", usage)
            if metered is not None:
                metered.append(usage)
            return {"content": message.get("content") or "", "reasoning": message.get("reasoning_content") or "",
                    "finish_reason": message.get("finish_reason"), "usage": usage.__dict__}
        tools["llm"] = FunctionTool(
            "llm", f"Call the run's model ({llm_client.model}) with chat messages; metered against the budget.", llm,
            source="run", input_schema={"type": "object", "required": ["messages"], "additionalProperties": False,
                                        "properties": {"messages": {"type": "array"},
                                                       "max_tokens": {"type": "integer"},
                                                       "temperature": {"type": "number"}}})
    return tools


class ToolServer:
    """Serve a toolset over HTTP on 127.0.0.1 for agents (through the MCP proxy) and external programs.
    GET /tools → [tool descriptions]; POST /call/<name> with a JSON object → {"result": ...} or {"error": ...}."""

    def __init__(self, tools: dict[str, FunctionTool]):
        self.tools = tools
        self.token = secrets.token_hex(16)     # the URL carries it: other local processes cannot call the tools
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                body = json.dumps(payload, ensure_ascii=False, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _route(self):
                prefix = f"/{outer.token}"
                return self.path[len(prefix):] if self.path.startswith(prefix + "/") else None

            def do_GET(self):
                if self._route() == "/tools":
                    self._send(200, [t.describe() for t in outer.tools.values()])
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                route = self._route()
                if route is None or not route.startswith("/call/"):
                    return self._send(404, {"error": "not found"})
                name = route[len("/call/"):]
                tool = outer.tools.get(name)
                if tool is None:
                    return self._send(404, {"error": f"unknown tool {name}"})
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    arguments = json.loads(self.rfile.read(length) or b"{}")
                    self._send(200, {"result": tool(**arguments)})
                except Exception as exc:
                    self._send(200, {"error": f"{type(exc).__name__}: {exc}"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/{self.token}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
