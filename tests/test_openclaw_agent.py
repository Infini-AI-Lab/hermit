"""Tests for OpenClaw acting agent using fake binary scenario suite."""
import json
import os
from pathlib import Path
import stat
import sys
import pytest

from hermit.agent.acting import load_agent
from hermit.agent.acting.openclaw import OpenClawAgent

ROOT = Path(__file__).resolve().parents[1]
PROMPT_ONLY = ROOT / "hermit" / "adapter" / "templates" / "prompt_only"


FAKE_OPENCLAW_OK = """#!/bin/sh
# Check arguments
if [ "$1" = "--version" ]; then
    echo "OpenClaw 2026.10.9 (fake)"
    exit 0
fi
if [ "$1" = "agent" ] && [ "$2" = "exec" ]; then
    shift 2
    cwd=""; msg_file=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --cwd) cwd="$2"; shift 2 ;;
            --message-file) msg_file="$2"; shift 2 ;;
            --config|--state-dir|--model|--fallback|--code-mode) shift 2 ;;
            --json|--isolated|--local-model-lean) shift ;;
            *) shift ;;
        esac
    done
    cat <<JSON
{
  "ok": true,
  "status": "ok",
  "final": "THE ANSWER IS 42",
  "payloads": [{"text": "THE ANSWER IS 42"}],
  "usage": {"input": 150, "output": 25, "total": 175},
  "costUsd": 0.0003,
  "assistantTurns": 2,
  "model": "openai/gpt-6-astra",
  "provider": "openai",
  "sessionId": "fake-sess-12345678"
}
JSON
    exit 0
fi

if [ "$1" = "sessions" ] && [ "$2" = "export-trajectory" ]; then
    shift 2
    ws=""; out=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --workspace) ws="$2"; shift 2 ;;
            --output) out="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    target="$ws/.openclaw/trajectory-exports/$out"
    mkdir -p "$target"
    echo '{"manifest": true}' > "$target/manifest.json"
    echo '{"events": []}' > "$target/events.jsonl"
    exit 0
fi

echo "Unknown command: $*" >&2
exit 1
"""

FAKE_OPENCLAW_TIMEOUT = """#!/bin/sh
sleep 10
exit 0
"""

FAKE_OPENCLAW_ERROR = """#!/bin/sh
cat <<JSON
{
  "ok": false,
  "status": "error",
  "final": "",
  "payloads": [{"isError": true, "text": "Model rate limit exceeded"}],
  "sessionId": "fake-sess-err",
  "error": {"message": "Model rate limit exceeded", "kind": "rate_limit"}
}
JSON
exit 1
"""

FAKE_OPENCLAW_INVALID_JSON = """#!/bin/sh
echo "Fatal: Segmentation fault (core dumped)"
exit 1
"""


def create_fake_bin(tmp_path, script_text, name="openclaw"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / name
    exe.write_text(script_text)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def test_openclaw_agent_ok(tmp_path):
    exe = create_fake_bin(tmp_path, FAKE_OPENCLAW_OK)
    params = {
        "bin": exe,
        "model": "openai/gpt-6-astra",
        "prompt": "Answer question: {question}",
        "workspaces": str(tmp_path / "ws"),
        "timeout_s": 30
    }
    agent = OpenClawAgent(params, tmp_path)
    out = agent.run({"id": "task_1", "question": "What is life?"}, PROMPT_ONLY)

    assert out.output == "THE ANSWER IS 42"
    assert out.cost_usd == 0.0003
    assert out.tokens == 175
    assert out.extra["status"] == "ok"
    assert out.extra["exit_code"] == 0
    assert out.extra["sessionId"] == "fake-sess-12345678"
    assert out.log.is_dir()


def test_openclaw_agent_timeout(tmp_path):
    exe = create_fake_bin(tmp_path, FAKE_OPENCLAW_TIMEOUT)
    params = {
        "bin": exe,
        "prompt": "Take too long",
        "workspaces": str(tmp_path / "ws_timeout"),
        "timeout_s": 0.5,
        "grace_s": 0.2
    }
    agent = OpenClawAgent(params, tmp_path)
    out = agent.run({"id": "task_timeout"}, None)

    assert out.extra["status"] == "timeout"
    assert out.extra["exit_code"] == -1
    assert out.output == ""


def test_openclaw_agent_error_graceful(tmp_path):
    exe = create_fake_bin(tmp_path, FAKE_OPENCLAW_ERROR)
    params = {
        "bin": exe,
        "prompt": "Fail",
        "workspaces": str(tmp_path / "ws_err")
    }
    agent = OpenClawAgent(params, tmp_path)
    out = agent.run({"id": "task_err"}, None)

    assert out.extra["status"] == "error"
    assert out.extra["exit_code"] == 1
    assert out.extra["error"]["kind"] == "rate_limit"


def test_openclaw_agent_invalid_json(tmp_path):
    exe = create_fake_bin(tmp_path, FAKE_OPENCLAW_INVALID_JSON)
    params = {
        "bin": exe,
        "prompt": "Crash",
        "workspaces": str(tmp_path / "ws_crash")
    }
    agent = OpenClawAgent(params, tmp_path)
    out = agent.run({"id": "task_crash"}, None)

    assert out.extra["status"] == "error"
    assert out.extra["exit_code"] == 1
    assert out.output == ""


def test_load_agent_openclaw(tmp_path):
    agent = load_agent("openclaw", {"model": "test/model"}, tmp_path)
    assert isinstance(agent, OpenClawAgent)


def test_openclaw_settings_check(tmp_path):
    from hermit.config import Config
    from hermit.settings_check import run_check
    import yaml

    exe = create_fake_bin(tmp_path, FAKE_OPENCLAW_OK)
    (tmp_path / "task_info").mkdir()
    (tmp_path / "task_info" / "README.md").write_text("# Task\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "tasks.jsonl").write_text(json.dumps({"id": "t1", "prompt": "hi"}) + "\n")
    (tmp_path / "data" / "train.txt").write_text("t1\n")
    (tmp_path / "data" / "val.txt").write_text("\n")
    (tmp_path / "data" / "test.txt").write_text("\n")
    (tmp_path / "judge.py").write_text("def score(t, o): return {\x27score\x27: 1.0}\n")
    (tmp_path / "ev").mkdir()
    (tmp_path / "ev" / "evolver.yaml").write_text("description: d\nclass: ev.py:E\nneeds: []\nparams: {}\n")
    (tmp_path / "ev" / "ev.py").write_text(
        "from hermit.agent.evolver.base import Evolver, StepResult\n"
        "class E(Evolver):\n"
        "    def step(self, ctx): return StepResult(status=\x27done\x27, summary=\x27ok\x27)\n"
    )
    cfg_path = tmp_path / "run.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "template": "prompt-only@1.0",
        "task_info": "task_info",
        "data": {"tasks": "data/tasks.jsonl", "train": "data/train.txt", "val": "data/val.txt", "test": "data/test.txt"},
        "agent": {"name": "openclaw", "params": {"bin": exe}},
        "judge": {"path": "judge.py", "name": "score"},
        "evolver": {"name": "ev", "params": {}},
        "sandbox": {"enabled": False},
        "trainer": {"epochs": 1, "budget": {"usd": 1.0}}
    }))
    lines = []
    ok = run_check(Config.load(cfg_path), probe=False, log=lines.append)
    assert ok is True
    assert any("[ok] agent: openclaw" in line and "OpenClaw 2026.10.9 (fake)" in line for line in lines)
    assert not any("[PROBLEM]" in line for line in lines)
