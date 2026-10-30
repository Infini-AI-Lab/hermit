"""ReHarness end to end on the Trainer, offline: a scripted model plays the reflection and revision agents, a stub
`/embeddings` server stands in for the embedding model, and a toy stream of two kinds of task is solved only when a
retrieved reflection names the kind's rule (the forward read), after the Evolver wrote and revised it.
"""
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

import pytest
import yaml

from hermit import Session
from hermit.adapter.loader import load_manifest
from hermit.config import Config
from hermit.evaluate import evaluate
from hermit.evolver.base import Usage
from hermit.spec import check_spec, repair
from hermit.trainer.trainer import Trainer

TEMPLATE = Path(__file__).resolve().parents[1] / "hermit" / "algorithms" / "templates" / "reharness"
KINDS = {"ALPHA": "alpha: sort the numbers ascending", "BETA": "beta: reverse the letters of the word"}

PIPELINE = '''
from hermit import Session
from hermit.task import RunOutput

def run(task, adapter):
    session = Session(adapter) if adapter else None
    context = session.context(query=task["question"], task=task) if session else ""   # the forward read
    answer = "done" if f"RULE_{task['kind']}" in context else "unsure"
    messages = [{"role": "system", "content": context}, {"role": "user", "content": task["question"]},
                {"role": "assistant", "content": answer}]
    extra = {"context_tool": session.last_context} if session is not None and session.last_context is not None else {}
    if session is not None:
        extra["sandboxed"] = session.sandboxed          # whether the adapter's reader ran in a sandbox of its own
    return RunOutput(output=answer, messages=messages, cost_usd=0.0, tokens=0, extra=extra)
'''
JUDGE = '''
def score(task, output):
    ok = output.output == "done"
    return {"score": float(ok), "feedback": "CORRECT." if ok else f"INCORRECT. The adapter requires RULE_{task['kind']}."}
'''


class ScriptedModel:
    """The reflection agent writes the rule the feedback named; the revision agent adds use-case guidance."""
    model = "scripted"

    def __init__(self):
        self.calls = 0
        self.prompts = []

    def chat(self, messages, **kwargs):
        self.calls += 1
        system, user = messages[0]["content"], messages[-1]["content"]
        self.prompts.append((system.split(".")[0], user))
        if system.startswith("You are ReflectionAgent"):
            # the kind is in the task's text; what worked (or was missing) is the kind's rule
            task = user.split("[CURRENT TASK, TRAJECTORY, AND OUTCOME]", 1)[-1]
            kind = (re.search(r"\b(alpha|beta):", task) or [None, "unknown"])[1].upper()
            evidence = "the task failed without the rule" if "requires RULE_" in task else "the rule solved the task"
            content = json.dumps({"reflection": f"- evidence: {evidence}\n- strategy: for {kind} tasks apply RULE_{kind}"})
        elif system.startswith("You are RevisionAgent"):
            current = user.split("[CURRENT REFLECTION]\n", 1)[1].split("\n\n[TARGET MEMORY'S ORIGINAL TRAJECTORY]", 1)[0]
            content = json.dumps({"revised_reflection": current + "\n- guidance: applies when the task names the kind"})
        else:
            content = "{}"
        return {"content": content, "finish_reason": "stop"}, Usage("scripted", 10, 0, 10, 1, 0.0)


class StubEmbeddings(http.server.BaseHTTPRequestHandler):
    """Bag-of-words vectors in 64 dimensions: texts sharing words are close."""
    DIM = 64

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
        data = []
        for i, text in enumerate(texts):
            vector = [0.0] * self.DIM
            for token in re.findall(r"[a-z0-9]+", text.lower()):
                vector[hash(token) % self.DIM] += 1.0
            data.append({"index": i, "embedding": vector})
        payload = json.dumps({"data": data, "usage": {"prompt_tokens": sum(len(t) // 4 for t in texts)}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def embeddings():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubEmbeddings)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


@pytest.fixture
def workdir():
    """A directory outside /tmp: a sandboxed worker sees an empty tmpfs at /tmp."""
    import tempfile
    base = Path.home() / ".cache" / "hermit-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


def make_run(tmp: Path, params: dict, *, sandbox: bool = False, llm: bool = True) -> Config:
    (tmp / "task-info").mkdir()
    (tmp / "task-info" / "README.md").write_text("Toy tasks of two kinds; each kind has a rule.\n")
    rows = []
    for i in range(8):
        kind = "ALPHA" if i % 2 == 0 else "BETA"
        rows.append({"id": f"t{i:02d}", "kind": kind, "question": f"Task {i}. {KINDS[kind]} {i * 7}"})
    (tmp / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp / "train.txt").write_text("".join(f"t{i:02d}\n" for i in range(6)))
    (tmp / "test.txt").write_text("t06\nt07\n")
    (tmp / "pipeline.py").write_text(PIPELINE)
    (tmp / "judge.py").write_text(JUDGE)
    config = {"data": {"tasks": "tasks.jsonl", "train": "train.txt", "test": "test.txt", "step_size": 1,
                       "shuffle": False},
              "task_info": "task-info", "agent": {"name": "pipeline.py:run", "concurrency": 1},
              "judge": {"path": "judge.py"}, "template": str(TEMPLATE),
              "evolver": {"name": "reharness", "params": params},
              "sandbox": {"enabled": sandbox, "network": "none", "env": [], "enforce": "required"},
              "trainer": {"run_name": "reharness-toy", "output_dir": "runs", "epochs": 1, "budget": {"hours": 1}}}
    if llm:
        config["llm"] = {"base_url": "http://127.0.0.1:9/v1", "model": "scripted"}   # replaced by the scripted model
    (tmp / "run.yaml").write_text(yaml.safe_dump(config))
    return Config.load(tmp / "run.yaml")


def train(config: Config) -> tuple[dict, ScriptedModel]:
    trainer = Trainer(config, log=lambda *_: None)
    trainer.llm = model = ScriptedModel()
    return trainer.train(), model


def records(run: Path) -> list[dict]:
    return [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines() if l.strip()]


# ---------- the template and the per-task context hook ----------

def test_template_conforms_and_hides_its_context_tool():
    assert check_spec(TEMPLATE, TEMPLATE).passed
    session = Session(TEMPLATE)
    assert session.context_tool == "reflections" and session.tools == [] and not session.has_tool("reflections")
    assert [t["name"] for t in session.declared_tools] == ["reflections"]
    static = session.context()
    assert session.last_context is None
    with_query = session.context(query="sort the numbers", task={"id": "x"})
    assert with_query == static and session.last_context["bank_size"] == 0        # an empty bank adds nothing
    reply = session.respond({"id": "c1", "function": {"name": "reflections", "arguments": '{"query": "x"}'}})
    assert reply["content"].startswith("Error: ValueError: Unknown tool")      # the harness's tool, not the agent's


def test_context_tool_must_be_a_declared_tool(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    (a / "ADAPTER.md").write_text("x\n")
    (a / ".hermit").mkdir()
    (a / ".hermit" / "adapter.yaml").write_text("template: {id: x, version: '1'}\nstructure: {}\n")
    (a / "exports.yaml").write_text("context_tool: nope\n")
    with pytest.raises(ValueError, match="context_tool"):
        load_manifest(a)


def test_reader_ranks_renders_and_the_check_catches_a_broken_bank(tmp_path, embeddings):
    import sys
    sys.path.insert(0, str(TEMPLATE))
    from reharness_core import bank, render, retrieval
    adapter = tmp_path / "adapter"
    shutil.copytree(TEMPLATE, adapter)
    bank.write_index(adapter, {"k": 2, "retrieval": "bm25", "embedding_model": None, "dim": None})
    for i, (kind, text) in enumerate(KINDS.items()):
        bank.save_reflection(adapter, bank.Reflection(id=f"m{i}", task=f"m{i}", step=i + 1, query=text,
                                                      reflection=f"- use RULE_{kind}"))
    session = Session(adapter)
    context = session.context(query="please reverse the letters of this word", task={})
    assert context.endswith("[RANKED REFLECTIONS]\n[RANK 1 OF 2 REFLECTION]\n- use RULE_BETA\n\n"
                            "[RANK 2 OF 2 REFLECTION]\n- use RULE_ALPHA\n")
    assert [r["id"] for r in session.last_context["retrieved"]] == ["m1", "m0"]
    assert session.last_context["retrieval"] == "bm25" and session.last_context["k"] == 2
    # embedding retrieval through the reader's environment variable
    os.environ[retrieval.READER_URL_ENV] = embeddings
    try:
        bank.write_index(adapter, {"k": 1, "retrieval": "embedding", "embedding_model": "stub", "dim": None})
        client = retrieval.EmbeddingClient(embeddings, "stub")
        for r in bank.load_bank(adapter):
            r.embedding, r.model = client.embed([r.query])[0][0], "stub"
            bank.save_reflection(adapter, r)
        got = Session(adapter).context(query="sort the numbers ascending please", task={})
        assert "[RANK 1 OF 1 REFLECTION]\n- use RULE_ALPHA" in got
        assert check_spec(adapter, TEMPLATE).passed
        # a reflection embedded with another model fails the template's check, and repair restores the parent
        parent = tmp_path / "parent"
        shutil.copytree(adapter, parent)
        broken = bank.load_bank(adapter)[0]
        broken.model = "other"
        bank.save_reflection(adapter, broken)
        result = check_spec(adapter, TEMPLATE)
        assert not result.passed and any(i.check == 6 and "embedded with" in i.problem for i in result.issues)
        final, repaired = repair(adapter, TEMPLATE, parent)
        assert final.passed and repaired and repaired[0]["path"].startswith("reflections/")
    finally:
        del os.environ[retrieval.READER_URL_ENV]
    assert render.ranked([], {}) == ""
    assert bank.decode_embedding(bank.encode_embedding([0.5, -0.25, 1.0])) == [0.5, -0.25, 1.0]


# ---------- the stream ----------

def test_reharness_learns_revises_and_evaluates(tmp_path, embeddings, monkeypatch):
    monkeypatch.setenv("REHARNESS_EMBED_URL", embeddings)
    config = make_run(tmp_path, {"k": 2, "embedding": {"base_url": embeddings, "model": "stub-embed"}})
    summary, model = train(config)
    run = tmp_path / "runs" / "reharness-toy"
    steps = records(run)
    assert [s["step"] for s in steps] == [0, 1, 2, 3, 4, 5, 6] and steps[0]["bootstrap"]
    index = json.loads((run / "checkout" / "index.json").read_text())
    assert index == {"k": 2, "retrieval": "embedding", "embedding_model": "stub-embed", "dim": 64, "memory": "reflection"}
    # one reflection per instance, each with the embedding of its input
    files = sorted((run / "checkout" / "reflections").glob("*.json"))
    assert [f.stem for f in files] == [f"t{i:02d}" for i in range(6)]
    first = json.loads(files[0].read_text())
    assert first["embedding"]["model"] == "stub-embed" and first["embedding"]["dim"] == 64
    assert "RULE_ALPHA" in first["reflection"]
    # the forward read: from the third instance on, a same-kind reflection is retrieved and the task is solved
    assert [s["rollout"]["mean_score"] for s in steps[1:]] == [0.0, 0.0, 1.0, 1.0, 1.0, 1.0]
    material = run / "learning_material"
    second = json.loads((material / "step_0002" / "tasks" / "t01.json").read_text())
    assert [r["id"] for r in second["pipeline_extra"]["context_tool"]["retrieved"]] == ["t00"]   # the bank had one
    third = json.loads((material / "step_0003" / "tasks" / "t02.json").read_text())
    assert [r["id"] for r in third["pipeline_extra"]["context_tool"]["retrieved"]] == ["t00", "t01"]
    assert third["pipeline_extra"]["sandboxed"] is False                              # sandbox.enabled: false here
    assert "[RANKED REFLECTIONS]" in third["messages"][0]["content"]
    # the backward revision: the retrieved reflections were revised from their source trajectories, with tau_t as
    # feedback, and the change was measured
    state = json.loads((run / "workspace" / "state" / "reharness.json").read_text())
    step2 = state["steps"][1]["instances"][0]
    assert [r["id"] for r in step2["retrieved"]] == ["t00"]
    assert step2["revisions"][0]["id"] == "t00" and step2["revisions"][0]["indel_distance"] > 0
    revised = json.loads((run / "checkout" / "reflections" / "t00.json").read_text())
    assert "guidance" in revised["reflection"] and len(revised["revisions"]) >= 1
    assert revised["revisions"][0] == {"step": 2, "by": "t01", "rank": 1, **{k: revised["revisions"][0][k]
                                       for k in ("indel_distance", "chars_before", "chars_after")}}
    assert (run / "workspace" / "state" / "trajectories" / "t00.txt").read_text().startswith("[TASK]\nTask 0.")
    # the revision agent saw the source trajectory and the later one in the paper's layout
    revision_prompts = [u for s, u in model.prompts if s.startswith("You are RevisionAgent")]
    assert revision_prompts and "[TARGET MEMORY ID]\nt00\n" in revision_prompts[0]
    assert "[TARGET MEMORY'S ORIGINAL TRAJECTORY]\n[TASK]\nTask 0." in revision_prompts[0]
    assert "[LATEST OBSERVED USE]\n[TASK]\nTask 1." in revision_prompts[0]
    reflection_prompts = [u for s, u in model.prompts if s.startswith("You are ReflectionAgent")]
    assert reflection_prompts[0].startswith("[CURRENT TASK, TRAJECTORY, AND OUTCOME]")     # no demonstrations yet
    assert reflection_prompts[1].startswith("[REVISED REFLECTION DEMONSTRATIONS]\n[DEMONSTRATION 1]\n")
    assert "guidance" in reflection_prompts[1].split("[CURRENT TASK")[0]        # the revised text, as a demonstration
    # one write per instance, plus one revision per retrieved reflection (k = 2 once the bank holds two)
    assert model.calls == 1 + 2 + 3 + 3 + 3 + 3
    assert steps[3]["evolver"]["usage"][0]["model"].endswith("stub-embed") and summary["steps"] == 6
    assert (run / "step_0003" / "output" / "report.md").read_text().startswith("# ReHarness step 3")
    # the final adapter retrieves for held-out instances too (the reader needs the embedding endpoint)
    assert evaluate(config, "final", log=lambda *_: None)["mean_score"] == 1.0
    assert evaluate(config, "root", log=lambda *_: None)["mean_score"] == 0.0


def test_no_revision_and_raw_memory_ablations(tmp_path, embeddings, monkeypatch):
    monkeypatch.setenv("REHARNESS_EMBED_URL", embeddings)
    config = make_run(tmp_path, {"k": 2, "revise": False, "embedding": {"base_url": embeddings, "model": "stub-embed"}})
    _, model = train(config)
    run = tmp_path / "runs" / "reharness-toy"
    for path in (run / "checkout" / "reflections").glob("*.json"):
        data = json.loads(path.read_text())
        assert data["revisions"] == [] and "guidance" not in data["reflection"]
    assert model.calls == 6                                                        # one write per instance
    state = json.loads((run / "workspace" / "state" / "reharness.json").read_text())
    assert all(i["revisions"] == [] for s in state["steps"] for i in s["instances"])

    config = Config.load(tmp_path / "run.yaml", ["trainer.run_name=raw", "evolver.params.raw_memory=true"])
    _, model = train(config)
    raw = json.loads((tmp_path / "runs" / "raw" / "checkout" / "reflections" / "t00.json").read_text())
    assert raw["reflection"].startswith("[TASK]\nTask 0.") and "[OUTCOME]" in raw["reflection"] and model.calls == 0


def test_bm25_without_an_embedding_endpoint(tmp_path):
    config = make_run(tmp_path, {"k": 1})
    summary, _ = train(config)
    run = tmp_path / "runs" / "reharness-toy"
    assert json.loads((run / "checkout" / "index.json").read_text())["retrieval"] == "bm25"
    assert [s["rollout"]["mean_score"] for s in records(run)[1:]] == [0.0, 0.0, 1.0, 1.0, 1.0, 1.0]
    assert json.loads(next((run / "checkout" / "reflections").glob("*.json")).read_text())["embedding"] is None
    assert summary["steps"] == 6


def test_embedding_retrieval_needs_the_readers_variable(tmp_path, embeddings, monkeypatch):
    monkeypatch.delenv("REHARNESS_EMBED_URL", raising=False)
    config = make_run(tmp_path, {"embedding": {"base_url": embeddings, "model": "stub-embed"}})
    with pytest.raises(SystemExit, match="REHARNESS_EMBED_URL"):
        train(config)


@pytest.mark.skipif(not shutil.which("bwrap"), reason="needs bubblewrap")
def test_sandboxed_rollout_reads_through_the_loopback_relay(workdir, embeddings, monkeypatch):
    """The agent's worker runs in its sandbox and the adapter's reader in a stricter one: the only way to the
    embedding endpoint is the forwarded 127.0.0.1 port the environment names."""
    monkeypatch.setenv("REHARNESS_EMBED_URL", embeddings)
    config = make_run(workdir, {"k": 2, "embedding": {"base_url": embeddings, "model": "stub-embed"}}, sandbox=True)
    summary, _ = train(config)
    run = workdir / "runs" / "reharness-toy"
    assert summary["steps"] == 6
    assert [s["rollout"]["mean_score"] for s in records(run)[1:]] == [0.0, 0.0, 1.0, 1.0, 1.0, 1.0]
    record = json.loads((run / "learning_material" / "step_0003" / "tasks" / "t02.json").read_text())
    assert record["pipeline_extra"]["sandboxed"] is True          # the reader ran in the adapter's own sandbox
    assert [r["id"] for r in record["pipeline_extra"]["context_tool"]["retrieved"]] == ["t00", "t01"]
    assert (run / "sandbox" / "rollout-1.json").exists()


# ---------- the agents' replies and the bank's ids ----------

def test_the_agents_json_is_parsed_leniently_and_ids_stay_distinct():
    from hermit.algorithms.evolvers.reharness.evolver import extract_field, parse_json_field
    import sys as _sys
    _sys.path.insert(0, str(TEMPLATE))
    from reharness_core import bank
    literal_newlines = '{"reflection": "- evidence: a\n- strategy: b"}'          # a newline inside the string
    assert parse_json_field(literal_newlines, "reflection") == "- evidence: a\n- strategy: b"
    assert parse_json_field('Here:\n```json\n{"revised_reflection": "x"}\n```', "revised_reflection") == "x"
    assert parse_json_field('{"reflection": ""}', "reflection") is None and parse_json_field("prose", "reflection") is None
    # What the agents actually send back at times: a draft before the final object (the reasoning leaked into the
    # reply), a bare quote or a markdown escape inside the string, the key in another case, prose around the object.
    leaked = 'draft {"reflection":"a draft"} and more</think>{"reflection":"final text"}'
    assert extract_field(leaked, "reflection") == ("final text", "json")
    assert extract_field('{"reflection":"first"}\n{"reflection":"second"}', "reflection") == ("second", "json")
    # A stub closing the reply after the real object ("see above", "done", "placeholder" in the OfficeQA streams).
    real = "- evidence: the table's header names the unit\n- strategy: " + "convert before summing; " * 4
    for stub in ("see above", "(see above)", "done", "ok", "placeholder"):
        stubbed = json.dumps({"reflection": real}) + "\n" + json.dumps({"reflection": stub})
        assert extract_field(stubbed, "reflection") == (real, "json")
        # The same key twice in one object, the real text first (json keeps only the last value).
        repeated = json.dumps({"reflection": real})[:-1] + ", " + json.dumps({"reflection": stub})[1:]
        assert extract_field(repeated, "reflection") == (real, "json")
    repeated_empty = json.dumps({"reflection": real})[:-1] + ', "reflection": "", "reflection_type": "concise"}'
    assert extract_field(repeated_empty, "reflection") == (real, "json")
    assert extract_field('{"reflection":"say "hi" now"}', "reflection") == ('say "hi" now', "json field")
    assert extract_field('{"reflection":"fdbk\\_g1, 5\\% more"}', "reflection") == ("fdbk\\_g1, 5\\% more", "json field")
    assert extract_field('{"Reflection":"x"} Hope this helps.', "reflection") == ("x", "json")
    assert extract_field('{"reflection":"abc def', "reflection") == (None, None)      # cut off: not a reflection
    assert bank.safe_id("o0_q007") == "o0_q007" and bank.safe_id("UID0002") == "UID0002"
    long_a, long_b = "x" * 70 + "a", "x" * 70 + "b"
    assert bank.safe_id(long_a) != bank.safe_id(long_b) and len(bank.safe_id(long_a)) < 64
    assert bank.safe_id("a b") != bank.safe_id("a_b")                      # folded characters do not collide


def test_a_cut_off_reply_gets_a_larger_budget_and_a_wrong_one_a_reminder():
    import types
    from hermit.algorithms.evolvers.reharness.evolver import ReHarnessEvolver
    calls = []

    def cut_then_fine(messages, **sampling):
        calls.append((len(messages), dict(sampling)))
        if len(calls) == 1:
            return {"content": '{"reflection":"a long reflection that', "finish_reason": "length"}
        return {"content": '{"reflection":"done"}', "finish_reason": "stop"}

    ctx = types.SimpleNamespace(tools={"llm": cut_then_fine})
    value, meta = ReHarnessEvolver()._chat(ctx, "system", "user", {"max_tokens": 8192}, "reflection")
    assert value == "done" and meta["calls"] == 2 and calls[1] == (2, {"max_tokens": 16384})   # the same request
    assert meta["parse"] == "json after a larger budget" and meta["note"] == "the reply was cut off at max_tokens"

    calls.clear()

    def prose_then_fine(messages, **sampling):
        calls.append(len(messages))
        return {"content": "I would rather explain." if len(calls) == 1 else '{"reflection":"done"}',
                "finish_reason": "stop"}

    ctx = types.SimpleNamespace(tools={"llm": prose_then_fine})
    value, meta = ReHarnessEvolver()._chat(ctx, "system", "user", {"max_tokens": 8192}, "reflection")
    assert value == "done" and calls == [2, 4] and meta["parse"] == "json after a reminder" and "note" not in meta

    def prose_twice(messages, **sampling):
        return {"content": "Just prose, twice.", "finish_reason": "stop"}

    value, meta = ReHarnessEvolver()._chat(types.SimpleNamespace(tools={"llm": prose_twice}), "s", "u", {}, "reflection")
    assert value == "Just prose, twice." and meta["parse"] == "raw text (no JSON in two replies)"


def test_a_trajectory_without_feedback_and_a_codex_event_stream(tmp_path):
    """`feedback: false` keeps the judge's verdict out of tau_t; a Codex session's event stream is rendered as its
    items, not as raw JSON, and the log directory's static files are left out."""
    import json
    from hermit.algorithms.evolvers.reharness import trajectory
    log = tmp_path / "trajectories" / "q1"
    log.mkdir(parents=True)
    events = [{"type": "thread.started", "thread_id": "t"},
              {"type": "item.completed", "item": {"type": "reasoning", "text": "Look at the table first."}},
              {"type": "item.completed", "item": {"type": "command_execution", "command": "rg -n 'debt' docs",
                                                   "exit_code": 0, "aggregated_output": "docs/a.md:3: debt 12\n"}},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "<answer>12</answer>"}},
              {"type": "turn.completed", "usage": {"input_tokens": 5}}]
    (log / "agent-codex.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (log / "prompt.txt").write_text("Read task.md")
    (log / "harness.json").write_text("{}")
    (log / "task.md").write_text("Question: how much debt?")
    record = {"id": "q1", "input": {"question": "how much debt?"}, "output": {"answer": "12"},
              "judge": {"score": 0.0, "feedback": "'12' but expected '13'"}, "status": "ok", "trajectory": "trajectories/q1"}
    with_feedback = trajectory.render(record, tmp_path)
    assert "[command exit 0] rg -n 'debt' docs" in with_feedback and "output: docs/a.md:3: debt 12" in with_feedback
    assert "[assistant] <answer>12</answer>" in with_feedback and "[reasoning] Look at the table first." in with_feedback
    assert "thread.started" not in with_feedback and "Read task.md" not in with_feedback and "Question: how much debt?" in with_feedback
    assert "expected '13'" in with_feedback and "reward: 0.0" in with_feedback
    without = trajectory.render(record, tmp_path, feedback=False)
    assert "expected" not in without and "13" not in without and "reward" not in without
    assert trajectory.NO_FEEDBACK in without and "[assistant] <answer>12</answer>" in without


def test_the_codex_agent_deploys_a_context_adapter_as_agents_md(tmp_path):
    """The OfficeQA Codex agent writes AGENTS.md from the Session's context for an adapter that is context rather than
    a Codex tree (the reharness template), and leaves a tree's own AGENTS.md alone."""
    import sys as _sys
    agent_dir = Path(__file__).resolve().parents[1] / "examples" / "officeqa" / "utils" / "agent"
    _sys.path.insert(0, str(agent_dir))
    import codex_agent
    adapter = tmp_path / "adapter"
    shutil.copytree(TEMPLATE, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    work = tmp_path / "work"
    work.mkdir()
    deployed = codex_agent.deploy_harness(adapter, work, "# Skill\n\n[RANKED REFLECTIONS]\n[RANK 1 OF 1 REFLECTION]\nx\n")
    assert deployed["context"] and deployed["agents_md"]
    assert (work / "AGENTS.md").read_text().startswith("# Skill") and "[RANK 1 OF 1 REFLECTION]" in (work / "AGENTS.md").read_text()
    assert not (work / "ADAPTER.md").exists() and not (work / "reflections").exists()     # the bank stays out of the session
    work2 = tmp_path / "work2"
    work2.mkdir()
    assert not codex_agent.deploy_harness(adapter, work2)["agents_md"]                  # no context given: no harness
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "AGENTS.md").write_text("the tree's own")
    work3 = tmp_path / "work3"
    work3.mkdir()
    deployed = codex_agent.deploy_harness(tree, work3, "context that must not replace it")
    assert (work3 / "AGENTS.md").read_text() == "the tree's own" and not deployed["context"]


def test_a_resumed_run_continues_with_the_restored_trajectories(tmp_path):
    """State persistence: a run that died after step 2 resumes at step 3 with the bank and the stored source
    trajectories of the saved step, so the revisions keep reading real trajectories and no step is recorded twice."""
    config = make_run(tmp_path, {"k": 2})
    train(config)
    run = tmp_path / "runs" / "reharness-toy"
    lines = (run / "steps.jsonl").read_text().splitlines()
    (run / "steps.jsonl").write_text("\n".join(lines[:3]) + "\n")             # as if the run died after step 2
    (run / "summary.json").unlink()
    summary, model = train(Config.load(tmp_path / "run.yaml"))
    assert [s["step"] for s in records(run)] == [0, 1, 2, 3, 4, 5, 6] and summary["steps"] == 6
    state = json.loads((run / "workspace" / "state" / "reharness.json").read_text())
    assert [s["step"] for s in state["steps"]] == [1, 2, 3, 4, 5, 6]            # the rerun added steps 3 to 6 once
    revisions = [u for kind, u in model.prompts if kind.startswith("You are RevisionAgent")]
    assert revisions and all("[TARGET MEMORY'S ORIGINAL TRAJECTORY]\n[TASK]\nTask " in u for u in revisions)
    assert not any("is not available" in u for u in revisions)              # the stored trajectories came back
    assert sorted(f.stem for f in (run / "checkout" / "reflections").glob("*.json")) == [f"t{i:02d}" for i in range(6)]


def test_a_stopped_step_leaves_no_record_and_no_orphan_trajectory(tmp_path):
    """Failure handling: when the Trainer stops the run while an agent is writing, the step comes back failed, the
    Training State keeps no record of it and the trajectory stored for its memory is removed."""
    import threading
    import types
    from hermit.algorithms.evolvers.reharness.evolver import ReHarnessEvolver
    config = make_run(tmp_path, {"k": 2})
    trainer = Trainer(config, log=lambda *_: None)
    trainer.llm = ScriptedModel()
    trainer.train()
    ws = trainer.ws
    before_state = (ws.state / "reharness.json").read_text()
    before_files = sorted(p.name for p in (ws.state / "trajectories").glob("*"))
    cancel = threading.Event()

    def llm(messages, **sampling):
        cancel.set()                                   # the Trainer stopped the run during this call
        return {"content": json.dumps({"reflection": "- strategy: x"}), "finish_reason": "stop"}

    ctx = types.SimpleNamespace(workspace=ws, params={"k": 2}, tools={"llm": llm},
                                progress=types.SimpleNamespace(step=99), cancel=cancel, sandbox=None)
    result = ReHarnessEvolver().step(ctx)
    assert result.status == "failed" and "not committed" in result.summary
    assert (ws.state / "reharness.json").read_text() == before_state
    assert sorted(p.name for p in (ws.state / "trajectories").glob("*")) == before_files


def test_a_failing_agent_leaves_the_bank_unchanged(tmp_path):
    """Failure handling: an `llm` that raises is recorded per instance; nothing is written, the run goes on."""
    class Raising(ScriptedModel):
        def chat(self, messages, **kwargs):
            raise RuntimeError("provider down")

    config = make_run(tmp_path, {"k": 2})
    trainer = Trainer(config, log=lambda *_: None)
    trainer.llm = Raising()
    trainer.train()
    run = tmp_path / "runs" / "reharness-toy"
    steps = [s for s in records(run) if not s.get("bootstrap")]
    assert len(steps) == 6 and all(s["status"] == "done" for s in steps)
    assert all(s["evolver"]["summary"].startswith("no reflection written: RuntimeError: provider down") for s in steps)
    assert list((run / "checkout" / "reflections").glob("*.json")) == []
    assert list((run / "workspace" / "state" / "trajectories").glob("*")) == []
    state = json.loads((run / "workspace" / "state" / "reharness.json").read_text())
    assert state["steps"][0]["instances"][0]["write"]["error"].startswith("RuntimeError: provider down")


def test_without_a_recorded_context_tool_result_nothing_is_revised(tmp_path, embeddings, monkeypatch):
    """Eq. 2 revises what the agent read: a pipeline that records no context_tool result is taken to have read no
    reflections; `recompute_retrieval: true` ranks the bank again instead and says so."""
    monkeypatch.setenv("REHARNESS_EMBED_URL", embeddings)
    config = make_run(tmp_path, {"k": 2, "embedding": {"base_url": embeddings, "model": "stub-embed"}})
    (tmp_path / "pipeline.py").write_text(PIPELINE.replace(
        'extra = {"context_tool": session.last_context} if session is not None and session.last_context is not None else {}',
        "extra = {}"))
    _, model = train(config)
    run = tmp_path / "runs" / "reharness-toy"
    state = json.loads((run / "workspace" / "state" / "reharness.json").read_text())
    assert all(i["revisions"] == [] and i["retrieval"].startswith("none recorded") for s in state["steps"] for i in s["instances"])
    assert "recorded no context_tool result" in state["steps"][0]["note"]
    assert model.calls == 6                                                 # one write per instance, nothing else
    report = (run / "step_0002" / "output" / "report.md").read_text()
    assert "(none recorded)" in report and "recorded no context_tool result" in report
    config = Config.load(tmp_path / "run.yaml", ["trainer.run_name=recompute", "evolver.params.recompute_retrieval=true"])
    _, model = train(config)
    state = json.loads((tmp_path / "runs" / "recompute" / "workspace" / "state" / "reharness.json").read_text())
    second = state["steps"][1]["instances"][0]
    assert second["retrieval"].startswith("recomputed") and second["revisions"][0]["retrieval"] == "recomputed"
    bank_entry = json.loads((tmp_path / "runs" / "recompute" / "checkout" / "reflections" / "t00.json").read_text())
    assert bank_entry["revisions"][0]["retrieval"] == "recomputed"


def test_a_signing_relay_listens_on_the_loopback_only(tmp_path):
    """The wire meter signs keyless requests only on the loopback, refuses another address or an empty key variable,
    and its /health says whether it signs."""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "UPSTREAM": "http://127.0.0.1:9",
           "UPSTREAM_KEY_ENV": "TEST_RELAY_KEY", "TEST_RELAY_KEY": "k", "PORT": "0", "LEDGER": ""}
    bad = subprocess.run([sys.executable, "-m", "hermit.runtime.wire_meter"], env={**env, "BIND": "0.0.0.0"},
                         capture_output=True, text=True, timeout=30)
    assert bad.returncode != 0 and "loopback" in (bad.stderr + bad.stdout)
    unset = subprocess.run([sys.executable, "-m", "hermit.runtime.wire_meter"], env={**env, "TEST_RELAY_KEY": ""},
                           capture_output=True, text=True, timeout=30)
    assert unset.returncode != 0 and "empty or unset" in (unset.stderr + unset.stdout)
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "hermit.runtime.wire_meter"], env={**env, "PORT": str(port)},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                    health = json.loads(response.read())
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise AssertionError("the relay did not come up")
        assert health["signs"] is True
        line = proc.stdout.readline()
        assert line.startswith(f"wire meter on 127.0.0.1:{port}")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
