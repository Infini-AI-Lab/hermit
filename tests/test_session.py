"""Session: using an adapter from Python (context, tools in a model's format, synchronous calls, audit log)."""
import asyncio
import json
from pathlib import Path

import pytest

from hermit import Session

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "_examples" / "file_adapter"


def test_exports_and_specs():
    s = Session(EXAMPLE)
    assert s.id == "file-example" and s.context().strip()
    assert s.tool_names == {"count_words"} and s.has_tool("count_words") and not s.has_tool("other")
    openai = s.tool_specs("openai")
    assert openai[0]["type"] == "function" and openai[0]["function"]["parameters"]["type"] == "object"
    anthropic = s.tool_specs("anthropic")
    assert anthropic[0]["name"] == "count_words" and "input_schema" in anthropic[0]
    assert [r["name"] for r in s.agents()] and [p.name for p in s.skills()]
    with pytest.raises(ValueError):
        s.tool_specs("unknown")


def test_call_sync_async_and_audit(tmp_path):
    s = Session(EXAMPLE, audit_log=tmp_path / "calls.jsonl")
    assert s.call("count_words", {"text": "a b c d"}) == {"count": 4}
    assert asyncio.run(s.acall("count_words", {"text": "a"})) == {"count": 1}

    async def inside_loop():       # the sync call still works when an event loop is running
        return s.call("count_words", {"text": "x y"})
    assert asyncio.run(inside_loop()) == {"count": 2}
    with pytest.raises(ValueError):
        s.call("missing", {})
    records = [json.loads(l) for l in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [r["status"] for r in records] == ["ok", "ok", "ok", "error"]
    assert records[0]["tool"] == "count_words" and records[0]["result"] == {"count": 4}
    with pytest.raises(ValueError, match="outside the adapter"):
        Session(EXAMPLE, audit_log=EXAMPLE / "calls.jsonl")


def test_respond_to_a_tool_call():
    s = Session(EXAMPLE)
    ok = s.respond({"id": "c1", "type": "function", "function": {"name": "count_words", "arguments": '{"text": "one two"}'}})
    assert ok == {"role": "tool", "tool_call_id": "c1", "content": '{"count": 2}'}
    bad_args = s.respond({"id": "c2", "function": {"name": "count_words", "arguments": "{not json"}})
    assert bad_args["content"].startswith("Error: JSONDecodeError")
    unknown = s.respond({"id": "c3", "function": {"name": "nope", "arguments": "{}"}})
    assert unknown["content"].startswith("Error: ValueError: Unknown tool")
    assert len(s.respond({"id": "c4", "function": {"name": "count_words", "arguments": '{"text": "a"}'}}, max_chars=5)["content"]) == 5
    assert Session.text({"text": "hello", "eof": True}) == "hello" and Session.text([1, 2]) == "[1, 2]"


def test_resources_are_bound(tmp_path):
    s = Session(EXAMPLE, deployment={"wm_call": "x"}, evidence=tmp_path / "ev", learning_material=tmp_path)
    assert s.resources["deployment"] == {"wm_call": "x"} and (tmp_path / "ev").is_dir()
    assert s.resources["evidence"].root == (tmp_path / "ev").resolve()
    assert s.resources["learning_material"].root == tmp_path.resolve() and "python_sandbox" in s.resources
