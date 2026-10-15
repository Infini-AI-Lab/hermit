"""The seat's view of a Harness Adapter (ported from the old port's tasks/CLBench/host/database_system.py).

metaworld's seat (mwm.seat.SeatMixin) talks to a world-model interface with host_tool_specs / host_tool_names /
resolve / call / package. This class serves that surface from an adapter directory through the framework's loader.
The package is rendered live by the adapter's own interface.context(), with the run's evidence store and the venue's
host.json bound, as the old port served it; an adapter without that function serves its ADAPTER.md.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional


class EvidenceStore:
    """The `evidence` resource: the run's evidence store, read by the adapter's trace/recall."""

    def __init__(self, root: Path):
        self.root = Path(root)


class AdapterIface:
    delegation = "none"
    delegate = None

    def __init__(self, root: Path, evidence_dir: Path, deployment: Optional[dict] = None):
        from harnessadapter.adapter.loader import Adapter
        evidence_dir.mkdir(parents=True, exist_ok=True)
        resources = {"evidence": EvidenceStore(evidence_dir)}
        if deployment:
            resources["deployment"] = dict(deployment)
        self.adapter = Adapter(Path(root), resources)
        self.tools = list(self.adapter.manifest.get("tools", []))

    def host_tool_specs(self) -> list:
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in self.tools]

    def host_tool_names(self) -> set:
        return {t["name"] for t in self.tools}

    def resolve(self, name: str, args) -> tuple:
        args = args if isinstance(args, dict) else {}
        if name == "wm":
            inner = args.get("args")
            return str(args.get("name", "")), (inner if isinstance(inner, dict) else {})
        return name, args

    def call(self, wname: str, wargs: Optional[dict] = None, transport_note=None) -> str:
        if not any(t["name"] == "wm" for t in self.tools):
            return "no world-model tool is declared by this adapter"
        result = asyncio.run(self.adapter.call("wm", {"name": wname, "args": wargs or {}}))
        if isinstance(result, dict) and "text" in result:
            text = result["text"]
            # A paged read: the host gets the page and the way to the next one.
            if result.get("eof") is False and result.get("next_offset") is not None:
                followup = {"path": result.get("path"), "offset": result["next_offset"],
                            "limit": result.get("limit", 4000)}
                if result.get("sha256"):
                    followup["expected_sha256"] = result["sha256"]
                text += ('\n[More text available; continue when useful: '
                         'wm(name="read", args=' + json.dumps(followup) + ')]')
            return text
        return json.dumps(result, ensure_ascii=False)

    def package(self, transport_note: str = "", with_delegate: bool = True) -> str:
        interface = self.adapter.root / "interface.py"
        if interface.is_file():
            module = self.adapter._module("interface.py")
            if callable(getattr(module, "context", None)):
                return module.context(_resources=self.adapter.resources)
        return self.adapter.context()
