#!/usr/bin/env python3
"""Render an adapter's context using the framework's installed environment.

The deploy check (check 5 of check_spec) and the hosts run this. An adapter whose manifest declares a `context_tool`
also has that tool called once with an empty query: with no query there is nothing to retrieve, so the call must
answer without network (this runs in a strict sandbox) and shows that the tool imports and its files parse.
"""
import argparse
import asyncio
import json
from pathlib import Path

from hermit.adapter.loader import Adapter

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    args = parser.parse_args()
    adapter = Adapter(args.adapter)
    out = {"context": adapter.context(), "agents": adapter.agent_definitions(),
           "context_tool": adapter.manifest.get("context_tool")}
    if out["context_tool"]:
        # One line, the tool's own message: a traceback would name the template's fixed modules, and the repair
        # attributes a deploy failure to the first adapter file the error text names.
        try:
            result = asyncio.run(adapter.call(out["context_tool"], {"query": "", "task": {}}))
        except Exception as exc:                        # noqa: BLE001
            raise SystemExit(f"context_tool {out['context_tool']} failed: {type(exc).__name__}: {exc}") from None
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise SystemExit(f"context_tool {out['context_tool']} must return an object with a `text` string")
    print(json.dumps(out))
