"""Harness² on Codex: the harness2-codex template (upstream's Codex tree as an adapter), the upstream Evolver's Codex
layout, and the paper's protocol end to end on the Trainer's toy task with a scripted session channel (no model, no
Docker, no network). How the Codex acting agent deploys such a tree and writes its home is tested without harness2
in tests/core/test_deploy.py."""
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace

import pytest

from hermit.config import Config
from hermit.spec import check_spec
from hermit.trainer.trainer import Trainer
from toy_task import make_task

pytest.importorskip("harness2.core.schedule")

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "hermit" / "adapter" / "templates" / "harness2_codex"
SKILL = "---\ndescription: reading a table of a bulletin\n---\nRun `python3 .agents/skills/tables/scripts/sum.py`.\n"
HOOKS = {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
                                                                  "command": "python3 scripts/check.py"}]}]}}


@pytest.fixture
def tmp_path():
    """A directory outside /tmp: the template check runs under bubblewrap with an empty tmpfs at /tmp."""
    base = Path.home() / ".cache" / "hermit-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


def full_tree() -> dict:
    return {"AGENTS.md": "Do the work.\n- When a table must be read, read .agents/skills/tables/SKILL.md and follow "
                         "it.\n",
            ".agents/skills/tables/SKILL.md": SKILL, ".agents/skills/tables/scripts/sum.py": "print(1 + 1)\n",
            "scripts/check.py": "print('ok')\n",
            "sub_agents/reader/AGENT.md": "---\ndescription: reads long files\n---\nSummarise what you read.\n",
            ".codex/hooks.json": json.dumps(HOOKS)}


def test_template_root_and_seed_pass_the_check(tmp_path):
    assert check_spec(TEMPLATE, TEMPLATE, deploy=True).passed
    seed = tmp_path / "seed"
    shutil.copytree(TEMPLATE, seed, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("AGENTS.md", "ADAPTER.md"):
        (seed / name).write_text("# OfficeQA Skill\n\nNarrow to the right file first.\n")
    assert check_spec(seed, TEMPLATE, deploy=True).passed


def test_codex_layout_round_trip_and_validators(tmp_path):
    from harness2.core import tree as T
    from hermit.agent.evolver.methods.harness2_upstream import surface as U
    layout = U.layout_of(TEMPLATE)
    assert type(layout).__name__ == "CodexLayout" and layout.name == "cx" and layout.allow_subagents
    assert layout.from_adapter(TEMPLATE) == {"AGENTS.md": (TEMPLATE / "AGENTS.md").read_text()}   # stubs are not the tree
    tree = full_tree()
    census = T.validate(layout.surface, tree, allow_subagents=True)
    assert census["skills"] == 1 and census["sub_agents"] == 1 and census["scripts"] == 1 and census["hooks"] == 1
    dest = tmp_path / "adapter"
    layout.to_adapter(tree, TEMPLATE, dest)
    assert layout.from_adapter(dest) == tree                                 # every component survives, as upstream's paths
    assert (dest / "ADAPTER.md").read_text() == tree["AGENTS.md"]            # the context export is the always-on text
    assert (dest / ".codex" / "README.md").read_text() == dict(layout.surface.stubs)[".codex/README.md"]
    assert check_spec(dest, TEMPLATE, deploy=True).passed
    bad = dict(tree, **{"AGENTS.md": "Do the work.\n", ".codex/hooks.json": "{}"})   # no trigger line; no hooks object
    wrong = tmp_path / "wrong"
    layout.to_adapter(bad, TEMPLATE, wrong)
    (wrong / "notes.txt").write_text("x")
    (wrong / "ADAPTER.md").write_text("drift")
    problems = " | ".join(i.problem for i in check_spec(wrong, TEMPLATE, deploy=True).issues)
    for expected in ("notes.txt is off the Codex harness surface", "skill 'tables' is never named",
                     'hooks.json needs a top-level {"hooks"', "ADAPTER.md is not AGENTS.md"):
        assert expected in problems, (expected, problems)
    assert layout.off_surface(tree) == []
    with pytest.raises(ValueError):                                          # only the Codex template is trained
        U.layout_of(ROOT / "hermit" / "adapter" / "templates" / "prompt_only")


def test_bench_reads_a_codex_session(tmp_path):
    """The bench renders a Codex session's event stream as the trajectory and task.md as the task prompt."""
    from hermit.agent.evolver.methods.harness2_upstream.bench import FrameworkBench
    from harness2.core.spec import BenchPrompts, ChannelSpec
    seed = tmp_path / "seed"
    shutil.copytree(TEMPLATE, seed, ignore=shutil.ignore_patterns("__pycache__"))
    material = tmp_path / "material" / "current"
    log = material / "logs" / "t0"
    log.mkdir(parents=True)
    events = [{"type": "thread.started", "thread_id": "x"},
              {"type": "item.completed", "item": {"id": "1", "type": "command_execution", "command": "rg -n 1934 docs/",
                                                   "aggregated_output": "docs/a.txt:3: 1934 ... 557", "exit_code": 0}},
              {"type": "item.completed", "item": {"id": "2", "type": "agent_message", "text": "<answer>557</answer>"}},
              {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}]
    (log / "agent-codex.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (log / "task.md").write_text("## Question\nWhat was it in 1934?\n")
    (log / "prompt.txt").write_text("Read `task.md` and answer.")
    ctx = SimpleNamespace(workspace=SimpleNamespace(adapter=seed, learning_material=material.parent, output=tmp_path / "out"),
                          tools={})
    (tmp_path / "out").mkdir()
    record = {"id": "t0", "input": {"id": "t0"}, "status": "ok", "output": {"answer": "557", "fail_reason": ""},
              "judge": {"score": 1.0}, "trajectory": "logs/t0"}
    channel = ChannelSpec("claude_cli", "m", effort="medium", timeout=60)
    bench = FrameworkBench(ctx, record, prompts=BenchPrompts(work="Answering a question."), channel=channel,
                           runs_root=tmp_path / "h2", domain="d", name="toy", obs_trunc=10000)
    assert bench.substrate.name == "cx" and bench.spec.allow_subagents and bench.spec.install_mode == "codex_root"
    assert bench.task_prompt("t0").startswith("Read `task.md` and answer.\n\n# task.md\n\n## Question")
    run_dir = bench.execute("t0", None, "base1")                                # the reference: the Trainer's rollout
    text = bench.trajectory(run_dir, obs_trunc=10000)
    assert "[ACTION] shell: rg -n 1934 docs/" in text and "[OBSERVATION] docs/a.txt:3" in text
    assert "[ASSISTANT] <answer>557</answer>" in text
    assert bench.deliverable_text(run_dir) == "557" and bench.score(run_dir, "t0")["score"] == 1.0
    h0 = bench.base_harness()
    assert (h0 / "AGENTS.md").read_text() == (TEMPLATE / "AGENTS.md").read_text()
    assert (h0 / ".codex" / "README.md").is_file()                           # the editors see the placeholders


def test_bench_reads_a_claude_session(tmp_path, monkeypatch):
    """A Claude Code execution (the harness2-codex tree deployed on Claude Code) reaches the editors in upstream's
    grammar: every tool's call and result (two alike are two), errors, what a hook printed, a sub-agent's turns and
    hand-back, the clip marker; never the login's token."""
    from hermit.agent.evolver.methods.harness2_upstream.bench import FrameworkBench
    from harness2.core.spec import BenchPrompts, ChannelSpec
    token = "sk-ant-oat01-" + "x" * 80
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    seed = tmp_path / "seed"
    shutil.copytree(TEMPLATE, seed, ignore=shutil.ignore_patterns("__pycache__"))
    material = tmp_path / "material" / "current"
    log = material / "logs" / "t0"
    log.mkdir(parents=True)

    def assistant(*blocks, **extra):
        return {"type": "assistant", "message": {"id": "m", "role": "assistant", "content": list(blocks)}, **extra}

    def results(*blocks, **extra):
        return {"type": "user", "message": {"role": "user", "content": list(blocks)}, **extra}
    events = [{"type": "system", "subtype": "init", "model": "claude-haiku-5-5", "tools": ["Bash", "Read"],
               "agents": ["reader"], "skills": ["tables"]},
              {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 3},
              assistant({"type": "thinking", "thinking": "look for 1934"}),
              assistant({"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "rg -n 1934 docs/"}}),
              results({"type": "tool_result", "tool_use_id": "a", "content": "docs/a.txt:3: 1934 ... 557"}),
              assistant({"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "docs/a.txt"}}),
              results({"type": "tool_result", "tool_use_id": "b",
                       "content": [{"type": "text", "text": "row " * 5000}]}),
              assistant({"type": "tool_use", "id": "c", "name": "Bash", "input": {"command": "cat /proc/$PPID/environ"}}),
              results({"type": "tool_result", "tool_use_id": "c", "is_error": True,
                       "content": f"CLAUDE_CODE_OAUTH_TOKEN={token} exit 1"}),
              assistant({"type": "tool_use", "id": "d", "name": "Bash", "input": {"command": "grep -c 557 docs/a.txt"}}),
              assistant({"type": "tool_use", "id": "e", "name": "Bash", "input": {"command": "grep -c 557 docs/b.txt"}}),
              results({"type": "tool_result", "tool_use_id": "d", "content": "2"}),
              results({"type": "tool_result", "tool_use_id": "e", "content": "2"}),
              # as `--include-hook-events` prints them (the real CLI's fields; its test is in tests/core)
              {"type": "system", "subtype": "hook_started", "hook_name": "PostToolUse:Bash", "hook_event": "PostToolUse"},
              {"type": "system", "subtype": "hook_response", "hook_name": "PostToolUse:Bash", "hook_event": "PostToolUse",
               "output": '{"decision":"block","reason":"check the unit"}\n', "stderr": "", "exit_code": 0,
               "outcome": "success"},
              {"type": "system", "subtype": "hook_response", "hook_name": "PostToolUse:Bash", "output": "",
               "exit_code": 0, "outcome": "success"},                       # a hook that said nothing: no line
              assistant({"type": "text", "text": "summary of the file"}, parent_tool_use_id="t1"),
              {"type": "system", "subtype": "task_notification", "task_id": "t1", "status": "completed",
               "summary": "the table is on page 3"},
              results({"type": "text", "text": "<system-reminder>a reminder</system-reminder>"}),
              {"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 10, "error": "overloaded",
               "error_status": 529},
              {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
              assistant({"type": "text", "text": "<answer>557</answer>"}),
              {"type": "result", "subtype": "success", "is_error": False, "num_turns": 4, "result": "<answer>557</answer>",
               "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 70,
                         "output_tokens": 5}}]
    (log / "agent-claude.jsonl").write_text("not json\n" + "".join(json.dumps(e) + "\n" for e in events) + "[1]\n")
    (log / "task.md").write_text("## Question\nWhat was it in 1934?\n")
    (log / "prompt.txt").write_text("Read `task.md` and answer.")
    ctx = SimpleNamespace(workspace=SimpleNamespace(adapter=seed, learning_material=material.parent, output=tmp_path / "out"),
                          tools={})
    (tmp_path / "out").mkdir()
    record = {"id": "t0", "input": {"id": "t0"}, "status": "ok", "output": {"answer": "557", "fail_reason": ""},
              "judge": {"score": 1.0}, "trajectory": "logs/t0"}
    channel = ChannelSpec("claude_cli", "claude-haiku-5-5", effort="medium", timeout=60)
    bench = FrameworkBench(ctx, record, prompts=BenchPrompts(work="Answering a question."), channel=channel,
                           runs_root=tmp_path / "h2", domain="d", name="toy", obs_trunc=10000)
    assert bench.task_prompt("t0").startswith("Read `task.md` and answer.\n\n# task.md\n\n## Question")
    text = bench.trajectory(bench.execute("t0", None, "base1"), obs_trunc=10000)
    lines = text.splitlines()
    assert lines[0] == "[HARNESS] session started | model=claude-haiku-5-5 tools=Bash,Read agents=reader skills=tables"
    assert "[REASONING] look for 1934" in lines and "[ACTION] Bash: rg -n 1934 docs/" in lines
    assert "[OBSERVATION] docs/a.txt:3: 1934 ... 557" in lines
    assert '[ACTION] Read: {"file_path": "docs/a.txt"}' in lines
    read = next(line for line in lines if line.startswith("[OBSERVATION] row"))
    assert read.endswith("…(+10000 chars — render truncation)") and len(read) < 10100   # upstream's clip and marker
    assert "[OBSERVATION ERROR] CLAUDE_CODE_OAUTH_TOKEN=<CLAUDE_CODE_OAUTH_TOKEN> exit 1" in lines
    assert token not in text
    assert lines.count("[OBSERVATION] 2") == 2                    # two calls alike, two results
    assert ('[HARNESS] hook PostToolUse:Bash (exit 0, success): {"decision":"block","reason":"check the unit"}'
            in lines) and sum(line.startswith("[HARNESS] hook") for line in lines) == 1
    assert "[ASSISTANT] (sub-agent) summary of the file" in lines
    assert "[HARNESS] task_notification: completed | the table is on page 3" in lines
    assert "[HARNESS] <system-reminder>a reminder</system-reminder>" in lines
    assert "[HARNESS] api retry 1/10: overloaded 529" in lines
    assert lines[-2:] == ["[ASSISTANT] <answer>557</answer>",
                          "[HARNESS] session completed | turns=4 tokens in=100 cached=70 out=5"]
    from hermit.agent.evolver.methods.harness2_upstream.render import render_claude_events
    failed = log / "failed.jsonl"
    failed.write_text(json.dumps(assistant({"type": "text", "text": "API Error: 500"}, error="server_error")) + "\n" +
                      json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "API Error: 500",
                                  "num_turns": 1, "usage": {}}) + "\n")
    rendered = render_claude_events(failed, obs_trunc=100, max_chars=1000).splitlines()
    assert rendered == ["[ERROR] API Error: 500",                 # the failed request, then the result (one line)
                        "[HARNESS] session completed | turns=1 tokens in=0 cached=None out=None"]
    assert render_claude_events(log / "missing.jsonl", obs_trunc=100, max_chars=10).startswith("(failed to read")
    assert render_claude_events(log / "task.md", obs_trunc=100, max_chars=10) == "(no parseable events in trajectory)"


def test_a_claude_event_whose_text_holds_a_unicode_line_separator_is_rendered_whole(tmp_path):
    """Claude Code's JSON.stringify leaves U+0085, U+2028 and U+2029 unescaped in an event's strings, where
    str.splitlines() ends a line: such an event is read whole, so its tool call, the call's result and the message
    reach the editors in upstream's grammar."""
    from hermit.agent.evolver.methods.harness2_upstream.render import render_claude_events
    for sep in ("\x85", "\u2028", "\u2029"):
        events = [{"type": "assistant", "message": {"role": "assistant", "content": [
                      {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": f"printf 'a{sep}b'"}}]}},
                  {"type": "user", "message": {"role": "user", "content": [
                      {"type": "tool_result", "tool_use_id": "a", "content": f"a{sep}b"}]}},
                  {"type": "assistant", "message": {"role": "assistant", "content": [
                      {"type": "text", "text": f"<answer>557</answer>{sep}done"}]}}]
        log = tmp_path / "agent-claude.jsonl"
        log.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
        assert render_claude_events(log, obs_trunc=100, max_chars=1000).split("\n") == [
            f"[ACTION] Bash: printf 'a{sep}b'", f"[OBSERVATION] a{sep}b", f"[ASSISTANT] <answer>557</answer>{sep}done"
        ], repr(sep)


def test_bench_reads_a_harbor_trial_without_its_verifier(tmp_path):
    """A Harbor trial (Claude Code in the task's container): the task is its instruction, the trajectory the session's
    own stream (agent/claude-code.txt), the deliverable its final message; nothing of the verifier (its output, its
    reward, which the trial's output also holds) reaches what the editors see."""
    from hermit.agent.evolver.methods.harness2_upstream.bench import FrameworkBench
    from harness2.core.spec import BenchPrompts, ChannelSpec
    seed = tmp_path / "seed"
    shutil.copytree(TEMPLATE, seed, ignore=shutil.ignore_patterns("__pycache__"))
    material = tmp_path / "material" / "current"
    log = material / "trajectories" / "fix-git_1"
    (log / "agent").mkdir(parents=True)
    (log / "verifier").mkdir()
    events = [{"type": "system", "subtype": "init", "model": "claude-haiku-5-5", "tools": ["Bash"],
               "apiKeySource": "none"},
              {"type": "assistant", "message": {"id": "m", "role": "assistant", "content": [
                  {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "git reflog | head"}}]}},
              {"type": "user", "message": {"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": "a",
                   "content": "c499730 HEAD@{1}: commit: Move to Stanford"}]}},
              {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
               "result": "The changes are back on master as 86a9dd7.", "usage": {}}]
    (log / "agent" / "claude-code.txt").write_text("".join(json.dumps(e) + "\n" for e in events))
    (log / "verifier" / "test-stdout.txt").write_text("FAILED test_outputs.py::test_merge - AssertionError: VERIFIER\n")
    (log / "verifier" / "reward.txt").write_text("1\n")
    (log / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": 1.0}}}))
    ctx = SimpleNamespace(workspace=SimpleNamespace(adapter=seed, learning_material=material.parent, output=tmp_path / "out"),
                          tools={})
    (tmp_path / "out").mkdir()
    row = {"id": "fix-git#1", "harbor_task": "fix-git", "instruction": "Find my lost changes and merge them.",
           "metadata": {"difficulty": "easy"}, "docker_image": "x/fix-git:1", "rollout": 1}
    record = {"id": "fix-git#1", "input": row, "status": "ok", "judge": {"score": 1.0},
              "trajectory": "trajectories/fix-git_1",
              "output": {"harbor_task": "fix-git", "rollout": 1, "reward": 1.0, "rewards": {"reward": 1.0},
                         "status": "ok"}}
    bench = FrameworkBench(ctx, record, prompts=BenchPrompts(work="Solving a terminal task."),
                           channel=ChannelSpec("claude_cli", "claude-haiku-5-5", effort="medium", timeout=60),
                           runs_root=tmp_path / "h2", domain="default", name="terminal-bench-2-1", obs_trunc=10000)
    assert bench.task_prompt("fix-git#1") == "Find my lost changes and merge them."
    run = bench.execute("fix-git#1", None, "base1")
    seen = (bench.trajectory(run, obs_trunc=10000) + bench.deliverable_text(run)
            + json.dumps(bench.deliverable_files(run)))
    assert "[ACTION] Bash: git reflog | head" in seen and "c499730 HEAD@{1}" in seen
    assert bench.deliverable_files(run) == {"final_message.txt": "The changes are back on master as 86a9dd7."}
    assert bench.delivered(run) and "VERIFIER" not in seen and "reward" not in seen
    assert bench.score(run, "fix-git#1")["score"] == 1.0           # the judge's, read only through score()


def _harbor_trial(tmp_path, fill):
    """The bench over a Harbor trial's record whose curated log `fill(log)` writes the agent's files into (the
    verifier's output beside them), and the reference execution of it."""
    from hermit.agent.evolver.methods.harness2_upstream.bench import FrameworkBench
    from harness2.core.spec import BenchPrompts, ChannelSpec
    seed = tmp_path / "seed"
    shutil.copytree(TEMPLATE, seed, ignore=shutil.ignore_patterns("__pycache__"))
    material = tmp_path / "material" / "current"
    log = material / "trajectories" / "t_1"
    (log / "agent").mkdir(parents=True)
    (log / "verifier").mkdir()
    (log / "verifier" / "test-stdout.txt").write_text("FAILED test_outputs.py::test_sum - AssertionError: VERIFIER\n")
    fill(log)
    ctx = SimpleNamespace(workspace=SimpleNamespace(adapter=seed, learning_material=material.parent,
                                                    output=tmp_path / "out"), tools={})
    (tmp_path / "out").mkdir()
    row = {"id": "t#1", "harbor_task": "t", "instruction": "Add the numbers.", "rollout": 1}
    record = {"id": "t#1", "input": row, "status": "ok", "judge": {"score": 0.0}, "trajectory": "trajectories/t_1",
              "output": {"harbor_task": "t", "rollout": 1, "reward": 0.0, "rewards": {"reward": 0.0}, "status": "ok"}}
    bench = FrameworkBench(ctx, record, prompts=BenchPrompts(work="Solving a terminal task."),
                           channel=ChannelSpec("claude_cli", "claude-haiku-5-5", effort="medium", timeout=60),
                           runs_root=tmp_path / "h2", domain="default", name="terminal-bench", obs_trunc=10000)
    return bench, bench.execute("t#1", None, "base1")


CODEX_EVENTS = [{"type": "thread.started", "thread_id": "th1"},
                {"type": "item.completed", "item": {"type": "command_execution", "command": "python3 add.py",
                                                    "exit_code": 0, "aggregated_output": "5\n"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "The sum is 5."}},
                {"type": "turn.completed", "usage": {"input_tokens": 3, "cached_input_tokens": 0, "output_tokens": 1}}]
MINI_EVENTS = [{"type": "session.started", "session": "s1", "model": "openai/m"},
               {"type": "message", "message": {"role": "assistant", "content": "Adding them.",
                                               "actions": ["python3 add.py"]}},
               {"type": "message", "message": {"role": "tool", "content": "", "returncode": 0, "output": "5\n"}},
               {"type": "message", "message": {"role": "exit", "content": "", "exit_status": "Submitted",
                                               "submission": "The sum is 5."}}]
# Each CLI's stream as a Harbor trial keeps it (agent/agent-<backend>.jsonl), a line the editors see of it, and the
# session's final message
HARBOR_SESSIONS = {"codex": ("".join(json.dumps(e) + "\n" for e in CODEX_EVENTS), "[ACTION] shell: python3 add.py",
                             "The sum is 5."),
                   "claude": ((ROOT / "tests/fixtures/claude_streams/reply.jsonl").read_text(),
                              "[ASSISTANT] mock-reply", "mock-reply"),
                   "pi": ((ROOT / "tests/fixtures/pi/success.jsonl").read_text(), "[ASSISTANT] the answer",
                          "the answer"),
                   "opencode": ((ROOT / "tests/fixtures/opencode/success.jsonl").read_text(),
                                "[ACTION] shell: echo hi && exit 3", "the answer"),
                   "dsh": ((ROOT / "tests/fixtures/dsh/success.jsonl").read_text(),
                           "[OBSERVATION ERROR] hi | exit_code=3", "the answer"),
                   "mini-swe-agent": ("".join(json.dumps(e) + "\n" for e in MINI_EVENTS),
                                      "[ACTION] shell: python3 add.py", "The sum is 5.")}


@pytest.mark.parametrize("backend", sorted(HARBOR_SESSIONS))
def test_bench_reads_a_harbor_trial_of_any_cli(tmp_path, backend):
    """A Harbor trial of a HermitXXX keeps its CLI's stream under agent/ (agent/agent-<backend>.jsonl), whichever CLI
    it ran: the editors see that session's trajectory in upstream's grammar (Codex's by upstream's renderer, Claude
    Code's and Pi's by the method's, the others' from the common reader's steps) and its final message as the
    deliverable, read as its Base Agent reads it; nothing of the verifier."""
    stream, line, final = HARBOR_SESSIONS[backend]
    bench, run = _harbor_trial(tmp_path, lambda log: (log / "agent" / f"agent-{backend}.jsonl").write_text(stream))
    seen = bench.trajectory(run, obs_trunc=10000)
    assert line in seen.splitlines() and "(no trajectory" not in seen
    assert bench.deliverable_files(run) == {"final_message.txt": final}
    assert "VERIFIER" not in seen + bench.deliverable_text(run)


@pytest.mark.parametrize("case, final", [("two_blocks", "first-text\n\nsecond-text"),
                                         ("cut_then_continued", "part-one part-two"),
                                         ("always_cut", "")])
def test_a_claude_trials_final_message_is_its_whole_last_turn_and_none_when_its_result_is_an_error(tmp_path, case,
                                                                                                     final):
    """A Claude Code trial's final message is its reply as the Base Agent reads it: every text block of the last turn
    (two blocks; a message the CLI continued past its output limit, joined where it was cut), where the result's own
    text keeps only the last block; a session whose result is an error gives none, whatever text it wrote."""
    from hermit.agent.evolver.methods.harness2_upstream.bench import final_message
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "agent-claude.jsonl").write_text(
        (ROOT / "tests" / "fixtures" / "claude_streams" / f"{case}.jsonl").read_text())
    assert final_message(tmp_path) == final


def test_bench_reads_a_codex_trials_collected_last_message_the_answer_agents_answer_and_harbors_own_atif(tmp_path):
    """A Codex trial's final message is its `-o` file, which the runner collected from the task's container
    (agent/hermit-collect/last_message.txt); the answer-only agent's is its answer (agent/answer.json); one of
    Harbor's own agents leaves only its ATIF trajectory, which gives the editors its steps and its last message."""
    def codex(log):
        (log / "agent" / "agent-codex.jsonl").write_text("".join(json.dumps(e) + "\n" for e in CODEX_EVENTS))
        (log / "agent" / "hermit-collect").mkdir()
        (log / "agent" / "hermit-collect" / "last_message.txt").write_text("The sum is 5, checked twice.")
    bench, run = _harbor_trial(tmp_path / "codex", codex)
    assert bench.deliverable_text(run) == "The sum is 5, checked twice."
    bench, run = _harbor_trial(tmp_path / "answer", lambda log: (log / "agent" / "answer.json").write_text(
        json.dumps({"reply": "<answer>5</answer>", "answer": "5", "usage": {}, "model": "m"})))
    assert bench.deliverable_files(run) == {"final_message.txt": "5"}
    atif = {"schema_version": "ATIF-v1.7", "agent": {"name": "terminus-2"}, "steps": [
        {"step_id": 1, "source": "user", "message": "Add the numbers."},
        {"step_id": 2, "source": "agent", "message": "Adding them.", "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash_command", "arguments": {"keystrokes": "python3 add.py\n"}}],
         "observation": {"results": [{"source_call_id": "c1", "content": "5"}]}},
        {"step_id": 3, "source": "agent", "message": "The sum is 5."}]}
    bench, run = _harbor_trial(tmp_path / "atif", lambda log: (log / "agent" / "trajectory.json").write_text(
        json.dumps(atif)))
    assert bench.trajectory(run, obs_trunc=10000).splitlines() == [
        "[ASSISTANT] Adding them.", "[ACTION] shell: python3 add.py", "[OBSERVATION] 5", "[ASSISTANT] The sum is 5."]
    assert bench.deliverable_text(run) == "The sum is 5."


# The scripted channel on the Codex tree: the INV and DOM editors add a triggered skill, the composer writes the rule
# the toy pipeline needs into AGENTS.md, the reflector writes a lesson naming AGENTS.md.
PROPOSER = r'''
import json, shutil, tempfile
from pathlib import Path
from harness2.core.proposer import Reply
from hermit.agent.evolver.methods.harness2_upstream.channel import FrameworkProposer

LEAKS = ("expected the sum", '"score"', '"judge"', '"feedback"')


class Scripted(FrameworkProposer):
    def call(self, mission, context_dir, label, **kwargs):
        root = Path(context_dir)
        for f in root.rglob("*"):
            if f.is_file():
                body = f.read_text(errors="replace")
                assert not any(x in body for x in LEAKS), (label, f, body[:200])
        with open(self.params["calls_log"], "a") as out:
            out.write(json.dumps({"label": label, "files": sorted(str(p.relative_to(root)) for p in root.rglob("*")
                                                                   if p.is_file()), "mission": mission[:400]}) + "\n")
        if label.startswith("experience."):
            text = ("<edits>\ncomponent: AGENTS.md\ncandidates: agg1\nchange: the rule\n"
                    "observed: the deliverable became the sum\nverdict: helped\n</edits>\n<experience>\n"
                    "cue: adding numbers\nlesson: state the operation in AGENTS.md\n"
                    "evidence: the agg1 rollout answered with the sum\nconfidence: high\n</experience>")
            return Reply(label=label, text=text, ok=True, attempts=1)
        work = Path(tempfile.mkdtemp()) / "harness"
        shutil.copytree(root / "harness", work)
        if label.startswith("agg"):                   # the composer keeps the INV candidate's skill and adds the rule
            shutil.copytree(root / "pool" / "inv1" / "harness" / ".agents" / "skills" / "sum_check",
                            work / ".agents" / "skills" / "sum_check")
            (work / "AGENTS.md").write_text("Always add the numbers.\n- When a sum must be checked, read "
                                            ".agents/skills/sum_check/SKILL.md and follow it.\n")
        else:
            (work / ".agents" / "skills" / "sum_check" / "scripts").mkdir(parents=True, exist_ok=True)
            (work / ".agents" / "skills" / "sum_check" / "SKILL.md").write_text(
                "---\ndescription: checking a sum before answering\n---\n"
                "Run python3 .agents/skills/sum_check/scripts/add.py and compare.\n")
            (work / ".agents" / "skills" / "sum_check" / "scripts" / "add.py").write_text("print(1 + 1)\n")
            (work / "AGENTS.md").write_text((work / "AGENTS.md").read_text().rstrip() + "\n- When a sum must be "
                                            "checked, read .agents/skills/sum_check/SKILL.md and follow it.\n")
        return Reply(label=label, text=f"<notes>{label}: edited</notes>", ok=True, attempts=1, workspace=work)
'''


def test_paper_protocol_on_the_codex_template(tmp_path):
    (tmp_path / "proposer.py").write_text(PROPOSER)
    (tmp_path / "slots.yaml").write_text("work: Adding two numbers given in the task.\ndeliverable_word: sum\n")
    path = make_task(tmp_path, template=str(TEMPLATE),
                     evolver={"name": "harness2_upstream",
                              "params": {"proposer": "proposer.py:Scripted", "backend": None, "model": "scripted",
                                         "slots": "slots.yaml", "name": "toy",
                                         "calls_log": str(tmp_path / "calls.jsonl")}},
                     trainer={"run_name": "h2cx", "output_dir": "runs", "epochs": 1, "budget": {"usd": 5}})
    (tmp_path / "stream.txt").write_text("t0\nt1\n")
    Trainer(Config.load(path, ["data.train=stream.txt", "data.step_size=1", "data.shuffle=false"])).train()
    run_dir = tmp_path / "runs" / "h2cx"
    steps = [json.loads(l) for l in (run_dir / "steps.jsonl").read_text().splitlines()]
    assert [s["tasks"] for s in steps] == [["t0"], ["t1"]] and not any(s["changed"] for s in steps)
    adapter = run_dir / "workspace" / "adapter"
    assert (adapter / "AGENTS.md").read_text() == (TEMPLATE / "AGENTS.md").read_text()      # H0 every task
    for i in (1, 2):
        r = json.loads((run_dir / f"step_{i:04d}" / "step_result.json").read_text())["extra"]["harness2"]
        assert r["status"] == "ok" and r["selection"]["chosen_label"] == "agg1"
        assert r["runs"]["base1"]["score"] == 0.0 and r["runs"]["agg1"]["score"] == 1.0
        assert r["library"]["added"] == [f"t{i - 1}"] and not any(s["fell_back"] for s in r["stages"])
    ns = next((run_dir / "step_0001" / "output" / "h2").glob("toy-cx-agg1"))
    entry = run_dir / "workspace" / "state" / "library" / "agg" / "default" / "harnesses" / "t0"
    assert (entry / "harness" / "AGENTS.md").read_text().startswith("Always add the numbers.")
    assert (entry / "harness" / ".agents" / "skills" / "sum_check" / "scripts" / "add.py").is_file()
    assert "lesson**: state the operation in AGENTS.md" in (entry / "EXPERIENCE.md").read_text()
    calls = [json.loads(l) for l in (tmp_path / "calls.jsonl").read_text().splitlines()]
    inv = next(c for c in calls if c["label"] == "inv1.a1")
    assert "harness/AGENTS.md" in inv["files"] and "harness/.codex/README.md" in inv["files"]
    assert "harness/.agents/skills/README.md" in inv["files"]                 # the on-demand levers' placeholders
    probe = next((ns / "t0").glob("harnesses/inv1/.agents/skills/sum_check/SKILL.md"))
    assert probe.is_file()


def test_bench_says_an_execution_failed_rather_than_answered_wrong(tmp_path):
    """A session that failed scores 0, as in the Trainer's records (status error), and the step's records say why: it
    is not a wrong answer. An answer the editors see has no login token in it."""
    from hermit.agent.evolver.methods.harness2_upstream.bench import FrameworkBench
    from harness2.core.spec import BenchPrompts, ChannelSpec
    seed = tmp_path / "seed"
    shutil.copytree(TEMPLATE, seed, ignore=shutil.ignore_patterns("__pycache__"))
    material = tmp_path / "material" / "current"
    material.mkdir(parents=True)
    failed = {"id": "t0", "status": "error", "error": "the agent's session failed: overloaded", "output": None,
              "judge": {"score": 0.0, "feedback": "the agent's session failed: overloaded"}, "log": None}
    ctx = SimpleNamespace(workspace=SimpleNamespace(adapter=seed, learning_material=material.parent,
                                                    output=tmp_path / "out"),
                          tools={"run_agent": lambda task_ids, adapter: {"results": [failed]}})
    (tmp_path / "out").mkdir()
    record = {"id": "t0", "input": {"id": "t0"}, **failed}          # the Trainer's own rollout failed too: run again
    bench = FrameworkBench(ctx, record, prompts=BenchPrompts(work="Answering a question."),
                           channel=ChannelSpec("claude_cli", "claude-haiku-5-5", effort="medium", timeout=60),
                           runs_root=tmp_path / "h2", domain="d", name="toy", obs_trunc=10000)
    out = bench.execute("t0", None, "base1")
    assert bench.score(out, "t0")["score"] == 0.0 and bench.private["base1"]["error"] == failed["error"]
    line = json.loads((tmp_path / "out" / "scores.jsonl").read_text().splitlines()[-1])
    assert line == {"run_id": "base1", "answered": False, "score": 0.0, "error": failed["error"]}
