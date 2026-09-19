#!/usr/bin/env python3
"""Exercise the actual stdio MCP transport without an LLM."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

from mcp import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]


async def smoke(adapter: Path):
    adapter = adapter.resolve()
    output = ROOT / ".demo" / "protocol"
    output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="smoke-", dir=output))
    audit = run / "tool-calls.jsonl"
    server = StdioServerParameters(command=sys.executable,
                                   args=["-m", "harnessadapter.runtime.mcp_server", "--adapter", str(adapter), "--audit-log", str(audit)],
                                   cwd=run)
    async with Client(server, read_timeout_seconds=20) as client:
        tools = (await client.list_tools()).tools
        names = {tool.name for tool in tools}
        assert names == {"list_memories", "read_memory", "calculate_refund"}, names
        memories = await client.call_tool("list_memories")
        assert not memories.is_error
        policy = await client.call_tool("read_memory", {"memory_id": "returns"})
        assert policy.structured_content["memory"]["fee_rate"] == "0.05", policy
        calculation = await client.call_tool("calculate_refund", {"item_subtotal": "200.00", "fee_rate": "0.05"})
        assert calculation.structured_content["refund"] == "190.00", calculation
        invalid = await client.call_tool("read_memory", {"memory_id": "../../README.md"})
        assert invalid.is_error, invalid
    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert [r["status"] for r in records] == ["ok", "ok", "ok", "error"], records
    summary = {"passed": True, "transport": "stdio", "tools": sorted(names),
               "refund": calculation.structured_content["refund"], "invalid_id_rejected": True,
               "audit_log": str(audit), "model_calls": 0}
    (run / "result.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, default=ROOT / "examples/minimal_shop")
    args = parser.parse_args()
    asyncio.run(smoke(args.adapter))
