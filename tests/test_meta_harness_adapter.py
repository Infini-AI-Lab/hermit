"""Meta-Harness Adapter: the meta-harness-adapter template, the Codex agent's `deploy: adapter` mode and the Evolver's
loop over steps (record the evaluated candidate, propose k, validate, queue, hand back, select) with a scripted
proposer (no model, no Docker, no network)."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import pytest

from hermit.algorithms.evolvers.meta_harness_adapter import evolver as E
from hermit.evolver.base import Access, Progress, StepContext, StepResult, Workspace
from hermit.spec import check_spec

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "hermit" / "algorithms" / "templates" / "meta_harness_adapter"
AGENT_DIR = ROOT / "examples" / "officeqa" / "utils" / "agent"


@pytest.fixture
def tmp_path():
    """A directory outside /tmp: the template check runs under bubblewrap with an empty tmpfs at /tmp."""
    base = Path.home() / ".cache" / "hermit-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


def seed(tmp: Path) -> Path:
    adapter = tmp / "adapter"
    shutil.copytree(TEMPLATE, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    (adapter / "ADAPTER.md").write_text("# OfficeQA Skill\n\nNarrow to the right file first.\n")
    return adapter


# ---------- the template ----------

def test_template_root_and_seed_pass_the_check(tmp_path):
    assert check_spec(TEMPLATE, TEMPLATE, deploy=True).passed
    adapter = seed(tmp_path)
    assert check_spec(adapter, TEMPLATE, deploy=True).passed


def test_any_file_in_any_layout_is_learnable_but_hermit_is_not(tmp_path):
    adapter = seed(tmp_path)
    (adapter / "tools" / "deep").mkdir(parents=True)
    (adapter / "tools" / "deep" / "sum.py").write_text("print(1)\n")
    (adapter / "notes.txt").write_text("free text\n")
    assert check_spec(adapter, TEMPLATE, deploy=True).passed
    (adapter / ".hermit" / "EVOLVER.md").write_text("changed\n")
    result = check_spec(adapter, TEMPLATE, deploy=True)
    assert not result.passed and any(".hermit" in i.path for i in result.issues)


# ---------- the Codex agent's `deploy: adapter` ----------

def _codex_agent():
    if str(AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(AGENT_DIR))
    import codex_agent
    return codex_agent


def test_deploy_adapter_gives_adapter_md_as_agents_md_and_every_other_file(tmp_path):
    codex_agent = _codex_agent()
    adapter = seed(tmp_path)
    (adapter / "scripts").mkdir()
    (adapter / "scripts" / "find.py").write_text("print('x')\n")
    (adapter / "skills" / "x").mkdir(parents=True)
    (adapter / "skills" / "x" / "ADAPTER.md").write_text("a nested file of that name is the adapter's own\n")
    (adapter / "exports.yaml").write_text("{}\n")
    work = tmp_path / "work"
    work.mkdir()
    out = codex_agent.deploy_adapter(adapter, work)
    assert (work / "AGENTS.md").read_text() == (adapter / "ADAPTER.md").read_text()
    assert (work / "scripts" / "find.py").is_file() and (work / "exports.yaml").is_file()
    assert not (work / ".hermit").exists() and not (work / "ADAPTER.md").exists()
    assert out["agents_md"] and out["sub_agents"] == []
    assert out["files"] == ["exports.yaml", "scripts/find.py", "skills/x/ADAPTER.md"]


def test_deploy_adapter_refuses_a_name_the_session_uses(tmp_path):
    codex_agent = _codex_agent()
    adapter = seed(tmp_path)
    (adapter / "task.md").write_text("mine\n")
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ValueError, match="task.md"):
        codex_agent.deploy_adapter(adapter, work)


def test_deploy_adapter_refuses_codex_configuration(tmp_path):
    codex_agent = _codex_agent()
    adapter = seed(tmp_path)
    (adapter / ".codex").mkdir()
    (adapter / ".codex" / "hooks.json").write_text("{}\n")
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ValueError, match=".codex"):
        codex_agent.deploy_adapter(adapter, work)
    assert not (work / ".codex").exists()


# ---------- the Evolver ----------

def material(ws: Workspace, step: int, scores: dict[str, float]) -> None:
    """The learning material of one step, as the Trainer writes it, with `current` pointing at it."""
    d = ws.learning_material / f"step_{step:04d}"
    (d / "tasks").mkdir(parents=True)
    for tid, score in scores.items():
        (d / "tasks" / f"{tid}.json").write_text(json.dumps({"id": tid, "judge": {"score": score, "feedback": "x"}}))
    current = ws.learning_material / "current"
    if current.is_symlink():
        current.unlink()
    current.symlink_to(d.name)


def workspace(tmp: Path) -> Workspace:
    ws = Workspace(adapter=seed(tmp), learning_material=tmp / "material", task_info=tmp / "task-info",
                   history=tmp / "history", state=tmp / "state", scratch=tmp / "scratch", output=tmp / "output")
    for d in (ws.learning_material, ws.state, ws.scratch, ws.output, ws.task_info):
        d.mkdir(parents=True, exist_ok=True)
    return ws


def context(ws: Workspace, step: int, **params) -> StepContext:
    def check_spec_tool(path: str) -> dict:
        result = check_spec(Path(path), TEMPLATE, deploy=True)
        return {"passed": result.passed, "issues": [i.__dict__ for i in result.issues]}
    return StepContext(workspace=ws, access=Access(), tools={"check_spec": check_spec_tool},
                       params={"k": 2, "max_rounds": None, "prompt": "PROPOSE.md", **params},
                       progress=Progress(step, step, True))


def evolver(proposals: list[list[dict]]):
    """The Evolver with a scripted proposer: each session writes the next round's candidates and pending file."""
    ev = E.MetaHarnessAdapterEvolver()
    ev.directory = Path(E.__file__).resolve().parent
    calls = []

    def session(self, ctx):
        prompt = Path(ctx.params["prompt"]).read_text()
        calls.append(prompt)
        out = ctx.workspace.scratch / E.CANDIDATES
        for item in proposals[len(calls) - 1]:
            d = out / item["name"]
            d.mkdir(parents=True)
            for rel, text in item.get("files", {}).items():
                (d / rel).parent.mkdir(parents=True, exist_ok=True)
                (d / rel).write_text(text)
        (ctx.workspace.scratch / E.PENDING).write_text(json.dumps({"candidates": [
            {k: v for k, v in item.items() if k != "files"} for item in proposals[len(calls) - 1]]}))
        (ctx.workspace.output / "report.md").write_text("why these candidates\n")
        return StepResult("done", "proposed")
    ev.calls = calls
    return ev, session


def test_one_round_evaluates_each_candidate_in_turn_and_hands_back_the_best(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    ev, session = evolver([[
        {"name": "alpha", "hypothesis": "a lookup script", "base": "seed",
         "files": {"ADAPTER.md": "Run scripts/look.py first.\n", "scripts/look.py": "print(1)\n"}},
        {"name": "beta", "hypothesis": "shorter instructions", "base": "seed", "files": {"ADAPTER.md": "Be brief.\n"}},
    ]])
    monkeypatch.setattr(E.AgentEvolver, "step", session)

    # Step 1: the seed was evaluated; the proposer writes alpha and beta; alpha is handed back.
    material(ws, 1, {"q1": 1.0, "q2": 0.0})
    r = ev.step(context(ws, 1, max_rounds=1))
    assert r.status == "done" and r.extra["next"] == "r01_alpha" and len(ev.calls) == 1
    assert "round 1" in ev.calls[0] and "Exactly 2 new candidates" in ev.calls[0]
    assert (ws.adapter / "scripts" / "look.py").is_file()
    assert (ws.adapter / ".hermit" / "EVOLVER.md").read_text() == (TEMPLATE / ".hermit" / "EVOLVER.md").read_text()
    summary = [json.loads(l) for l in (ws.state / E.SUMMARY).read_text().splitlines()]
    assert [s["id"] for s in summary] == ["seed", "r01_alpha", "r01_beta"]
    assert summary[0]["mean_score"] == 0.5 and summary[0]["rollouts"] == [str(ws.learning_material / "step_0001")]
    assert all(Path(s["files"]).is_dir() for s in summary) and Path(summary[0]["rollouts"][0]).is_dir()

    # Step 2: alpha was evaluated; beta is handed back without a proposer call.
    material(ws, 2, {"q1": 1.0, "q2": 1.0})
    r = ev.step(context(ws, 2, max_rounds=1))
    assert r.extra["next"] == "r01_beta" and len(ev.calls) == 1
    assert not (ws.adapter / "scripts").exists() and (ws.adapter / "ADAPTER.md").read_text() == "Be brief.\n"

    # Step 3: beta was evaluated; no round is left, so the best so far (alpha) is handed back to be evaluated again.
    material(ws, 3, {"q1": 0.0, "q2": 0.0})
    r = ev.step(context(ws, 3, max_rounds=1))
    assert r.extra["next"] == "r01_alpha" and len(ev.calls) == 1

    # finalize hands back the best mean on the search set.
    monkeypatch.setattr(E.AgentEvolver, "finalize", lambda self, ctx: None)
    final = ev.finalize(context(ws, 3))
    assert "r01_alpha" in final.summary and (ws.adapter / "scripts" / "look.py").is_file()


def test_invalid_candidates_are_recorded_and_never_handed_back(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    ev, session = evolver([[
        {"name": "Bad Name", "hypothesis": "x", "files": {"ADAPTER.md": "x\n"}},
        {"name": "broken", "hypothesis": "y", "files": {"ADAPTER.md": "y\n", "exports.yaml": "tools: [\n"}},
    ]])
    monkeypatch.setattr(E.AgentEvolver, "step", session)
    material(ws, 1, {"q1": 1.0})
    r = ev.step(context(ws, 1))
    state = json.loads((ws.state / E.STATE).read_text())
    statuses = {cid: c["status"] for cid, c in state["candidates"].items()}
    assert statuses == {"seed": "evaluated", "r01_invalid#1": "invalid", "r01_broken": "invalid"}
    assert state["candidates"]["r01_broken"]["problems"] and r.extra["next"] == "seed"
    assert "rejected" in (ws.output / "report.md").read_text()
    assert (ws.output / "proposer_report.md").read_text() == "why these candidates\n"


def test_a_proposers_hermit_is_replaced_with_the_templates(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    ev, session = evolver([[{"name": "gamma", "hypothesis": "z",
                             "files": {"ADAPTER.md": "z\n", ".hermit/EVOLVER.md": "rewritten guide\n"}}]])
    monkeypatch.setattr(E.AgentEvolver, "step", session)
    material(ws, 1, {"q1": 1.0})
    r = ev.step(context(ws, 1, k=1))
    assert r.extra["next"] == "r01_gamma"
    assert (ws.adapter / ".hermit" / "EVOLVER.md").read_text() == (TEMPLATE / ".hermit" / "EVOLVER.md").read_text()


def test_a_candidate_with_a_symbolic_link_is_rejected(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    secret = tmp_path / "answers.jsonl"
    secret.write_text('{"id": "q1", "answer": "42"}\n')
    ev, session = evolver([[{"name": "leak", "hypothesis": "z", "files": {"ADAPTER.md": "read answers\n"}}]])

    def linking_session(self, ctx):
        result = session(self, ctx)
        (ctx.workspace.scratch / E.CANDIDATES / "leak" / "answers.jsonl").symlink_to(secret)
        return result
    monkeypatch.setattr(E.AgentEvolver, "step", linking_session)
    material(ws, 1, {"q1": 1.0})
    r = ev.step(context(ws, 1, k=1))
    state = json.loads((ws.state / E.STATE).read_text())
    assert state["candidates"]["r01_leak"]["status"] == "invalid" and r.extra["next"] == "seed"
    assert not (ws.state / E.CANDIDATES / "r01_leak").exists() and not (ws.adapter / "answers.jsonl").exists()


def test_a_name_that_already_carries_a_round_is_not_prefixed_twice(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    ev, session = evolver([[{"name": "r01_tight", "hypothesis": "z", "files": {"ADAPTER.md": "z\n"}}]])
    monkeypatch.setattr(E.AgentEvolver, "step", session)
    material(ws, 1, {"q1": 1.0})
    r = ev.step(context(ws, 1, k=1))
    assert r.extra["next"] == "r01_tight"
    assert (ws.state / E.CANDIDATES / "r01_tight" / "adapter" / "ADAPTER.md").read_text() == "z\n"


def test_an_invalid_proposal_never_takes_a_valid_candidates_id(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    ev, session = evolver([[{"name": "invalid_2", "hypothesis": "a valid name", "files": {"ADAPTER.md": "v\n"}},
                            {"name": "Bad Name", "hypothesis": "x", "files": {"ADAPTER.md": "x\n"}}]])
    monkeypatch.setattr(E.AgentEvolver, "step", session)
    material(ws, 1, {"q1": 1.0})
    r = ev.step(context(ws, 1))
    state = json.loads((ws.state / E.STATE).read_text())
    assert state["order"] == ["seed", "r01_invalid_2", "r01_invalid#2"]
    assert state["candidates"]["r01_invalid_2"]["status"] == "queued" and r.extra["next"] == "r01_invalid_2"
    assert state["candidates"]["r01_invalid#2"]["status"] == "invalid"


def test_a_candidate_changed_in_place_is_never_handed_back(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    ev, session = evolver([
        [{"name": "alpha", "hypothesis": "a", "files": {"ADAPTER.md": "alpha\n"}}],
        [{"name": "beta", "hypothesis": "b", "files": {"ADAPTER.md": "beta\n"}}],
    ])

    def editing_session(self, ctx):
        if len(ev.calls) == 1:              # the second round edits the scored alpha in place
            (ctx.workspace.state / E.CANDIDATES / "r01_alpha" / "adapter" / "ADAPTER.md").write_text("EDITED\n")
        return session(self, ctx)
    monkeypatch.setattr(E.AgentEvolver, "step", editing_session)
    material(ws, 1, {"q1": 0.0})
    assert ev.step(context(ws, 1, k=1, max_rounds=2)).extra["next"] == "r01_alpha"
    material(ws, 2, {"q1": 1.0})                                    # alpha scores 1.0, then is edited
    assert ev.step(context(ws, 2, k=1, max_rounds=2)).extra["next"] == "r02_beta"
    material(ws, 3, {"q1": 0.5})
    r = ev.step(context(ws, 3, k=1, max_rounds=2))
    state = json.loads((ws.state / E.STATE).read_text())
    assert state["candidates"]["r01_alpha"]["status"] == "tampered" and r.extra["next"] == "r02_beta"
    assert "r01_alpha skipped" in (ws.output / "report.md").read_text()
    monkeypatch.setattr(E.AgentEvolver, "finalize", lambda self, ctx: None)
    final = ev.finalize(context(ws, 3))
    assert "r02_beta" in final.summary and (ws.adapter / "ADAPTER.md").read_text() == "beta\n"


def test_a_candidate_directory_that_is_a_link_is_rejected_and_its_target_untouched(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    outside = tmp_path / "outside"
    (outside / ".hermit").mkdir(parents=True)
    (outside / ".hermit" / "keep.txt").write_text("not the proposer's\n")
    (outside / "ADAPTER.md").write_text("outside\n")
    ev, session = evolver([[]])

    def linking_session(self, ctx):
        result = session(self, ctx)
        (ctx.workspace.scratch / E.CANDIDATES / "link").symlink_to(outside, target_is_directory=True)
        (ctx.workspace.scratch / E.PENDING).write_text(json.dumps({"candidates": [{"name": "link", "hypothesis": "z"}]}))
        return result
    monkeypatch.setattr(E.AgentEvolver, "step", linking_session)
    material(ws, 1, {"q1": 1.0})
    r = ev.step(context(ws, 1, k=1))
    state = json.loads((ws.state / E.STATE).read_text())
    assert state["candidates"]["r01_link"]["status"] == "invalid" and r.extra["next"] == "seed"
    assert (outside / ".hermit" / "keep.txt").read_text() == "not the proposer's\n"


def test_the_proposer_reads_a_summary_with_this_steps_scores(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    ev, session = evolver([[{"name": "alpha", "hypothesis": "a", "files": {"ADAPTER.md": "alpha\n"}}]])
    seen = []

    def reading_session(self, ctx):
        seen.append([json.loads(l) for l in (ctx.workspace.state / E.SUMMARY).read_text().splitlines()])
        return session(self, ctx)
    monkeypatch.setattr(E.AgentEvolver, "step", reading_session)
    material(ws, 1, {"q1": 1.0, "q2": 0.0})
    ev.step(context(ws, 1, k=1))
    assert seen == [[{**seen[0][0], "id": "seed", "mean_score": 0.5, "status": "evaluated"}]]


def test_a_store_replaced_by_a_link_is_never_written_through(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the proposer's\n")
    ev, session = evolver([[{"name": "alpha", "hypothesis": "a", "files": {"ADAPTER.md": "alpha\n"}}]])

    def relinking_session(self, ctx):
        store = ctx.workspace.state / E.CANDIDATES
        shutil.rmtree(store)
        store.symlink_to(outside, target_is_directory=True)
        return session(self, ctx)
    monkeypatch.setattr(E.AgentEvolver, "step", relinking_session)
    material(ws, 1, {"q1": 1.0})
    r = ev.step(context(ws, 1, k=1))
    assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]
    assert (outside / "keep.txt").read_text() == "not the proposer's\n"
    store = ws.state / E.CANDIDATES
    assert not store.is_symlink() and (store / "r01_alpha" / "adapter" / "ADAPTER.md").read_text() == "alpha\n"
    assert r.extra["next"] == "r01_alpha"
    state = json.loads((ws.state / E.STATE).read_text())
    assert state["candidates"]["seed"]["status"] == "evaluated"
    assert not ev._intact(context(ws, 1), state, "seed")        # its files went with the replaced store


def test_a_changed_executable_bit_makes_a_candidate_tampered(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    ev, session = evolver([
        [{"name": "alpha", "hypothesis": "a", "files": {"ADAPTER.md": "run look.py\n", "look.py": "print(1)\n"}}],
        [{"name": "beta", "hypothesis": "b", "files": {"ADAPTER.md": "beta\n"}}],
    ])

    def chmod_session(self, ctx):
        script = ctx.workspace.state / E.CANDIDATES / "r01_alpha" / "adapter" / "look.py"
        if len(ev.calls) == 0:
            result = session(self, ctx)
            (ctx.workspace.scratch / E.CANDIDATES / "alpha" / "look.py").chmod(0o755)
            return result
        script.chmod(0o644)                     # the second round makes the scored script non-executable
        return session(self, ctx)
    monkeypatch.setattr(E.AgentEvolver, "step", chmod_session)
    material(ws, 1, {"q1": 0.0})
    assert ev.step(context(ws, 1, k=1, max_rounds=2)).extra["next"] == "r01_alpha"
    material(ws, 2, {"q1": 1.0})
    assert ev.step(context(ws, 2, k=1, max_rounds=2)).extra["next"] == "r02_beta"
    material(ws, 3, {"q1": 0.5})
    ev.step(context(ws, 3, k=1, max_rounds=2))
    state = json.loads((ws.state / E.STATE).read_text())
    assert state["candidates"]["r01_alpha"]["status"] == "tampered"


def test_files_the_proposer_replaced_with_links_are_never_written_through(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    for name in ("state.json", "summary.jsonl", "prompt.md"):
        (outside / name).write_text("not the proposer's\n")
    ev, session = evolver([[{"name": "alpha", "hypothesis": "a", "files": {"ADAPTER.md": "alpha\n"}}]])

    def linking_session(self, ctx):
        for path, target in ((ctx.workspace.state / E.STATE, "state.json"), (ctx.workspace.state / E.SUMMARY,
                             "summary.jsonl"), (ctx.workspace.output / "proposer_prompt.md", "prompt.md")):
            path.unlink(missing_ok=True)
            path.symlink_to(outside / target)
        return session(self, ctx)
    monkeypatch.setattr(E.AgentEvolver, "step", linking_session)
    material(ws, 1, {"q1": 1.0})
    assert ev.step(context(ws, 1, k=1)).extra["next"] == "r01_alpha"
    assert all((outside / n).read_text() == "not the proposer's\n" for n in ("state.json", "summary.jsonl", "prompt.md"))
    assert not (ws.state / E.STATE).is_symlink() and json.loads((ws.state / E.STATE).read_text())["handed_back"] == "r01_alpha"


def test_a_working_hermit_replaced_by_a_link_is_never_copied_into_a_candidate(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("42\n")
    ev, session = evolver([[{"name": "alpha", "hypothesis": "a", "files": {"ADAPTER.md": "alpha\n"}}]])

    def linking_session(self, ctx):
        hermit = ctx.workspace.adapter / ".hermit"
        shutil.rmtree(hermit)
        hermit.symlink_to(outside, target_is_directory=True)
        return session(self, ctx)
    monkeypatch.setattr(E.AgentEvolver, "step", linking_session)
    material(ws, 1, {"q1": 1.0})
    ev.step(context(ws, 1, k=1))
    state = json.loads((ws.state / E.STATE).read_text())
    assert state["candidates"]["r01_alpha"]["status"] == "invalid"
    assert not list((ws.state / E.CANDIDATES).rglob("secret.txt")) and not list(ws.scratch.rglob("secret.txt"))
