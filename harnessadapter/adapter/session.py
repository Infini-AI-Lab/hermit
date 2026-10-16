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
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from harnessadapter.adapter.loader import Adapter
from harnessadapter.adapter.resources import EvidenceStore, LearningMaterial


class Session:
    def __init__(self, adapter: Path | str, *, resources: dict | None = None, deployment: dict | Path | str | None = None,
                 learning_material: Path | str | None = None, evidence: Path | str | None = None,
                 audit_log: Path | str | None = None):
        bound = dict(resources or {})
        if deployment is not None:
            bound["deployment"] = dict(deployment) if isinstance(deployment, dict) else json.loads(Path(deployment).read_text())
        if learning_material is not None:
            bound["learning_material"] = LearningMaterial(Path(learning_material))
        if evidence is not None:
            bound["evidence"] = EvidenceStore(Path(evidence))
        self.adapter = Adapter(Path(adapter), bound)
        self.audit_log = Path(audit_log) if audit_log is not None else None
        if self.audit_log is not None and self.audit_log.resolve().is_relative_to(self.adapter.root):
            raise ValueError("The audit log must live outside the adapter")

    # --- identity and exports -----------------------------------------------------------------------------------
    @property
    def root(self) -> Path:
        return self.adapter.root

    @property
    def manifest(self) -> dict:
        return self.adapter.manifest

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def resources(self) -> dict:
        return self.adapter.resources

    def context(self) -> str:
        """The adapter's context, ADAPTER.md exactly as written after the last adopted step."""
        return self.adapter.context()

    def agents(self) -> list[dict]:
        """The exported subagents: name, description, instructions path, body."""
        return self.adapter.agent_definitions()

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
        return list(self.adapter.tools.values())

    @property
    def tool_names(self) -> set[str]:
        return set(self.adapter.tools)

    def has_tool(self, name: str) -> bool:
        return name in self.adapter.tools

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
            result = await self.adapter.call(name, arguments)
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
