"""OfficeQA's judge in a Harbor task's container (utils/harbor/judge.py, which build_tasks.py copies into every task's
tests/): `find_answer` takes the `<answer>` of the final message from the Codex event stream a trial leaves in
/logs/agent, HermitCodex's agent-codex.jsonl or the codex.txt Harbor's own Codex agent writes. The container's paths
(the module's own ANSWER_FILE and STREAMS) are moved under tmp_path; the stream has the shape of those the OfficeQA
GPT-5.6-Luna runs kept (Codex 0.164.0, `codex exec --json`): Codex's stderr line first, then its events. The name of
HermitCodex's stream is checked against the launch plan Hermit renders for a Codex trial (hermit.harbor.launch)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

JUDGE = Path(__file__).resolve().parents[1] / "harbor" / "judge.py"
COMMAND = {"id": "item_1", "type": "command_execution", "command": "/bin/bash -lc 'rg -n receipts docs/'"}
STREAM = ["Reading additional input from stdin...",
          {"type": "thread.started", "thread_id": "019a0000-0000-7000-8000-000000000000"},
          {"type": "turn.started"},
          {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Searching the docs."}},
          {"type": "item.started", "item": {**COMMAND, "aggregated_output": "", "exit_code": None,
                                            "status": "in_progress"}},
          {"type": "item.completed", "item": {**COMMAND, "aggregated_output": "receipts 44,463\n", "exit_code": 0,
                                              "status": "completed"}},
          {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message",
                                              "text": "The 1944 receipts were <answer>44,463</answer>."}},
          {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 50}}]


def load():
    """The judge module as the container runs it, with its own paths."""
    spec = importlib.util.spec_from_file_location("officeqa_harbor_judge", JUDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def judge(tmp_path, monkeypatch):
    """The judge with each container path it reads at the same place under tmp_path (/logs/agent/x at
    tmp_path/logs/agent/x)."""
    module = load()
    monkeypatch.setattr(module, "ANSWER_FILE", tmp_path / module.ANSWER_FILE.relative_to("/"))
    monkeypatch.setattr(module, "STREAMS", tuple(tmp_path / path.relative_to("/") for path in module.STREAMS))
    for path in module.STREAMS:
        path.parent.mkdir(parents=True, exist_ok=True)
    return module


def write(path: Path, stream: list) -> None:
    """The stream as Codex writes it: one line per event, non-ASCII characters unescaped (serde_json's way)."""
    lines = [line if isinstance(line, str) else json.dumps(line, ensure_ascii=False) for line in stream]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize("stream", ["agent-codex.jsonl", "codex.txt"])
def test_an_answer_in_the_final_message_is_found_in_either_codex_stream(judge, stream):
    """HermitCodex keeps the session's stream in agent-codex.jsonl; the judge read only codex.txt, which Harbor's own
    Codex agent writes, so every answer given in a HermitCodex trial's final message scored 0."""
    found = [path for path in judge.STREAMS if path.name == stream]
    assert len(found) == 1
    write(found[0], STREAM)
    assert judge.find_answer() == ("44,463", "final message")


def test_the_judge_reads_the_stream_the_launch_plan_names_for_a_codex_trial(tmp_path, monkeypatch):
    """harborside writes a session's stream to /logs/agent/<the plan's stream>, a name Hermit's launch plan gives
    (agent-<backend>.jsonl): a judge reading another name finds no final message, and every answer scores 0."""
    from hermit.agent.model.backends import Binding
    from hermit.harbor import launch
    monkeypatch.setenv("FAKE_MODEL_KEY", "fake-model-key")
    binding = Binding.from_spec({"ref": "fake/glm-5", "backend": "fake", "model": "glm-5",
                                 "base_url": "https://models.example/v1", "api_key_env": "FAKE_MODEL_KEY",
                                 "key_required": True, "apis": ["chat", "responses", "messages"]})
    plan = json.loads(launch.render("codex", launch.settings_for("codex", binding=binding, web_search=False),
                                    deploy=None, tools_url=None, job=tmp_path / "job").read_text())
    assert load().STREAMS[0] == Path("/logs/agent") / plan["stream"]


def test_a_line_of_codexs_stderr_that_is_json_but_no_object_is_skipped(judge):
    """Codex's stderr shares the stream's file (harborside: `> stream 2>&1`): a line of it that parses as JSON but is
    no object, a bare number, raised AttributeError in the judge before it wrote its reward, and test.sh's fallback
    scored the trial 0: a correct answer lost."""
    write(judge.STREAMS[0], ["429", "null", *STREAM])
    assert judge.find_answer() == ("44,463", "final message")


def test_a_line_nested_past_the_json_parsers_depth_is_skipped(judge):
    """A line of the stream's file nested deeper than json.loads can parse raises RecursionError, not ValueError: the
    judge caught only ValueError, so it crashed before writing its reward and test.sh's fallback scored the trial 0 (a
    correct answer lost), where Hermit's readers skip the line."""
    write(judge.STREAMS[0], ["[" * 100_000 + "]" * 100_000, *STREAM])
    assert judge.find_answer() == ("44,463", "final message")


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_a_final_message_holding_a_unicode_line_separator_is_read_as_one_event(judge, separator):
    """Codex leaves U+2028, U+2029 and U+0085 unescaped in an event's strings: the judge split the stream with
    str.splitlines(), which ends a line at each, so the final message's event came apart into pieces that are no JSON,
    the message before it was read as the final one, and its correct answer scored 0."""
    text = f"Receipts (from the bulletin):{separator}<answer>44,463</answer>."
    final = {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": text}}
    write(judge.STREAMS[0], [*STREAM[:-2], final, STREAM[-1]])
    assert separator in judge.STREAMS[0].read_text(encoding="utf-8")         # unescaped, as Codex writes it
    assert judge.find_answer() == ("44,463", "final message")
