"""Session: a program's use of one adapter from Python.

The third way an adapter reaches an agent, beside `deploy` (a project workspace) and `convert` (a host plugin): the
program that runs the agent loads the adapter itself. A Session gives that program what the two other routes
give a host, in the shapes a model API takes:

    session = Session(adapter_dir)
    system_prompt = base + session.context()                 # ADAPTER.md as the Evolver last wrote it
    tools = task_tools + session.tool_specs("openai")        # the adapter's tools beside the task's own
    ...
    if session.has_tool(call["function"]["name"]):           # a tool call from the model
        messages.append(session.respond(call))               # the tool message, errors as text

`call` is synchronous and safe inside a running event loop; `acall` is the coroutine. Every call can be recorded
in an audit log (one JSON line each), the same record `harnessadapter serve` writes. Resources an adapter's tools
may receive through `_resources` are bound at construction: the deployment texts (a host.json), the learning
material, an evidence store, or anything else under `resources`.

With `sandbox=` the program never imports the adapter's code (#11): the tools run in a `harnessadapter serve` inside
that sandbox (`True` for the default: no network, nothing written but the evidence store), reached over MCP. The
deployment texts, learning material and evidence store go with it; other `resources` are Python objects that cannot
cross into the sandbox, and are refused. Close the Session (or use it as a context manager) to stop the server.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from harnessadapter.adapter.loader import Adapter, inside, load_manifest, subagent_files
from harnessadapter.adapter.resources import EvidenceStore, LearningMaterial


# Set by a sandboxed agent's worker (#11) in its environment, so every process it starts inherits it (a benchmark's
# own runner, a CLI agent's tool server): a Session made there runs the adapter's tools in a sandbox of their own.
# The adapter is the learner's work; its code runs neither in the process that reports the task's result nor with
# that process's access.
ENV_SANDBOX = "HA_ADAPTER_SANDBOX"                    # "1": sandbox by default; "0": this is that sandbox's server
ENV_ENFORCE = "HA_ADAPTER_SANDBOX_ENFORCE"            # the run's sandbox.enforce
ENV_PYTHON = "HA_FRAMEWORK_PYTHON"                    # the framework's interpreter, for a server started elsewhere


def _adapter_sandbox():
    """Strict, like the learner's: no network but the loopback endpoints the environment names (forwarded), none of
    the environment's keys, and the framework's own packages (the templates' tools need nothing else)."""
    from harnessadapter.sandbox import LOCAL_URL, Sandbox
    loopback = [k for k, v in os.environ.items() if LOCAL_URL.search(str(v))]
    return Sandbox(role="adapter", network="isolated", env_allow=loopback,
                   enforce=os.environ.get(ENV_ENFORCE, "required"))


class Session:
    def __init__(self, adapter: Path | str, *, resources: dict | None = None, deployment: dict | Path | str | None = None,
                 learning_material: Path | str | None = None, evidence: Path | str | None = None,
                 audit_log: Path | str | None = None, sandbox=None):
        self._remote = None
        self._sandboxed = False
        if sandbox is None and os.environ.get(ENV_SANDBOX) == "1":
            sandbox = _adapter_sandbox()
            owned = True
        else:
            owned = sandbox is True
        if sandbox is not None and sandbox is not False:
            from harnessadapter.adapter.remote import SandboxedServer
            from harnessadapter.sandbox import Sandbox
            if resources:
                raise ValueError("a sandboxed Session takes deployment, learning_material and evidence; other "
                                 f"resources ({', '.join(sorted(resources))}) cannot cross into the sandbox")
            self.adapter = None
            self._root = Path(adapter).resolve()
            self._manifest = load_manifest(self._root)
            self._sandbox = Sandbox(role="session") if sandbox is True else sandbox
            self._owns_sandbox = owned
            if isinstance(deployment, (str, Path)):
                deployment = json.loads(Path(deployment).read_text())
            self._init_audit(audit_log)
            self._sandboxed = True
            if self._manifest.get("tools"):                # nothing to serve, nothing to start
                self._remote = SandboxedServer(
                    self._sandbox, self._root, deployment=deployment,
                    learning_material=Path(learning_material).resolve() if learning_material is not None else None,
                    evidence=Path(evidence).resolve() if evidence is not None else None)
            return
        bound = dict(resources or {})
        if deployment is not None:
            bound["deployment"] = dict(deployment) if isinstance(deployment, dict) else json.loads(Path(deployment).read_text())
        if learning_material is not None:
            bound["learning_material"] = LearningMaterial(Path(learning_material))
        if evidence is not None:
            bound["evidence"] = EvidenceStore(Path(evidence))
        self.adapter = Adapter(Path(adapter), bound)
        self._init_audit(audit_log)

    def _init_audit(self, audit_log) -> None:
        self.audit_log = Path(audit_log) if audit_log is not None else None
        if self.audit_log is not None and self.audit_log.resolve().is_relative_to(self.root):
            raise ValueError("The audit log must live outside the adapter")

    @property
    def sandboxed(self) -> bool:
        return self._sandboxed

    def close(self) -> None:
        """Stop a sandboxed Session's server (nothing to do otherwise)."""
        if self._remote is not None:
            self._remote.close()
            self._remote = None
        if self._sandboxed and self._owns_sandbox:
            self._sandbox.close()
            self._owns_sandbox = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- identity and exports -----------------------------------------------------------------------------------
    @property
    def root(self) -> Path:
        return self.adapter.root if self.adapter is not None else self._root

    @property
    def manifest(self) -> dict:
        return self.adapter.manifest if self.adapter is not None else self._manifest

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def resources(self) -> dict:
        if self.adapter is None:
            raise AttributeError("a sandboxed Session's resources are bound in its server, not in this process")
        return self.adapter.resources

    def context(self) -> str:
        """The adapter's context, ADAPTER.md exactly as written after the last adopted step."""
        return inside(self.root, self.manifest["context"]).read_text()

    def agents(self) -> list[dict]:
        """The exported subagents: name, description, instructions path, body."""
        if self.adapter is not None:
            return self.adapter.agent_definitions()
        declaration = self.manifest.get("agents")
        if not declaration:
            return []
        if not isinstance(declaration, str):
            raise ValueError("this adapter's subagents come from a Python exporter; a sandboxed Session does not run "
                             "the adapter's code in this process")
        return subagent_files(self.root, declaration)

    def skills(self) -> list[Path]:
        """The exported skill directories (each holds a SKILL.md), sorted by name."""
        directory = self.manifest.get("skills")
        if not directory:
            return []
        root = (self.root / directory).resolve()
        if not root.is_dir():
            return []
        return sorted(p for p in root.iterdir() if (p / "SKILL.md").is_file())

    # --- tools --------------------------------------------------------------------------------------------------
    @property
    def tools(self) -> list[dict]:
        """The tool declarations: name, description, input_schema, output_schema, annotations."""
        return list(self.manifest.get("tools", []))

    @property
    def tool_names(self) -> set[str]:
        return {t["name"] for t in self.tools}

    def has_tool(self, name: str) -> bool:
        return name in self.tool_names

    def tool_specs(self, style: str = "openai") -> list[dict]:
        """The tools in a model API's format: "openai" (chat completions `tools`), "anthropic" (Messages `tools`)
        or "declarations" (the manifest entries)."""
        if style == "openai":
            return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                      "parameters": t["input_schema"]}} for t in self.tools]
        if style == "anthropic":
            return [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
                    for t in self.tools]
        if style == "declarations":
            return [dict(t) for t in self.tools]
        raise ValueError(f"Unknown tool spec style: {style}")

    async def acall(self, name: str, arguments: dict | None = None) -> Any:
        """Run one tool; the validated result. Raises on an unknown tool, invalid arguments or a tool error."""
        arguments = arguments if arguments is not None else {}
        record = {"time": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), "adapter": self.manifest["name"],
                  "version": self.version, "tool": name, "arguments": arguments}
        try:
            if not self._sandboxed:
                result = await self.adapter.call(name, arguments)
            elif not self.has_tool(name) or self._remote is None:
                raise ValueError(f"Unknown tool: {name}")
            else:
                result = await self._remote.acall(name, arguments)
            record.update(status="ok", result=result)
            return result
        except Exception as exc:
            record.update(status="error", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._audit(record)

    def call(self, name: str, arguments: dict | None = None) -> Any:
        """`acall` from synchronous code, inside or outside a running event loop."""
        coroutine = self.acall(name, arguments)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coroutine).result()

    @staticmethod
    def text(result: Any) -> str:
        """A tool result as the text a model reads: a result's own `text` field, else its JSON."""
        if isinstance(result, dict) and isinstance(result.get("text"), str):
            return result["text"]
        return json.dumps(result, ensure_ascii=False)

    def respond(self, tool_call: dict, *, max_chars: int | None = None) -> dict:
        """Answer one chat-completions tool call: the `tool` message to append. Argument parsing, validation and
        tool errors come back as text in the message, never as exceptions, so the loop goes on."""
        function = tool_call.get("function") or {}
        name = function.get("name", "")
        try:
            raw = function.get("arguments") or "{}"
            arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            content = self.text(self.call(name, arguments))
        except Exception as exc:
            content = f"Error: {type(exc).__name__}: {exc}"
        if max_chars is not None and len(content) > max_chars:
            content = content[:max_chars]
        return {"role": "tool", "tool_call_id": tool_call.get("id"), "content": content}

    def _audit(self, record: dict) -> None:
        if self.audit_log is None:
            return
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
