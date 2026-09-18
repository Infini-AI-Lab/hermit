#!/usr/bin/env python3
"""Verify this demo's real calls and native role using Codex 0.154 rollout records.

Only selected facts are exported. Full prompts, credentials and opaque payloads
are not copied into the proof file. This is a demo-specific verifier, not a
general cross-version Codex tracing API.
"""
import argparse
import json
import os
from pathlib import Path


def records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def verify(run: Path) -> dict:
    events = records(run / "events.jsonl")
    audit = records(run / "tool-calls.jsonl")
    parent = next((r["thread_id"] for r in events if r.get("type") == "thread.started"), None)
    answer = (run / "answer.md").read_text() if (run / "answer.md").exists() else ""
    sessions = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    parent_paths = list(sessions.glob(f"*/*/*/*{parent}*.jsonl")) if parent else []
    requested_role = False
    child = None
    child_called_memory = False
    if len(parent_paths) == 1:
        for row in records(parent_paths[0]):
            payload = row.get("payload", {})
            if payload.get("type") == "function_call" and payload.get("name", "").endswith("spawn_agent"):
                try:
                    requested_role |= json.loads(payload.get("arguments", "{}" )).get("agent_type") == "ha_memory_scout"
                except (ValueError, TypeError):
                    pass
        # Inspect only the metadata header of sibling rollouts to find this test's child.
        for path in parent_paths[0].parent.glob("*.jsonl"):
            try:
                with path.open() as stream:
                    meta = json.loads(next(stream)).get("payload", {})
            except (OSError, ValueError, StopIteration):
                continue
            source = meta.get("source")
            spawn = source.get("subagent", {}).get("thread_spawn", {}) if isinstance(source, dict) else {}
            if spawn.get("parent_thread_id") != parent or meta.get("agent_role") != "ha_memory_scout":
                continue
            child = {"id": meta["id"], "role": meta["agent_role"], "parent_id": parent}
            for row in records(path):
                payload = row.get("payload", {})
                text = str(payload.get("input", "")) + str(payload.get("name", ""))
                if payload.get("type") in ("function_call", "custom_tool_call"):
                    child_called_memory |= "read_memory" in text and "ha_demo" in text
            break
    read_ids = {r.get("arguments", {}).get("memory_id") for r in audit
                if r.get("tool") == "read_memory" and r.get("status") == "ok"}
    calculated = any(r.get("tool") == "calculate_refund" and r.get("status") == "ok"
                     and r.get("result", {}).get("refund") == "190.00" for r in audit)
    parent_calculated = any(r.get("type") == "item.completed"
                            and r.get("item", {}).get("type") == "mcp_tool_call"
                            and r["item"].get("server") == "ha_demo"
                            and r["item"].get("tool") == "calculate_refund"
                            and r["item"].get("status") == "completed" for r in events)
    checks = {"native_role_requested": requested_role, "native_child_recorded": child is not None,
              "child_called_memory_mcp": child_called_memory,
              "required_memories_read": {"returns", "shipping"} <= read_ids,
              "parent_called_calculator_mcp": parent_calculated, "correct_calculation_recorded": calculated,
              "role_marker_returned": "HA_MEMORY_SCOUT_V1" in answer}
    proof = {"passed": all(checks.values()), "checks": checks, "parent_thread_id": parent,
             "native_child": child, "memory_ids_read": sorted(read_ids), "refund": "190.00" if calculated else None,
             "tool_calls": [{"tool": r["tool"], "status": r["status"], "pid": r["pid"]} for r in audit]}
    (run / "verification.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
    return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    result = verify(args.run.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
