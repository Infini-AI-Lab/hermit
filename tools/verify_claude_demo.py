#!/usr/bin/env python3
"""Verify actual native delegation and MCP execution from a Claude Code trial.

Uses stream-json parent_tool_use_id links rather than trusting the final answer.
Only selected facts are written to the proof, not complete prompts or thinking.
"""
import argparse
import json
from pathlib import Path


def records(path: Path) -> list:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def verify(run: Path) -> dict:
    events, audit = records(run / "events.jsonl"), records(run / "tool-calls.jsonl")
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    final = next((e for e in reversed(events) if e.get("type") == "result"), {})
    process_path = run / "process-result.json"
    process = json.loads(process_path.read_text()) if process_path.exists() else {}
    calls, results = [], {}
    for event in events:
        if event.get("type") not in ("assistant", "user"):
            continue
        for block in event.get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            parent = event.get("parent_tool_use_id")
            if block.get("type") == "tool_use":
                calls.append({**block, "parent": parent})
            elif block.get("type") == "tool_result":
                results[(parent, block.get("tool_use_id"))] = block

    def successful(call):
        result = results.get((call["parent"], call.get("id")))
        return result is not None and not result.get("is_error", False)

    spawns = [c for c in calls if c.get("name") == "Agent" and c["parent"] is None
              and c.get("input", {}).get("subagent_type") == "ha-memory-scout" and successful(c)]
    spawn = spawns[0] if len(spawns) == 1 else None
    child_reads = [c for c in calls if spawn and c["parent"] == spawn["id"]
                   and c.get("name") == "mcp__ha_demo__read_memory" and successful(c)]
    read_ids = {c.get("input", {}).get("memory_id") for c in child_reads}
    audited_ids = {a.get("arguments", {}).get("memory_id") for a in audit
                   if a.get("tool") == "read_memory" and a.get("status") == "ok"}
    calculation = next((c for c in calls if c["parent"] is None
                        and c.get("name") == "mcp__ha_demo__calculate_refund" and successful(c)
                        and c.get("input") == {"item_subtotal": "200.00", "fee_rate": "0.05"}), None)
    audited_calculation = any(a.get("tool") == "calculate_refund" and a.get("status") == "ok"
                              and a.get("arguments") == {"item_subtotal": "200.00", "fee_rate": "0.05"}
                              and a.get("result", {}).get("refund") == "190.00" for a in audit)
    child_result = results.get((None, spawn["id"]), {}) if spawn else {}
    answer = final.get("result", "")
    checks = {
        "process_and_session_succeeded": process.get("exit_code") == 0 and process.get("timed_out") is False
        and final.get("subtype") == "success" and final.get("is_error") is False,
        "mcp_server_connected": any(s.get("name") == "ha_demo" and s.get("status") == "connected"
                                    for s in init.get("mcp_servers", [])),
        "native_role_loaded": "ha-memory-scout" in init.get("agents", []),
        "native_role_completed": spawn is not None,
        "child_read_required_memories": {"returns", "shipping"} <= read_ids,
        "server_recorded_required_reads": {"returns", "shipping"} <= audited_ids,
        "parent_completed_calculation": calculation is not None,
        "server_recorded_correct_calculation": audited_calculation,
        "child_role_marker_returned": "HA_MEMORY_SCOUT_V1" in str(child_result.get("content", "")),
        "context_marker_returned": "HA_ADAPTER_CONTEXT_V1" in answer,
        "no_permission_denials": final.get("permission_denials") == [],
    }
    proof = {"passed": all(checks.values()), "checks": checks,
             "session_id": init.get("session_id"), "model": init.get("model"),
             "native_role": "ha-memory-scout", "spawn_tool_use_id": spawn["id"] if spawn else None,
             "child_memory_ids": sorted(x for x in read_ids if isinstance(x, str)),
             "refund": "190.00" if audited_calculation else None,
             "total_cost_usd": final.get("total_cost_usd"),
             "tool_calls": [{"tool": a.get("tool"), "status": a.get("status"), "pid": a.get("pid")} for a in audit]}
    (run / "verification.json").write_text(json.dumps(proof, indent=2) + "\n")
    return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    result = verify(parser.parse_args().run.resolve())
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
