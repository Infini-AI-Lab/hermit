"""A session's stream read back wherever it was kept (hermit.agent.base.trajectory, the one reader of the hosts' logs):
Hermit's own log, a Harbor trial's curated log (each CLI's stream under agent/, as the HermitXXX that ran it in the
task's container wrote it; Harbor's own output of a CLI in a trial recorded before that; Harbor's ATIF trajectory, the
last place a log is read from); and each CLI Base Agent's outcome() of a stream its caller read elsewhere, or of none.
The streams are the CLIs' recorded fixtures (tests/fixtures/), no CLI runs."""
import json
from pathlib import Path

import pytest

from hermit.agent.base import cli, trajectory
from hermit.agent.base.trajectory import (HARBOR_STREAMS, READERS, STREAM_HOSTS, events, find_stream, log_steps,
                                          read_atif)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
CODEX = [{"type": "thread.started", "thread_id": "th1"},
         {"type": "item.completed", "item": {"type": "command_execution", "command": "cat task.md", "exit_code": 0,
                                             "aggregated_output": "What were the receipts?"}},
         {"type": "item.completed", "item": {"type": "agent_message", "text": "<answer>44,463</answer>"}},
         {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2}}]
MINI = [{"type": "session.started", "session": "s1", "model": "openai/m"},
        {"type": "message", "message": {"role": "system", "content": "You are a helpful assistant."}},
        {"type": "message", "message": {"role": "user", "content": "What is 2 + 2?"}},
        {"type": "message", "message": {"role": "assistant", "content": "It is 4.",
                                        "actions": ["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && echo 4"]},
         "usage": {"input_tokens": 9, "cached_input_tokens": 0, "output_tokens": 3}},
        {"type": "message", "message": {"role": "exit", "content": "", "exit_status": "Submitted",
                                        "submission": "4"}}]


def stream_text(backend: str) -> str:
    """A served session's stream of each CLI: a recorded fixture, else (Codex, mini-swe-agent) one of its events."""
    fixture = {"claude": "claude_streams/reply.jsonl", "pi": "pi/success.jsonl", "opencode": "opencode/success.jsonl",
               "dsh": "dsh/success.jsonl"}.get(backend)
    if fixture:
        return (FIXTURES / fixture).read_text()
    return "a notice of the CLI's own\n" + "".join(json.dumps(e) + "\n" for e in {"codex": CODEX,
                                                                                     "mini-swe-agent": MINI}[backend])


BACKENDS = sorted(STREAM_HOSTS.values())


def harbor_log(root: Path, backend: str) -> Path:
    """A Harbor trial's curated log (hermit.harbor.curate) with the CLI's stream under agent/, the verifier's output
    beside it."""
    log = root / "hermit-log"
    (log / "agent").mkdir(parents=True)
    (log / "verifier").mkdir()
    (log / "agent" / f"agent-{backend}.jsonl").write_text(stream_text(backend))
    (log / "verifier" / "test-stdout.txt").write_text("1 passed\n")
    return log


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_harbor_trials_log_is_read_from_its_acting_clis_stream_under_agent(tmp_path, backend):
    """A Harbor trial keeps its acting CLI's stream as agent/agent-<backend>.jsonl, whichever of the six CLIs it ran:
    the log, the stream file and a learning-material record of it give that CLI's own steps."""
    log = harbor_log(tmp_path / "trial", backend)
    stream = log / "agent" / f"agent-{backend}.jsonl"
    assert f"agent/agent-{backend}.jsonl" in HARBOR_STREAMS
    assert find_stream(log) == (stream, backend)
    steps = READERS[backend](events(stream))
    assert steps and log_steps(log) == steps and log_steps(stream) == steps
    assert trajectory.read({"trajectory": "trial/hermit-log"}, tmp_path) == steps


def test_hermits_own_stream_at_a_logs_top_comes_before_a_harbor_layout(tmp_path):
    """A log with Hermit's stream at its top is read from it, whatever else lies below."""
    log = harbor_log(tmp_path, "pi")
    (log / "agent-codex.jsonl").write_text(stream_text("codex"))
    assert find_stream(log) == (log / "agent-codex.jsonl", "codex")
    assert [s.kind for s in log_steps(log)] == ["command", "message"]


def test_harbors_own_output_of_a_cli_is_read_by_its_name(tmp_path):
    """A trial recorded before Hermit named its stream keeps Harbor's output of the CLI: Codex's events as
    agent/codex.txt, Claude Code's stream-json as agent/claude-code.txt (an empty one is no stream)."""
    log = tmp_path / "trial"
    (log / "agent").mkdir(parents=True)
    (log / "agent" / "claude-code.txt").write_text(stream_text("claude"))
    assert find_stream(log) == (log / "agent" / "claude-code.txt", "claude")
    assert log_steps(log) == READERS["claude"](events(log / "agent" / "claude-code.txt"))
    assert log_steps(log / "agent") == log_steps(log)                     # the trial's agent/ itself
    (log / "agent" / "claude-code.txt").write_text("")
    assert find_stream(log) is None and log_steps(log) is None


ATIF = {"schema_version": "ATIF-v1.7", "session_id": "s", "agent": {"name": "claude-code", "version": "2.1.318"},
        "steps": [
            {"step_id": 1, "source": "system", "message": "You are Claude Code."},
            {"step_id": 2, "source": "user", "message": "Count the connected components."},
            {"step_id": 3, "source": "agent", "message": "", "reasoning_content": "List the files first.",
             "tool_calls": [{"tool_call_id": "t1", "function_name": "Bash", "arguments": {"command": "ls /app"}},
                            {"tool_call_id": "t2", "function_name": "Write",
                             "arguments": {"file_path": "/app/solver.py", "content": "class Solver: ..."}}],
             "observation": {"results": [
                 {"source_call_id": "t1", "content": "solver.py", "extra": {"tool_result_is_error": False}},
                 {"source_call_id": "t2", "content": [{"type": "text", "text": "File created"}]}]}},
            {"step_id": 4, "source": "user", "message": "Stop hook: run the tests first."},
            {"step_id": 5, "source": "agent", "message": "",
             "tool_calls": [{"tool_call_id": "c1", "function_name": "bash_command",
                             "arguments": {"keystrokes": "pytest -q\n", "duration": 5}},
                            {"tool_call_id": "c2", "function_name": "mark_task_complete", "arguments": {}}],
             "observation": {"results": [{"content": "1 failed"}]}},
            {"step_id": 6, "source": "agent", "message": "",
             "observation": {"results": [{"content": "$ (the screen, unchanged)"}]}},
            {"step_id": 7, "source": "agent", "message": [{"type": "text", "text": "Done: 3 components."}]}]}


def test_harbors_atif_trajectory_is_the_last_place_a_log_is_read_from(tmp_path):
    """One of Harbor's own agents leaves only its ATIF trajectory (agent/trajectory.json): its agent steps become the
    common steps (the system's steps and the task left out, as the streams leave out their prompt; a later user step
    a user message; each call's result its output, one naming no call the step's last call's, and one of a step
    without calls what the agent was shown next); a stream, when there is one, comes first; a trajectory.json that is
    no ATIF is none."""
    log = tmp_path / "trial"
    (log / "agent").mkdir(parents=True)
    (log / "agent" / "trajectory.json").write_text(json.dumps(ATIF, indent=1))
    steps = log_steps(log)
    assert [(s.kind, s.role) for s in steps] == [("reasoning", "assistant"), ("command", "assistant"),
                                                 ("file_change", "assistant"), ("message", "user"),
                                                 ("command", "assistant"), ("tool_call", "assistant"),
                                                 ("message", "user"), ("message", "assistant")]
    listed, written, told, tests, done, screen, answer = steps[1:]
    assert (listed.command, listed.output, listed.exit_code) == ("ls /app", "solver.py", 0)
    assert json.loads(written.text) == {"tool": "Write", "path": "/app/solver.py"} and written.output == "File created"
    assert told.text == "Stop hook: run the tests first."
    assert (tests.command, tests.output, tests.exit_code) == ("pytest -q", "", None)     # the keys' Enter left out
    assert json.loads(done.text) == {"tool": "mark_task_complete", "input": {}} and done.output == "1 failed"
    assert screen.text == "$ (the screen, unchanged)" and answer.text == "Done: 3 components."
    assert log_steps(log / "agent" / "trajectory.json") == steps == read_atif(log / "agent" / "trajectory.json")
    (log / "agent" / "agent-claude.jsonl").write_text(stream_text("claude"))
    assert log_steps(log) == READERS["claude"](events(log / "agent" / "agent-claude.jsonl"))
    (log / "agent" / "agent-claude.jsonl").unlink()
    (log / "agent" / "trajectory.json").write_text(json.dumps({"steps": [{"source": "agent", "message": "hi"}]}))
    assert log_steps(log) is None and read_atif(log / "agent" / "trajectory.json") is None


def test_json_a_trial_wrote_nested_past_the_parsers_depth_is_no_event_and_no_trajectory(tmp_path):
    """A task's container writes the stream and the ATIF file: an event line nested past the JSON parser's depth is
    skipped as one that does not parse is (the stream's other events still read), and such a trajectory.json is
    none; never a RecursionError, which would fail the trial."""
    deep = "[" * 100_000 + "]" * 100_000
    stream = tmp_path / "agent-codex.jsonl"
    stream.write_text('{"type": "x", "a": ' + deep + "}\n" + "".join(json.dumps(e) + "\n" for e in CODEX))
    assert events(stream) == CODEX
    (tmp_path / "trajectory.json").write_text('{"schema_version": "ATIF-v1.7", "steps": ' + deep + "}")
    assert read_atif(tmp_path / "trajectory.json") is None


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_clis_outcome_reads_a_stream_its_caller_read_elsewhere_or_none(tmp_path, backend):
    """A Harbor trial's runner reads the stream it took from the task's container (its own copy of the file): every
    CLI's outcome() gives the text alone what the same stream's file gives, the reply included; a stream that is
    missing, or none at all, is a session that was not served, never a TypeError."""
    raw = stream_text(backend)
    path = tmp_path / f"agent-{backend}.jsonl"
    path.write_text(raw)
    agent = cli.CLIS[backend]({})
    said = agent.outcome(raw, path, None)
    assert said["served"] and said["reply"].strip()
    assert agent.outcome(raw, None, None) == said
    for stream in (None, tmp_path / "missing.jsonl"):
        nothing = agent.outcome("", stream, None)
        assert nothing["reply"] == "" and not nothing["served"]
    assert events(None) == [] and events(tmp_path / "missing.jsonl") == []


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_reply_that_holds_a_unicode_line_separator_is_one_event_read_whole_and_no_notice(tmp_path, backend):
    """A CLI's JSON writer leaves U+0085, U+2028 and U+2029 unescaped in an event's strings (Codex's serde_json,
    JSON.stringify, the mini-swe-agent runner's json.dumps), where str.splitlines() ends a line; they end no line of a
    stream. Every CLI's served session whose reply holds one is served, its events, steps and reply whole, from the
    stream's file and from its text, and the words after it are no notice of the CLI's own."""
    raw = stream_text(backend)
    agent = cli.CLIS[backend]({})
    reply = agent.outcome(raw, None, None)["reply"]
    plain = tmp_path / "plain.jsonl"
    plain.write_text(raw, encoding="utf-8")
    for sep in ("\x85", "\u2028", "\u2029"):
        held = f"{reply}{sep}Not logged in"
        text = raw.replace(json.dumps(reply)[1:-1], json.dumps(held, ensure_ascii=False)[1:-1])
        path = tmp_path / f"agent-{backend}.jsonl"
        path.write_text(text, encoding="utf-8")
        assert sep in path.read_text(encoding="utf-8") and len(events(path)) == len(events(plain)), repr(sep)
        assert any(held in s.text + s.command + s.output for s in READERS[backend](events(path))), repr(sep)
        said = agent.outcome(text, path, None)
        assert said["served"] and said["reply"] == held and agent.outcome(text, None, None) == said, repr(sep)
        assert agent.notices(text) == agent.notices(raw) and not said.get("login_failed"), repr(sep)
