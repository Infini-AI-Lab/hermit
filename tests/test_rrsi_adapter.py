"""RRSI Adapter: the rrsi-adapter template and the Evolver's rounds over steps (baseline and delta, analysis,
drafting with upstream's done() contract, the critic with repair, tagging, check and smoke, one candidate evaluated
per step, upstream's selection, H_T) with a scripted proposer and scripted roles (no model, no Docker, no network)."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

pytest.importorskip("rrsi")

from hermit.agent.base.interface import BaseAgent, Result  # noqa: E402
from hermit.agent.evolver.base import Access, Progress, StepContext, Workspace  # noqa: E402
from hermit.agent.evolver.methods.rrsi_adapter import evolver as E  # noqa: E402
from hermit.agent.evolver.methods.rrsi_adapter import roles as R  # noqa: E402
from hermit.spec import check_spec  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "hermit" / "adapter" / "templates" / "rrsi_adapter"

DOMAIN_PY = '''
from pathlib import Path
from hermit.agent.evolver.methods.rrsi_adapter.domain import AdapterDomain

class ToyDomain(AdapterDomain):
    name = "toy"
    texts = Path(__file__).resolve().parent / "texts"
    critic_patterns = [(r"SECRET-ANSWER", "an answer in the diff")]
'''


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
    (adapter / "ADAPTER.md").write_text("# Skill\n\nRead the task first.\n")
    return adapter


def domain_files(tmp: Path) -> str:
    texts = tmp / "dom" / "texts"
    texts.mkdir(parents=True)
    (texts / "SKILL.md").write_text("# Constitution\nNo task-specific content.\n")
    (texts / "PATTERNS.md").write_text("# Patterns\n")
    (texts / "briefs.json").write_text(json.dumps({k: f"{k} brief" for k in
                                                   ("analyst", "digester", "proposer", "critic")}))
    (tmp / "dom" / "domain.py").write_text(DOMAIN_PY)
    return str(tmp / "dom" / "domain.py") + ":ToyDomain"


# ---------- the template ----------

def test_template_root_and_seed_pass_the_check(tmp_path):
    assert check_spec(TEMPLATE, TEMPLATE, deploy=True).passed
    assert check_spec(seed(tmp_path), TEMPLATE, deploy=True).passed


def test_any_layout_is_learnable_but_hermit_is_not(tmp_path):
    adapter = seed(tmp_path)
    (adapter / "scripts").mkdir()
    (adapter / "scripts" / "look.py").write_text("print(1)\n")
    (adapter / "memory").mkdir()
    (adapter / "memory" / "NOTES.md").write_text("## lessons\n")
    assert check_spec(adapter, TEMPLATE, deploy=True).passed
    (adapter / ".hermit" / "EVOLVER.md").write_text("changed\n")
    result = check_spec(adapter, TEMPLATE, deploy=True)
    assert not result.passed and any(".hermit" in i.path for i in result.issues)


# ---------- component signals ----------

def test_adapter_diffs_are_tagged_by_upstreams_normalize():
    from rrsi.components import normalize
    from hermit.agent.evolver.methods.rrsi_adapter.domain import ADAPTER_SIGNALS
    script = "diff --git a/scripts/sum.py b/scripts/sum.py\n--- /dev/null\n+++ b/scripts/sum.py\n+print(1)\n"
    notes = "--- a/memory/NOTES.md\n+++ b/memory/NOTES.md\n+## a lesson\n"
    prose = "--- a/ADAPTER.md\n+++ b/ADAPTER.md\n+Check units before answering.\n"
    hooks = '--- a/hooks.json\n+++ b/hooks.json\n+  "Stop": [{"command": "python scripts/check.py"}]\n'
    assert normalize("client_tool", script, ADAPTER_SIGNALS) == "client_tool"
    assert normalize("skill", script, ADAPTER_SIGNALS) == "client_tool"        # no evidence: re-tagged from the diff
    assert normalize("memory", notes, ADAPTER_SIGNALS) == "memory"
    assert normalize("client_tool", prose, ADAPTER_SIGNALS) == "prompt"
    assert normalize("control_flow", hooks, ADAPTER_SIGNALS) == "control_flow"
    assert normalize("prompt", prose + script, ADAPTER_SIGNALS) == "prompt"       # one edit of two in a candidate
    assert normalize(None, prose + script, ADAPTER_SIGNALS) == "client_tool"


# ---------- the Evolver ----------

def workspace(tmp: Path) -> Workspace:
    ws = Workspace(adapter=seed(tmp), learning_material=tmp / "material", task_info=tmp / "task-info",
                   history=tmp / "history", state=tmp / "state", scratch=tmp / "scratch", output=tmp / "output")
    for d in (ws.learning_material, ws.state, ws.scratch, ws.output, ws.task_info):
        d.mkdir(parents=True, exist_ok=True)
    return ws


def material(ws: Workspace, step: int, scores: dict, tokens: int = 1000, status: dict | None = None) -> None:
    """One step's learning material: rows `<task>#<k>` with the judge's score, tokens and a Codex trajectory."""
    d = ws.learning_material / f"step_{step:04d}"
    (d / "tasks").mkdir(parents=True)
    for row, score in scores.items():
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", row)
        traj = d / "trajectories" / slug
        traj.mkdir(parents=True)
        (traj / "agent-codex.jsonl").write_text(
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": f"working on {row}"}})
            + "\n" + json.dumps({"type": "turn.completed", "usage": {"input_tokens": tokens, "output_tokens": 1}})
            + "\n")
        (d / "tasks" / f"{slug}.json").write_text(json.dumps({
            "id": row, "input": {"question": f"question {row}"}, "output": {"answer": "x"},
            "judge": {"score": score, "feedback": "predicted 'x' but expected '41.5'"},
            "status": (status or {}).get(row, "ok"), "tokens": tokens, "trajectory": f"trajectories/{slug}"}))
    current = ws.learning_material / "current"
    if current.is_symlink():
        current.unlink()
    current.symlink_to(d.name)


def rows(scores: dict) -> dict:
    """{task: [trial scores]} as rows `<task>#<k>`."""
    return {f"{t}#{i + 1}": s for t, ss in scores.items() for i, s in enumerate(ss)}


def instructions(settings: dict, prompt: str) -> str:
    """The developer instructions a role's session was given (hermit.agent.base.cli.codex_instructions)."""
    args = list(settings.get("args") or [])
    for i, arg in enumerate(args[:-1]):
        if arg == "-c" and args[i + 1].startswith("developer_instructions="):
            text = json.loads(args[i + 1].split("=", 1)[1])
            return prompt.split("# END", 1)[0] if text.startswith("Follow the SYSTEM INSTRUCTIONS") else text
    return ""


def role_reply(system: str, prompt: str) -> str:
    """The analyst reports one failure mode, the critic accepts."""
    if system.startswith("You are the batch analyst"):
        return json.dumps({"action": "report", "failure_modes": [
            {"mode": "wrong_form", "n_tasks": 2, "affected_tasks": ["q1"]}], "capability_gaps": [],
            "success_habits": [{"habit": "reads_task", "n_tasks": 1}]})
    if system.startswith("You are a strict reviewer"):
        return json.dumps({"verdict": "accept", "reasons": [], "risk_notes": []})
    raise AssertionError(f"unexpected role: {system[:80]}")


class Role(BaseAgent):
    """The `codex` Base Agent as a role's session sees it: one scripted reply per call."""

    def __init__(self, settings, reply=role_reply, extra=None, usage=()):
        super().__init__(settings)
        self.reply, self.extra, self.usage = reply, extra, list(usage)

    def run(self, prompt, *, workspace=None, tools=(), limits=None, session=None, roles=()):
        ROLE_CALLS.append({"settings": self.settings, "workspace": workspace, "limits": limits, "prompt": prompt})
        text = self.reply(instructions(self.settings, prompt), prompt)
        return Result(reply=text, exit_code=0, usage=list(self.usage),
                      extra=dict(self.extra if self.extra is not None else SERVED))


ROLE_CALLS: list = []


def context(ws: Workspace, step: int, dom: str, tmp: Path, run_agent=None, sandbox=None, **params) -> StepContext:
    def check_spec_tool(path: str) -> dict:
        result = check_spec(Path(path), TEMPLATE, deploy=True)
        return {"passed": result.passed, "issues": [i.__dict__ for i in result.issues]}
    tools = {"check_spec": check_spec_tool}
    if run_agent is not None:
        tools["run_agent"] = run_agent
    cfg = {"T": 2, "k": 2, "m": 2, "b_min": 1, "b_max": 3, "w": 3, "m_draft": 1, "delta": 0.05, "beta0": 0.1,
           "beta1": 44.5, "w_s": 0.0, "w_c": 15.0, "w_n": 0.5, "n_prune": 4, "repair_rounds": 2,
           "invalid_missing_frac": 0.2, "n_fail_traces": 3, "n_success_traces": 1}
    cfg.update(params.pop("rrsi", {}))
    return StepContext(workspace=ws, access=Access(), tools=tools,
                       params={"prompt": "PROPOSE.md", "domain": dom, "rrsi": cfg, "smoke": False,
                               "backend": "codex", "web_search": False, "home": [], "tools": ["check_spec"],
                               **params},
                       progress=Progress(step, step, True), base=tmp, sandbox=sandbox)


SERVED = {"served": True, "unavailable": False, "started": True, "cancelled": False, "output_tail": ""}


class Proposer(BaseAgent):
    """The `codex` Base Agent as the proposer's sessions see it: `run` is one scripted session."""

    def __init__(self, script, settings):
        super().__init__(settings)
        self.script = script

    def run(self, prompt, *, workspace=None, tools=(), limits=None, session=None, roles=()):
        self.script.sessions.append({"settings": self.settings, "workspace": workspace, "limits": limits,
                                     "tools": [t.name if hasattr(t, "name") else t for t in tools]})
        return self.script.run(prompt, session, Path(self.settings["log"]))


class Script:
    """A scripted proposer: `plans[(t, variant)]` is a list of session actions, each writing files into the
    candidate and a done.json (or not). `make` stands in for the Base Agent factory the Evolver calls."""

    def __init__(self, plans: dict):
        self.plans = plans
        self.sessions, self.calls, self.paths, self.prompts = [], [], {}, []

    def make(self, name, settings=None, model=None):
        assert name == "codex"
        return Proposer(self, settings)

    def run(self, prompt: str, session, log: Path) -> Result:
        self.prompts.append(prompt)
        if session:
            key, cand, done = self.paths[session]
        else:
            m = re.search(r"variant ([A-H]) of round (\d+)", prompt)
            key = (int(m.group(2)), m.group(1))
            cand = Path(re.search(r"Its adapter/ \((\S+)\) is your candidate", prompt).group(1))
            done = Path(re.search(r"write (\S+done\.json) with exactly", prompt).group(1))
        self.calls.append((key, bool(session)))
        actions = self.plans[key]
        action = actions.pop(0) if actions else {}
        for rel, text in action.get("files", {}).items():
            (cand / rel).parent.mkdir(parents=True, exist_ok=True)
            (cand / rel).write_text(text)
        for rel, data in action.get("bytes", {}).items():
            (cand / rel).parent.mkdir(parents=True, exist_ok=True)
            (cand / rel).write_bytes(data)
        if action.get("link"):
            rel, target = action["link"]
            (cand / rel).symlink_to(target)
        for rel, target in (action.get("work_links") or {}).items():      # links in or beside its own directory
            path = cand.parent / rel
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)
        if "done" in action:
            done.write_text(json.dumps(action["done"]))
        sid = session or f"s-{key[0]}{key[1]}"
        self.paths[sid] = (key, cand, done)
        log.write_text(json.dumps({"type": "thread.started", "thread_id": sid}) + "\n")
        return Result(reply="", session=sid, exit_code=int(action.get("exit", 0)), log=log, extra=dict(SERVED))


def edit(eid="C1", component="prompt", **kw):
    return {"id": eid, "component": component, "hypothesis": "h", "targets_mode": "wrong_form",
            "predicted_affected": ["q1"], "retroactive_check": "r", **kw}


def evolver(monkeypatch, script: Script):
    ev = E.RRSIAdapterEvolver()
    ev.directory = Path(E.__file__).resolve().parent
    monkeypatch.setattr(E, "make", script.make)
    monkeypatch.setattr(R, "make", lambda name, settings=None, model=None: Role(settings))
    ROLE_CALLS.clear()
    return ev


def state(ws: Workspace) -> dict:
    return json.loads((ws.state / E.STATE).read_text())


def test_a_run_baseline_rounds_selection_and_h_t(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({
        (0, "A"): [{"files": {"ADAPTER.md": "# Skill\n\nAnswer with the bare number.\n"},
                    "done": {"summary": "form", "edits": [edit()]}}],
        (0, "B"): [{"files": {"scripts/units.py": "print('units')\n"},
                    "done": {"summary": "units", "edits": [edit(component="client_tool")]}}],
        (1, "A"): [{"files": {"memory/NOTES.md": "## lessons\n"},
                    "done": {"summary": "notes", "edits": [edit(component="memory")]}}],
        (1, "B"): [{"files": {"ADAPTER.md": "# Skill\n\nShorter.\n"},
                    "done": {"summary": "short", "edits": [edit()]}}],
    })
    ev = evolver(monkeypatch, script)
    base = {"q1": [0, 0], "q2": [1, 0], "q3": [1, 1], "q4": [0, 1]}

    # Step 1: H_0 evaluated (S 0.5); round 0 drafted: A and B stored; A handed back.
    material(ws, 1, rows(base))
    r = ev.step(context(ws, 1, dom, tmp_path))
    assert r.status == "done" and r.extra["next"] == "r00A", r.summary
    fr = json.loads((ws.state / "rrsi" / "frontier.json").read_text())
    assert fr["incumbent"]["cid"] == "h0" and fr["S_star"] == 0.5
    # the record names the models this run used, not RRSIConfig's defaults, which are not read here
    assert {k: fr["config"][k] for k in ("proposer_model", "analyst_model", "critic_model")} == \
        ev._models(context(ws, 1, dom, tmp_path)) and "claude-opus" not in json.dumps(fr["config"])
    assert "Answer with the bare number" in (ws.adapter / "ADAPTER.md").read_text()
    st = state(ws)
    assert st["round"]["queue"] == ["r00B"] and st["candidates"]["r00B"]["status"] == "queued"
    directives = json.loads((ws.state / "rrsi" / "r0" / "directives.json").read_text())
    assert directives["b_t"] == 3 and directives["sigma_t"] == 0
    assert (ws.state / "rrsi" / "r0" / "analysis_report.json").is_file()
    rendered = ws.state / "rrsi" / "r0" / "analysis" / "rendered"
    assert (rendered / "q1.txt").is_file() and "GRADING" in (rendered / "q1.txt").read_text()
    assert "EDIT BUDGET" in (ws.scratch / "rrsi" / "r0A" / "context" / "round.md").read_text()

    # Step 2: A measured (S 0.75, +0.25 > delta, same tokens): B handed back.
    material(ws, 2, rows({"q1": [1, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 1]}))
    r = ev.step(context(ws, 2, dom, tmp_path))
    assert r.extra["next"] == "r00B" and (ws.adapter / "scripts" / "units.py").is_file()

    # Step 3: B measured (S 0.5); round 0 decided: A wins; round 1 drafted from A.
    material(ws, 3, rows(base))
    r = ev.step(context(ws, 3, dom, tmp_path))
    fr = json.loads((ws.state / "rrsi" / "frontier.json").read_text())
    assert fr["incumbent"]["cid"] == "r00A" and fr["S_star"] == 0.75 and r.extra["next"] == "r01A"
    history = [json.loads(l) for l in (ws.state / "rrsi" / "history.jsonl").read_text().splitlines()]
    outcomes = {(h["t"], h["variant"]): h["outcome"] for h in history if h.get("edit_id")}
    # B is admissible too (inside the band, a new structural component: w_n * nu = 0.5 > 0) but A scored higher
    assert outcomes == {(0, "A"): "ACCEPTED", (0, "B"): "LOST"}
    assert (ws.adapter / "memory" / "NOTES.md").is_file()
    assert "bare number" in (ws.adapter / "ADAPTER.md").read_text()          # built on the incumbent A

    # Steps 4-5: round 1's candidates measured below the floor; H_2 = H_1; the run stops with H_T handed back.
    material(ws, 4, rows({"q1": [0, 0], "q2": [0, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 4, dom, tmp_path))
    assert r.extra["next"] == "r01B"
    material(ws, 5, rows({"q1": [0, 0], "q2": [0, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 5, dom, tmp_path))
    assert r.status == "stop" and r.extra["next"] == "r00A"
    decisions = json.loads((ws.state / "rrsi" / "r1" / "decisions.json").read_text())
    assert all("floor" in d["reason"] for d in decisions)
    final = ev.finalize(context(ws, 5, dom, tmp_path))
    assert "r00A" in final.summary and "bare number" in (ws.adapter / "ADAPTER.md").read_text()
    assert final.extra["complete"] is True and final.extra["rounds"] == 2 and "ended early" not in final.summary
    attribution = [json.loads(l) for l in (ws.state / "rrsi" / "attribution.jsonl").read_text().splitlines()]
    assert attribution[0]["predicted_hit"] == ["q1"]


def test_a_retried_close_decides_with_the_counts_of_the_rounds_before(tmp_path, monkeypatch):
    """A close that failed after writing some of its round's rows (an attribution that raised) decides again with
    the incumbent's components as they were before the round, so it picks the same winner and records it once."""
    import rrsi.selection
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({
        (0, "A"): [{"files": {"ADAPTER.md": "# Skill\n\nAnswer with the bare number.\n"},
                    "done": {"summary": "form", "edits": [edit()]}}],
        (0, "B"): [{"files": {"scripts/units.py": "print('units')\n"},
                    "done": {"summary": "units", "edits": [edit(component="client_tool")]}}],
    })
    ev = evolver(monkeypatch, script)
    seen, real = [], rrsi.selection.select_round

    def select(cands, inc, s_star, delta, cfg, counts, **kw):
        seen.append(dict(counts))
        return real(cands, inc, s_star, delta, cfg, counts, **kw)
    monkeypatch.setattr(rrsi.selection, "select_round", select)
    base = {"q1": [0, 0], "q2": [1, 0], "q3": [1, 1], "q4": [0, 1]}
    material(ws, 1, rows(base))
    ev.step(context(ws, 1, dom, tmp_path))
    material(ws, 2, rows({"q1": [1, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 1]}))
    ev.step(context(ws, 2, dom, tmp_path))
    attribute, calls = ev._attribute, []

    def failing(*a, **k):
        calls.append(1)
        if len(calls) == 2:                     # B's, after A's row (the winner, ACCEPTED) is in the history
            raise TypeError("an attribution that raised")
        return attribute(*a, **k)
    monkeypatch.setattr(ev, "_attribute", failing)
    material(ws, 3, rows(base))
    assert ev.step(context(ws, 3, dom, tmp_path)).status == "failed"
    written = [json.loads(l) for l in (ws.state / "rrsi" / "history.jsonl").read_text().splitlines()]
    assert [(h["variant"], h["accepted"]) for h in written if h.get("t") == 0 and h.get("edit_id")] == [("A", True)]
    ev.step(context(ws, 4, dom, tmp_path))
    assert len(seen) == 2 and seen[0] == seen[1]
    history = [json.loads(l) for l in (ws.state / "rrsi" / "history.jsonl").read_text().splitlines()]
    assert sum(1 for h in history if h.get("t") == 0 and h.get("accepted") and h.get("edit_id")) == 1
    attribution = [json.loads(l) for l in (ws.state / "rrsi" / "attribution.jsonl").read_text().splitlines()]
    assert sorted(r["variant"] for r in attribution if r["t"] == 0) == ["A", "B"]      # the failed one written too


def test_a_round_decided_before_the_state_was_saved_is_not_decided_again(tmp_path, monkeypatch):
    """A step that ended after the round's frontier was written (H_{t+1} the winner) but before the state was saved
    leaves the round open in the state; the next step finishes it as decided, never weighing the candidates against
    the winner itself."""
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({
        (0, "A"): [{"files": {"ADAPTER.md": "# Skill\n\nAnswer with the bare number.\n"},
                    "done": {"summary": "form", "edits": [edit()]}}],
        (0, "B"): [{"files": {"scripts/units.py": "print('units')\n"},
                    "done": {"summary": "units", "edits": [edit(component="client_tool")]}}],
    })
    ev = evolver(monkeypatch, script)
    base = {"q1": [0, 0], "q2": [1, 0], "q3": [1, 1], "q4": [0, 1]}
    material(ws, 1, rows(base))
    ev.step(context(ws, 1, dom, tmp_path))
    material(ws, 2, rows({"q1": [1, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 1]}))
    ev.step(context(ws, 2, dom, tmp_path))

    class Killed(BaseException):
        pass
    save = ev._save

    def killed(ctx, st):
        if st.get("round") is None and st.get("t") == 1:     # the close's own save, after frontier.json
            raise Killed()
        save(ctx, st)
    monkeypatch.setattr(ev, "_save", killed)
    material(ws, 3, rows(base))
    with pytest.raises(Killed):
        ev.step(context(ws, 3, dom, tmp_path))
    R = ws.state / "rrsi"
    decided, frontier = (R / "r0" / "decisions.json").read_text(), json.loads((R / "frontier.json").read_text())
    assert frontier["incumbent"]["cid"] == "r00A" and json.loads((ws.state / "rrsi_adapter.json").read_text())["t"] == 0
    monkeypatch.setattr(ev, "_save", save)
    ev.step(context(ws, 4, dom, tmp_path))
    assert (R / "r0" / "decisions.json").read_text() == decided
    after = json.loads((R / "frontier.json").read_text())
    assert after["incumbent"] == frontier["incumbent"] and [x["t"] for x in after["trajectory"]][:2] == [0, 1]
    history = [json.loads(l) for l in (R / "history.jsonl").read_text().splitlines()]
    assert [(h["variant"], h["accepted"]) for h in history if h.get("t") == 0 and h.get("edit_id")] == \
        [("A", True), ("B", False)]


def test_delta_is_calibrated_from_the_baseline_when_the_config_has_none(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    ev.step(context(ws, 1, dom, tmp_path, rrsi={"delta": None}))
    cal = json.loads((ws.state / "rrsi" / "calibration.json").read_text())
    assert cal["method"].startswith("bootstrap") and cal["delta"] > 0 and cal["jobs"] == ["h0"]


def test_an_invalid_done_goes_back_to_the_same_session(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({
        (0, "A"): [{"files": {"ADAPTER.md": "x\n"},
                    "done": {"summary": "s", "edits": [edit("C1"), edit("C2"), edit("C3"), edit("C4")]}},
                   {"done": {"summary": "s", "edits": [edit("C1")]}}],
        (0, "B"): [{"done": {"summary": "s", "edits": [edit()]}}, {}, {}, {}],     # never changes a file
    })
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 1, dom, tmp_path))
    assert r.extra["next"] == "r00A"
    assert script.calls[:2] == [((0, "A"), False), ((0, "A"), True)]
    assert "exceed the budget b_t = 3" in script.prompts[1]
    st = state(ws)
    assert st["round"]["variants"]["B"]["gate_failure"] == "no_proposal" and "r00B" not in st["candidates"]


def test_the_critic_rejects_a_leak_and_the_repair_is_screened_again(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({
        (0, "A"): [{"files": {"ADAPTER.md": "The answer to q1 is SECRET-ANSWER.\n"},
                    "done": {"summary": "s", "edits": [edit()]}},
                   {"files": {"ADAPTER.md": "Check the units.\n"}, "done": {"summary": "s", "edits": [edit()]}}],
        (0, "B"): [{"files": {"ADAPTER.md": "SECRET-ANSWER\n"}, "done": {"summary": "s", "edits": [edit()]}},
                   {"done": {"summary": "s", "edits": [edit()]}}, {"done": {"summary": "s", "edits": [edit()]}}],
    })
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 1, dom, tmp_path))
    st = state(ws)
    assert r.extra["next"] == "r00A" and (ws.adapter / "ADAPTER.md").read_text() == "Check the units.\n"
    assert st["round"]["variants"]["B"]["gate_failure"] == "critic_reject"
    critic = json.loads((ws.state / "rrsi" / "r0" / "A" / "critic_a0.json").read_text())
    assert critic["verdict"] == "reject" and "precheck" in critic["reasons"][0]
    assert any("REPAIR ROUND" in p for p in script.prompts)


def test_a_reserved_slot_and_the_edits_fields_are_checked_in_done(tmp_path, monkeypatch):
    """With the incumbent stalled, the last variant must declare an edit on a never-exercised component (done()'s
    contract; the declared component is checked against the diff in the draft); an edit's fields have their types."""
    ev = evolver(monkeypatch, Script({}))
    from rrsi.history import exploration
    explore = exploration(3, 1, {"prompt"}, 1)
    assert explore["sigma"] == 1 and "skill" in explore["untried"]
    prop, problems, final = ev._check_done(_done(tmp_path, [edit(component="prompt")]), 2, True, explore, True)
    assert prop is None and "RESERVED EXPLORATION SLOT" in problems[0] and not final
    prop, problems, final = ev._check_done(_done(tmp_path, [edit(component="skill")]), 2, True, explore, True)
    assert prop is not None and not problems
    prop, problems, final = ev._check_done(_done(tmp_path, [edit(predicted_affected=42)]), 2, False, explore, True)
    assert prop is None and any("predicted_affected must be an array" in p for p in problems)
    prop, problems, final = ev._check_done(_done(tmp_path, []), 2, False, explore, False)
    assert prop is None and final                     # nothing changed and nothing declared: upstream's no_proposal


def _done(tmp: Path, edits: list) -> Path:
    path = tmp / "done.json"
    path.write_text(json.dumps({"summary": "s", "edits": edits}))
    return path


def test_rollouts_of_another_adapter_are_never_credited(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    ev.step(context(ws, 1, dom, tmp_path))
    (ws.adapter / "ADAPTER.md").write_text("something else ran\n")
    material(ws, 2, rows({"q1": [1, 1], "q2": [1, 1], "q3": [1, 1], "q4": [1, 1]}))
    r = ev.step(context(ws, 2, dom, tmp_path))
    st = state(ws)
    assert st["candidates"]["r00A"]["status"] == "queued" and r.extra["next"] == "r00A"
    assert (ws.adapter / "ADAPTER.md").read_text() == "x\n"


def test_too_many_missing_trials_are_measured_again_once_then_invalid(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    ev.step(context(ws, 1, dom, tmp_path))
    broken = rows({"q1": [1, 1], "q2": [1, 1], "q3": [1, 1], "q4": [1, 1]})
    material(ws, 2, broken, status={r: "error" for r in list(broken)[:4]})
    r = ev.step(context(ws, 2, dom, tmp_path))
    assert state(ws)["candidates"]["r00A"]["status"] == "retry" and r.extra["next"] == "r00A"
    material(ws, 3, broken, status={r: "error" for r in list(broken)[:4]})
    r = ev.step(context(ws, 3, dom, tmp_path))
    st = state(ws)
    assert st["candidates"]["r00A"]["status"] == "eval_invalid" and r.extra["next"] == "r00B"


def test_consecutive_failures_stop_the_run(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    ev = evolver(monkeypatch, Script({}))

    def broken(self, ctx, *a):
        raise RuntimeError("the analyst is down")
    monkeypatch.setattr(E.RRSIAdapterEvolver, "_open", broken)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0]}))
    statuses = [ev.step(context(ws, i, dom, tmp_path)).status for i in (1, 2, 3)]
    assert statuses == ["failed", "failed", "stop"]
    final = ev.finalize(context(ws, 3, dom, tmp_path))       # H_0 was measured; no round was ever decided
    assert final.extra["complete"] is False and "after 0 of 2 rounds: the run ended early" in final.summary
    # the Trainer heard that stop; one that runs on (allow_early_stop: false) gets the Evolver trying again at once
    r = ev.step(context(ws, 4, dom, tmp_path))
    assert r.status == "failed" and "the analyst is down" in r.summary and state(ws)["failures"] == 1


def test_the_roles_bridge_rebinds_upstreams_generate_only_while_bound():
    import rrsi.analyst
    import rrsi.critic
    import rrsi.digester
    original = rrsi.critic.generate
    with R.bound(lambda *a, **k: "{}"):
        assert rrsi.analyst.generate("x") == "{}" and rrsi.digester.generate("x") == "{}"
    assert rrsi.critic.generate is original


def test_smoke_failure_keeps_a_candidate_out(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    (tmp_path / "dom" / "domain.py").write_text(DOMAIN_PY + "    smoke_tasks = ['q3#1']\n")
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    seen = []

    def run_agent(task_ids, adapter=None):
        seen.append((task_ids, Path(adapter).name))
        status = "ok" if (Path(adapter) / "ADAPTER.md").read_text() == "x\n" else "error"
        return {"results": [{"id": t, "status": status, "output": None} for t in task_ids]}
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 1, dom, tmp_path, run_agent=run_agent, smoke=True))
    st = state(ws)
    assert seen == [(["q3#1"], "checked")] * 3                     # B's failed rollout was run once more
    assert st["round"]["variants"]["B"]["gate_failure"] == "smoke_fail" and r.extra["next"] == "r00A"


def test_a_smoke_rollout_that_fails_once_is_run_again(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    (tmp_path / "dom" / "domain.py").write_text(DOMAIN_PY + "    smoke_tasks = ['q3#1']\n")
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    calls = []

    def run_agent(task_ids, adapter=None):                # A's first smoke rollout fails, as a dropped connection
        calls.append(task_ids)
        return {"results": [{"id": t, "status": "error" if len(calls) == 1 else "ok", "output": None}
                            for t in task_ids]}
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 1, dom, tmp_path, run_agent=run_agent, smoke=True))
    st = state(ws)
    assert len(calls) == 3 and st["round"]["variants"]["A"]["cid"] == "r00A" and r.extra["next"] == "r00A"
    assert json.loads((ws.state / "rrsi" / "r0" / "A" / "smoke.json").read_text())["ok"] is True


def test_halted_rollouts_are_never_credited(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    (tmp_path / "dom" / "domain.py").write_text(DOMAIN_PY + """
    def halted_rows(self, material):
        return ["q1#1"] if (material / "HALTED").exists() else []
""")
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    (ws.learning_material / "step_0001" / "HALTED").write_text("")
    r = ev.step(context(ws, 1, dom, tmp_path))
    assert r.status == "failed" and state(ws)["candidates"]["h0"]["status"] == "queued"
    assert not (ws.state / "rrsi" / "frontier.json").exists()
    material(ws, 2, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 2, dom, tmp_path))
    assert r.status == "done" and r.extra["next"] == "r00A"


@pytest.mark.parametrize("extra", [
    # the plan held the session back past its wait (hermit.agent.base.cli: a session that never started)
    {"served": False, "unavailable": True, "started": False, "cancelled": False,
     "output_tail": "hermit: the plan held the session back for 43200 s"},
    # a session the plan refused for its usage limit (a CLI that is not on the plan's own login runs it once)
    {"served": False, "unavailable": False, "started": True, "cancelled": False,
     "output_tail": '{"type":"error","message":"You\'ve hit your usage limit."}'},
])
def test_a_plan_refusal_in_a_proposer_session_is_not_a_gate_failure(tmp_path, monkeypatch, extra):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    ev = evolver(monkeypatch, Script({}))

    class Refused(BaseAgent):
        def run(self, prompt, **kw):
            return Result(reply="", exit_code=-1 if not extra["started"] else 1, extra=dict(extra))
    monkeypatch.setattr(E, "make", lambda name, settings=None, model=None: Refused(settings))
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 1, dom, tmp_path))
    st = state(ws)
    assert r.status == "failed" and "plan did not serve a proposer session" in r.summary
    assert st["round"]["variants"] == {} and st["failures"] == 0
    assert not (ws.state / "rrsi" / "r0" / "A" / "prep.json").exists()


def test_a_host_side_write_replaces_a_named_pipe_instead_of_waiting_on_it(tmp_path):
    import threading
    from hermit.agent.evolver.methods.rrsi_adapter.store import _write
    path = tmp_path / "prompt_2.md"
    os.mkfifo(path)                                     # left by a proposer session where the next prompt goes
    writer = threading.Thread(target=_write, args=(path, "the prompt"), daemon=True)
    writer.start()
    writer.join(5)
    if writer.is_alive():                               # release the blocked open before failing
        os.close(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
    assert not writer.is_alive() and path.is_file() and path.read_text() == "the prompt"


def test_staging_keeps_what_git_commits_and_the_diff_is_independent_of_the_candidate(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "ADAPTER.md").write_text("a\n")
    cand = tmp_path / "cand"
    shutil.copytree(base, cand)
    (cand / "ADAPTER.md").write_text("b\n")
    (cand / "scripts" / "__pycache__").mkdir(parents=True)
    (cand / "scripts" / "x.py").write_text("print(1)\n")
    (cand / "scripts" / "__pycache__" / "x.cpython-39.pyc").write_bytes(b"\x00\x01")
    (cand / ".pytest_cache").mkdir()
    (cand / ".pytest_cache" / "README.md").write_text("cache\n")
    (cand / "tools" / ".git").mkdir(parents=True)
    (cand / "tools" / ".git" / "HEAD").write_text("ref\n")
    (cand / "helper").mkdir()
    (cand / "helper" / ".git").write_text("gitdir: /elsewhere/.git/worktrees/helper\n")   # a worktree's
    (cand / "notes").mkdir()
    (cand / "notes" / "secret.md").write_text("ignored\n")
    (cand / ".gitignore").write_text("notes/\n*.log\n")
    (cand / "caf\u00e9 \"x\".log").write_text("ignored, a name git quotes\n")
    (cand / os.fsdecode(b"raw\xff.log")).write_text("ignored, a name not in UTF-8\n")
    (cand / ".gitattributes").write_text("ADAPTER.md export-ignore\n")     # the Trainer's export would apply it
    (cand / "scripts" / ".gitattributes").write_text("* text eol=crlf\n")
    staged = tmp_path / "staged"
    dropped = E.stage(cand, staged)
    files = sorted(str(p.relative_to(staged)) for p in staged.rglob("*") if p.is_file())
    assert files == [".gitignore", "ADAPTER.md", "scripts/x.py"]
    assert {"notes/", ".pytest_cache/", "tools/.git/", "caf\u00e9 \"x\".log", os.fsdecode(b"raw\xff.log"),
            ".gitattributes", "scripts/.gitattributes", "helper/.git"} <= set(dropped)
    diff = E.diff_of(base, staged)
    assert "+b" in diff and "+++ b/scripts/x.py" in diff and "--- a/ADAPTER.md" in diff and "pyc" not in diff


def test_the_diff_reads_every_file_whatever_the_candidates_gitattributes_say(tmp_path):
    repo = tmp_path / "checkout"                      # a run kept inside a git checkout, as the examples' runs are
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    base, new = repo / "base", repo / "new"
    base.mkdir()
    (base / "ADAPTER.md").write_text("a\n")
    shutil.copytree(base, new)
    (new / "ADAPTER.md").write_text("a\nThe answer is SECRET-ANSWER.\n")
    (new / ".gitattributes").write_text("* -diff\n")
    diff = E.diff_of(base, new)
    assert "+The answer is SECRET-ANSWER." in diff and "Binary files" not in diff


def test_the_diff_shows_every_files_content_whatever_its_bytes_or_the_attributes_say(tmp_path, monkeypatch):
    base, new = tmp_path / "base", tmp_path / "new"
    base.mkdir()
    (base / "ADAPTER.md").write_text("a\n")
    (base / "old.bin").write_bytes(b"\x00\x01old\xff")              # the incumbent's own bytes
    shutil.copytree(base, new)
    (new / "hints.md").write_bytes(b"\x00 the answer is SECRET-ANSWER\n")
    (new / "notes.txt").write_bytes("caf\u00e9 latin\n".encode("latin-1"))
    (new / "old.bin").write_bytes(b"\x00\x01new\xff")
    config = tmp_path / "xdg"                                          # the user's own attributes file
    (config / "git").mkdir(parents=True)
    (config / "git" / "attributes").write_text("* -diff\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    diff = E.diff_of(base, new)
    assert "SECRET-ANSWER" in diff and "+caf\ufffd latin" in diff and "+\ufffd" not in diff.split("old.bin")[0]
    assert "Binary files" not in diff and "diff --git a/old.bin b/old.bin" in diff
    # a candidate's own files must be text: these go back to the proposer (the incumbent's are as they were)
    assert E.not_text_in(new, base) == ["hints.md", "notes.txt", "old.bin"]
    (new / "old.bin").write_bytes(b"\x00\x01old\xff")
    (new / os.fsdecode(b"caf\xe9.md")).write_text("a name not in UTF-8\n")
    assert E.not_text_in(new, base) == ["caf\ufffd.md", "hints.md", "notes.txt"]
    assert E.tree_digest(new)                                          # such a name is no crash


def test_the_diff_drops_the_copies_prefixes_on_header_lines_only(tmp_path):
    base, new = tmp_path / "base", tmp_path / "new"
    base.mkdir()
    (base / "run.sh").write_text("-- a/a/old\nkeep\n")
    shutil.copytree(base, new)
    (new / "run.sh").write_text("keep\npython a/a/tool.py b/b/x\n")
    diff = E.diff_of(base, new)
    assert "diff --git a/run.sh b/run.sh\n" in diff and "\n--- a/run.sh\n" in diff and "\n+++ b/run.sh\n" in diff
    assert "\n--- a/a/old\n" in diff and "\n+python a/a/tool.py b/b/x\n" in diff       # changed lines as they are
    (new / "added file.md").write_text("hi\n")                       # a new file and a deleted one, a space in a name
    (base / "gone.md").write_text("bye\n")
    diff = E.diff_of(base, new)
    assert "diff --git a/added file.md b/added file.md\n" in diff and "\n+++ b/added file.md" in diff
    assert "diff --git a/gone.md b/gone.md\n" in diff and "\n--- a/gone.md" in diff
    assert " a/b/" not in diff and " b/a/" not in diff
    (new / 'say "hi".md').write_text("q\n")                          # a name git quotes
    diff = E.diff_of(base, new)
    assert 'diff --git "a/say \\"hi\\".md" "b/say \\"hi\\".md"' in diff and '+++ "b/say \\"hi\\".md"' in diff


def test_a_proposers_directory_or_home_turned_into_a_link_ends_its_draft(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    (ws.state / "marker").write_text("state\n")
    script = Script({
        # A edits, declares too many edits (a repair is due) and turns its home into a link to the state
        (0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "work_links": {".codex-home": str(ws.state), "r0B": str(ws.state)},
                    "done": {"summary": "s", "edits": [edit("C1"), edit("C2"), edit("C3"), edit("C4")]}}],
        (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}],
    })
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 1, dom, tmp_path))
    st = state(ws)
    assert st["round"]["variants"]["A"]["gate_failure"] == "no_proposal"
    assert script.calls.count(((0, "A"), True)) == 0           # no repair session ran with the link as its home
    assert r.extra["next"] == "r00B" and (ws.scratch / "rrsi" / "r0B").is_dir() and \
        not (ws.scratch / "rrsi" / "r0B").is_symlink()          # B's directory made afresh, the planted link gone
    assert sorted(p.name for p in ws.state.iterdir() if p.name == "marker") == ["marker"] and \
        not (ws.state / "adapter").exists()


def test_links_left_in_the_proposers_home_are_removed_before_its_next_session(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    outside = tmp_path / "outside.toml"
    outside.write_text("mine\n")
    script = Script({
        (0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "work_links": {".codex-home/config.toml": str(outside)},
                    "done": {"summary": "s", "edits": [edit("C1"), edit("C2"), edit("C3"), edit("C4")]}},
                   {"done": {"summary": "s", "edits": [edit("C1")]}}],
        (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}],
    })
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    ev.step(context(ws, 1, dom, tmp_path))
    home = ws.scratch / "rrsi" / "r0A" / ".codex-home"
    assert script.calls[:2] == [((0, "A"), False), ((0, "A"), True)]       # the repair ran, in a real home
    assert not (home / "config.toml").is_symlink() and outside.read_text() == "mine\n"


def test_a_workspace_reached_through_a_link_runs_with_paths_hidden(tmp_path, monkeypatch):
    """The run's sandbox covers a hidden path under every name it shows (#69): a workspace given by a link's name
    runs, as any other."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)
    ws = workspace(tmp_path / "alias")
    dom = domain_files(tmp_path)
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 1, dom, tmp_path, sandbox=Box()))
    assert r.status == "done" and script.sessions and "reached through a link" not in r.summary


def test_setup_checks_what_every_step_needs(tmp_path):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    ev = E.RRSIAdapterEvolver()
    ctx = context(ws, 1, dom, tmp_path)
    ev.setup(ctx)
    with pytest.raises(ValueError, match="web_search"):
        ev.setup(context(ws, 1, dom, tmp_path, web_search=None))
    with pytest.raises(Exception):
        ev.setup(context(ws, 1, str(tmp_path / "dom" / "missing.py") + ":ToyDomain", tmp_path))
    with pytest.raises(ValueError, match="roles.model"):
        ev.setup(context(ws, 1, dom, tmp_path, roles={"model": "anthropic/claude-x"}))
    with pytest.raises(ValueError, match="roles.binding.*base_url"):       # not a failure at every step
        ev.setup(context(ws, 1, dom, tmp_path, roles={"binding": {"backend": "openai", "model": "m"}}))
    with pytest.raises(ValueError, match="roles.binding.*128k"):           # nor one its Codex arguments refuse
        ev.setup(context(ws, 1, dom, tmp_path, roles={"binding": {
            "backend": "x", "base_url": "http://x", "model": "m", "api_key_env": "K", "context_window": "128k"}}))
    with pytest.raises(ValueError, match="not RRSI hyperparameters"):
        ev.setup(context(ws, 1, dom, tmp_path, rrsi={"T": 2, "rounds": 3}))
    with pytest.raises(ValueError, match="m = 9"):                          # variants A..H
        ev.setup(context(ws, 1, dom, tmp_path, rrsi={"m": 9}))
    (tmp_path / "rrsi.json").write_text(json.dumps({"_doc": "a comment", "T": 2, "beta_1": 10}))
    with pytest.raises(ValueError, match=r"rrsi\.json.*beta_1"):                 # the file form too
        ev._cfg(SimpleNamespace(params={"rrsi": "rrsi.json"}, base=tmp_path))
    (tmp_path / "rrsi.json").write_text(json.dumps({"_doc": "a comment", "T": 2, "beta1": 10}))
    assert ev._cfg(SimpleNamespace(params={"rrsi": "rrsi.json"}, base=tmp_path)).beta1 == 10
    with pytest.raises(ValueError, match="critic_model.*no effect here"):
        ev.setup(context(ws, 1, dom, tmp_path, rrsi={"T": 2, "critic_model": "claude-x"}))
    with pytest.raises(ValueError, match="eval_parallel.*no effect here"):
        ev.setup(context(ws, 1, dom, tmp_path, rrsi={"T": 2, "eval_parallel": 4}))
    with pytest.raises(RuntimeError, match="no installed package"):
        ev.setup(context(ws, 1, dom, tmp_path, hide=["module:no_such_package_here"]))
    for name in ("rrsi.json", "rrsi_eng.json"):                     # the example's own files pass
        ev._cfg(SimpleNamespace(params={"rrsi": name}, base=ROOT / "examples" / "officeqa" / "rrsi_adapter"))


def test_a_failed_session_that_mentions_a_429_is_not_retried_as_the_providers(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    ev = evolver(monkeypatch, Script({}))
    calls = []

    class Failed(BaseAgent):                 # served (a turn finished), failed, with "429" in what it read
        def run(self, prompt, **kw):
            calls.append(1)
            return Result(reply="", exit_code=1, extra={**SERVED, "unavailable": True,
                                                        "output_tail": "grep found 'unexpected status 429'"})
    monkeypatch.setattr(E, "make", lambda name, settings=None, model=None: Failed(settings))
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 1, dom, tmp_path, session_retry_wait_s=0))
    # one session, not retried as the provider's failure; a failed session without a proposal fails the step
    assert len(calls) == 1 and r.status == "failed" and "failed without a proposal" in r.summary
    assert not (ws.state / "rrsi" / "r0" / "A" / "prep.json").exists()


def test_a_proposer_session_that_fails_without_a_proposal_fails_the_step_and_is_drafted_again(tmp_path,
                                                                                             monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({
        (0, "A"): [{"exit": 1}, {"files": {"ADAPTER.md": "# Skill\n\nAnswer with the bare number.\n"},
                                 "done": {"summary": "form", "edits": [edit()]}}],
        (0, "B"): [{"files": {"ADAPTER.md": "# Skill\n\nShorter.\n"}, "done": {"summary": "s", "edits": [edit()]}}],
    })
    ev = evolver(monkeypatch, script)
    base = rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]})
    material(ws, 1, base)
    r = ev.step(context(ws, 1, dom, tmp_path))
    assert r.status == "failed" and "r0A: a proposer session failed without a proposal" in r.summary
    assert not (ws.state / "rrsi" / "r0" / "A" / "prep.json").exists() and state(ws)["failures"] == 1
    history = (ws.state / "rrsi" / "history.jsonl").read_text()
    assert "no_proposal" not in history                     # never recorded as the proposer's choice
    material(ws, 2, base)                                    # the Trainer kept H_0: its rollouts again
    r = ev.step(context(ws, 2, dom, tmp_path))
    assert r.status == "done" and r.extra["next"] == "r00A", r.summary
    assert [key for key, resumed in script.calls if not resumed] == [(0, "A"), (0, "A"), (0, "B")]
    assert state(ws)["failures"] == 0


def test_officeqa_smoke_fails_a_rollout_without_an_answer():
    import importlib.util
    spec = importlib.util.spec_from_file_location("oqa_domain", ROOT / "examples/officeqa/rrsi_adapter/domain.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dom = module.OfficeQADomain()
    timed_out = {"answer": "", "fail_reason": "session timed out after 3600 s without an answer"}
    assert dom.smoke_failure({"status": "ok", "output": timed_out})
    exited = {"answer": "", "fail_reason": "the CLI exited 1 without an answer"}
    assert dom.smoke_failure({"status": "ok", "output": exited})
    assert not dom.smoke_failure({"status": "ok", "output": {"answer": "41.5", "fail_reason": ""}})    # wrong is alive
    # where run_agent reports how the session ended, only a timeout or the CLI failing fails the smoke
    no_tag = {"answer": "", "fail_reason": "no answer in the final message"}
    assert not dom.smoke_failure({"status": "ok", "output": no_tag, "timed_out": False, "exit_code": 0})
    assert dom.smoke_failure({"status": "ok", "output": timed_out, "timed_out": True, "exit_code": None})
    assert dom.smoke_failure({"status": "ok", "output": exited, "timed_out": False, "exit_code": 1})


def test_officeqa_smoke_ids_name_a_recorded_row_and_screening_survives_a_recursive_alias(tmp_path):
    import importlib.util
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("oqa_domain", ROOT / "examples/officeqa/rrsi_adapter/domain.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dom = module.OfficeQADomain()
    per_task = {"q1": SimpleNamespace(rewards=[0.0, 0.0], extra={"rows": [None, "q1#2"]}),
                "q2": SimpleNamespace(rewards=[0.0], extra={"rows": [None, None]}),
                "q3": SimpleNamespace(rewards=[0.0, 1.0], extra={"rows": ["q3#1", "q3#2"]})}
    assert dom.smoke_ids(per_task) == ["q1#2", "q3#1"]            # padded trials name no row; q2 has none
    assert dom.smoke_ids({"q2": per_task["q2"]}) == []
    (tmp_path / "exports.yaml").write_text("x: &x {self: *x}\n")
    assert dom.screen(tmp_path) == ["exports.yaml refers to itself (a recursive YAML alias)"]


def test_officeqa_rollouts_the_plan_never_ran_are_halted_not_failed(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("oqa_domain", ROOT / "examples/officeqa/rrsi_adapter/domain.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dom = module.OfficeQADomain()
    feedback = ("the pipeline failed: RuntimeError: agent codex could not be served after 3 attempts: hermit: the plan "
                "held the session back for 43200 s")
    held = {"id": "q1#1", "status": "error", "output": None, "judge": {"score": 0.0, "feedback": feedback}}
    broken = {"id": "q2#1", "status": "error", "output": None, "judge": {"score": 0.0, "feedback": "the pipeline "
                                                                         "failed: ValueError: bad adapter"}}
    assert dom.halted_result(held) and not dom.halted_result(broken)
    (tmp_path / "tasks").mkdir()
    for r in (held, broken):
        (tmp_path / "tasks" / f"{r['id'].replace('#', '_')}.json").write_text(json.dumps(r))
    assert dom.halted_rows(tmp_path) == ["q1#1"]


def test_officeqa_refuses_python_an_exports_yaml_names_however_it_is_spelled(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("oqa_domain", ROOT / "examples/officeqa/rrsi_adapter/domain.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dom = module.OfficeQADomain()
    cases = {
        "tools:\n  - name: look\n    entrypoint: tools.py:look\n": True,
        '"tools": [{"name": "look", "description": "d", "\\x65ntrypoint": "tools.py:look"}]\n': True,
        "agents: {'entrypoint': x.py:f}\n": True,
        "base: &b {entrypoint: x.py:f}\nagents:\n  <<: *b\n": True,
        "context_tool: reflections\n": True,
        "skills: skills\nhooks: hooks.json\nagents: agents\n": False,
        "tools: []\ncontext_tool: null\nreactions: {}\n": False,                # declared, naming nothing
        "not: [valid\n": True,
    }
    for text, refused in cases.items():
        adapter = tmp_path / f"a{len(list(tmp_path.iterdir()))}"
        adapter.mkdir()
        (adapter / "exports.yaml").write_text(text)
        assert bool(dom.screen(adapter)) is refused, text
    assert dom.screen(tmp_path / "missing") == []


def test_a_symbolic_link_in_a_candidate_is_sent_back_and_never_stored(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("the answers\n")
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]},
                                 "link": ("notes.md", secret)}, {}, {}, {}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 1, dom, tmp_path))
    st = state(ws)
    assert st["round"]["variants"]["A"]["gate_failure"] == "no_proposal" and r.extra["next"] == "r00B"
    assert any("symbolic links" in p for p in script.prompts)
    assert not any("the answers" in p.read_text() for p in (ws.state / "candidates").rglob("*") if p.is_file())


def test_a_candidate_file_that_is_not_text_is_sent_back_and_never_stored(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    utf16 = "the answer is SECRET-ANSWER\n".encode("utf-16")
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "bytes": {"notes.md": utf16},
                                 "done": {"summary": "s", "edits": [edit()]}},
                                {"files": {"notes.md": "the units table\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    ev.step(context(ws, 1, dom, tmp_path))
    assert any("UTF-8 text" in p and "notes.md" in p for p in script.prompts)
    stored = [p for p in (ws.state / "candidates").rglob("notes.md")]
    assert stored and all(p.read_text() == "the units table\n" for p in stored)


def test_components_the_agent_cannot_carry_are_never_offered_for_exploration(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    (tmp_path / "dom" / "domain.py").write_text(DOMAIN_PY + "    carriers = ['prompt', 'skill', 'memory']\n")
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    ev.step(context(ws, 1, dom, tmp_path))
    d = json.loads((ws.state / "rrsi" / "r0" / "directives.json").read_text())
    assert d["explore"]["untried"] == ["prompt", "skill", "memory"]
    assert "subagent" in d["uncarried"] and "control_flow" in d["uncarried"]
    assert "How the diff evidences each component" in (ws.scratch / "rrsi" / "r0A" / "context" / "round.md").read_text()


def test_a_working_copy_that_never_matches_is_invalid_after_one_more_hand_back(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    ev.step(context(ws, 1, dom, tmp_path))
    for step in (2, 3):
        (ws.adapter / "ADAPTER.md").write_text("what actually ran\n")
        material(ws, step, rows({"q1": [1, 1], "q2": [1, 1], "q3": [1, 1], "q4": [1, 1]}))
        r = ev.step(context(ws, step, dom, tmp_path))
    st = state(ws)
    assert st["candidates"]["r00A"]["status"] == "eval_invalid" and r.extra["next"] == "r00B"
    assert st["round"]["variants"]["A"]["gate_failure"] == "eval_invalid"


def test_a_failed_hand_back_keeps_the_candidate_queued(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    real = E.RRSIAdapterEvolver._hand_back
    calls = {"n": 0}

    def flaky(self, ctx, st, cid):
        calls["n"] += 1
        if cid == "r00A" and calls["n"] == 1:
            raise RuntimeError("the stored files of r00A changed after it was stored")
        return real(self, ctx, st, cid)
    monkeypatch.setattr(E.RRSIAdapterEvolver, "_hand_back", flaky)
    r = ev.step(context(ws, 1, dom, tmp_path))
    assert r.status == "failed" and state(ws)["round"]["queue"] == ["r00A", "r00B"]


def test_smoke_rows_with_an_errored_trial_fail_the_smoke():
    from hermit.agent.evolver.methods.rrsi_adapter.domain import AdapterDomain
    dom = AdapterDomain()
    assert dom.smoke_failure({"status": "ok", "output": {"status": "agent_error"}})
    assert dom.smoke_failure({"status": "ok", "output": {"status": "timeout"}})
    assert not dom.smoke_failure({"status": "ok", "output": {"status": "ok"}})
    assert not dom.smoke_failure({"status": "ok", "output": {"answer": "42"}})
    assert dom.smoke_failure({"status": "ok", "output": {"answer": ""}, "timed_out": True})


def test_the_digesters_shell_runs_through_the_sandbox_while_bound():
    import rrsi.digester
    original = rrsi.digester.subprocess
    calls = []

    class Fake:
        def run(self, *a, **k):
            calls.append(a)
    with R.bound(lambda *a, **k: "{}", Fake()):
        rrsi.digester.subprocess.run("grep x a.txt", shell=True)
    assert calls and rrsi.digester.subprocess is original


def test_the_digesters_shell_is_refused_when_the_sandbox_cannot_hide_paths(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    with pytest.raises(RuntimeError, match="bubblewrap"):
        R.SandboxedSubprocess(Box(), [state]).run("true", shell=True)       # Box: on, but no bwrap in its command
    assert R.SandboxedSubprocess(Box(), []).run("true", shell=True).returncode == 0


def test_the_digesters_shell_runs_in_the_visible_copy_of_the_traces(tmp_path):
    # Upstream's digester runs its shell with cwd = the round's rendered traces, under the state the sandbox covers;
    # the command must run in the visible copy, where its relative paths name the same files.
    state = tmp_path / "state"
    rendered = state / "rrsi" / "r0" / "analysis" / "rendered"
    rendered.mkdir(parents=True)
    (rendered / "T1.txt").write_text("hidden original\n")
    copy = tmp_path / "scratch" / "rrsi_roles" / "rendered_r0"
    copy.mkdir(parents=True)
    (copy / "T1.txt").write_text("visible copy\n")
    run = R.SandboxedSubprocess(None, [state], visible=copy).run
    out = run("cat T1.txt", shell=True, cwd=rendered, capture_output=True, text=True)
    assert out.stdout == "visible copy\n"
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "T1.txt").write_text("elsewhere\n")
    assert run("cat T1.txt", shell=True, cwd=other, capture_output=True, text=True).stdout == "elsewhere\n"
    assert R.SandboxedSubprocess(None, [state]).run("cat T1.txt", shell=True, cwd=rendered, capture_output=True,
                                                    text=True).stdout == "hidden original\n"


class Box:
    """The run's sandbox, on: covering the hidden paths is the Base Agent's (cli.py, HidingSandbox)."""
    enabled = True

    def wrap(self, command, **kw):
        return list(command), dict(kw.get("env") or {})


def test_the_proposer_runs_in_its_variant_with_the_runs_records_hidden(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    private = tmp_path / "private"
    private.mkdir()
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    ctx = context(ws, 1, dom, tmp_path, run_agent=lambda **kw: {"results": []}, sandbox=Box(),
                  hide=[str(private), "module:json"])
    assert ev.step(ctx).status == "done"
    a, b = script.sessions[0], script.sessions[1]
    work = ws.scratch / "rrsi" / "r0A"
    assert a["workspace"] == work and a["settings"]["home"] == work / ".codex-home"
    assert a["settings"]["log"] == ws.output / "r0A" / "session_1.jsonl" and a["settings"]["cancel"] is ctx.cancel
    assert a["tools"] == [ctx.tools["check_spec"]]            # no run_agent: the smoke is the Evolver's
    hidden = set(map(Path, a["limits"].hidden))
    package = Path(__import__("json").__file__).parent           # "module:<name>": the installed package
    assert {ws.learning_material, ws.state, ws.adapter, ws.output, private, package} <= hidden
    assert work not in hidden and package in set(ROLE_CALLS[-1]["limits"].hidden)
    assert work in set(map(Path, b["limits"].hidden))          # variant B never sees A's directory or home
    assert [m.source for m in a["limits"].readable] == [ws.task_info]


def test_with_the_runs_sandbox_off_nothing_is_asked_to_be_hidden(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    off = Box()
    off.enabled = False
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    assert ev.step(context(ws, 1, dom, tmp_path, sandbox=off)).status == "done"
    assert all(list(x["limits"].hidden) == [] for x in script.sessions)


def roles(tmp: Path, cancel=None, sandbox=None, traces: Path | None = None) -> "R.Roles":
    out = R.Roles(SimpleNamespace(cancel=cancel, sandbox=sandbox), {"model": "m", "codex_sandbox": "read-only",
                                                                    "args": ["-c", "x=1"]}, tmp / "roles",
                  hide=[tmp / "state"])
    out.traces = traces
    return out


def test_the_roles_are_codex_sessions_with_upstreams_generate_contract(tmp_path, monkeypatch):
    seen = []

    def reply(system, prompt):
        seen.append((system, prompt))
        return "" if len(seen) == 1 else 'Here: {"a": 1}'
    monkeypatch.setattr(R, "make", lambda name, settings=None, model=None: Role(settings, reply))
    monkeypatch.setattr(R.Roles, "_wait", lambda self, seconds: False)
    ROLE_CALLS.clear()
    traces = tmp_path / "rendered"
    traces.mkdir()
    r = roles(tmp_path, sandbox=Box(), traces=traces)
    assert json.loads(r.generate("the prompt", system="SYS", json_only=True, cache_prefix="PREFIX")) == {"a": 1}
    assert len(seen) == 2                                       # a session without a reply is run again
    system, prompt = seen[1]
    assert system.startswith("SYS") and "Output ONLY a single valid JSON object" in system and \
        prompt == "PREFIX\n\nthe prompt"
    call = ROLE_CALLS[1]
    s = call["settings"]
    assert s["stdin"] is True and s["codex_sandbox"] == "read-only" and s["args"][:2] == ["-c", "x=1"]
    call_dir = tmp_path / "roles" / "00002"
    assert s["home"] == call_dir / "home" and s["log"] == call_dir / "events.jsonl"
    assert call["workspace"] == tmp_path / "roles" / "00002" / "work"          # not a digester: an empty directory
    assert list(call["limits"].hidden) == [tmp_path / "state", tmp_path / "roles" / "00001"]   # sandbox on
    assert (tmp_path / "roles" / "00002" / "reply.txt").read_text() == 'Here: {"a": 1}'
    r.generate("p", system="You are a trajectory digester: a read-only investigator")
    assert ROLE_CALLS[-1]["workspace"] == traces                             # a digester runs in the traces


def test_a_role_session_the_trainer_stopped_or_the_plan_held_ends_the_step(tmp_path, monkeypatch):
    monkeypatch.setattr(R.Roles, "_wait", lambda self, seconds: False)
    for extra in ({**SERVED, "cancelled": True, "served": False},
                  {**SERVED, "served": False, "started": False, "unavailable": True,
                   "output_tail": "hermit: the plan held the session back for 43200 s"}):
        monkeypatch.setattr(R, "make", lambda name, settings=None, model=None, e=extra:
                            Role(settings, lambda *a: "", extra=e))
        with pytest.raises(R.Stop):
            roles(tmp_path).generate("p", system="s")
    calls = []

    def empty(system, prompt):
        calls.append(1)
        return ""
    monkeypatch.setattr(R, "make", lambda name, settings=None, model=None: Role(settings, empty))
    with pytest.raises(RuntimeError, match="generate failed after 3 Codex sessions"):
        roles(tmp_path).generate("p", max_retries=3)
    assert len(calls) == 3


def test_the_roles_usage_is_reported_with_the_proposers(tmp_path, monkeypatch):
    from hermit.agent.evolver.base import Usage
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    monkeypatch.setattr(R, "make", lambda name, settings=None, model=None:
                        Role(settings, usage=[Usage("luna", input_tokens=10, output_tokens=2, calls=1)]))
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 1, dom, tmp_path, roles={"model": "luna", "effort": "low"},
                        binding={"litellm": "anthropic/claude-x"}, price={"input": 15, "output": 75}))
    n = len(ROLE_CALLS)
    assert r.status == "done" and n >= 3                         # the analyst and both critics at least
    assert [(u.model, u.input_tokens, u.calls) for u in r.usage] == [("luna", 10 * n, n)]
    s = ROLE_CALLS[0]["settings"]
    assert s["model"] == "luna" and s["effort"] == "low" and s["codex_sandbox"] == "read-only" and \
        s["timeout_s"] == 1800 and s["cancel"] is None
    assert "binding" not in s and "price" not in s              # the proposer's model's, not the roles'
    # each call's record is kept with the step's output; its home (a copy of the sign-in) stays in the scratch
    rec = ws.output / "rrsi_roles" / "00001"
    assert s["log"] == rec / "events.jsonl" and (rec / "prompt.txt").is_file() and (rec / "reply.txt").is_file()
    assert s["home"] == ws.scratch / "rrsi_roles" / "00001" / "home" and not (rec / "home").exists()


def test_a_role_with_its_own_model_drops_the_proposers_provider_arguments(tmp_path):
    from hermit.agent.model.backends import Binding
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    spec = {"backend": "fireworks", "base_url": "https://api.fireworks.ai/inference/v1", "model": "glm",
            "api_key_env": "FIREWORKS_API_KEY", "apis": ["responses"]}
    provider = Binding.from_spec(spec).codex_args()
    ctx = context(ws, 1, dom, tmp_path, roles={"model": "gpt-5.6-luna", "login": "chatgpt"}, binding=spec,
                  model="glm", args=[*provider, "-c", "x=1"])
    roles = E.RRSIAdapterEvolver()._roles(ctx)
    assert roles.settings["args"] == ["-c", "x=1"] and roles.settings["login"] == "chatgpt"
    ctx = context(ws, 1, dom, tmp_path, binding=spec, model="glm", args=[*provider, "-c", "x=1"])
    assert E.RRSIAdapterEvolver()._roles(ctx).settings["args"] == [*provider, "-c", "x=1"]   # the proposer's model
    ctx = context(ws, 1, dom, tmp_path, roles={"args": ["-c", "y=2"]}, binding=spec, model="glm",
                  args=[*provider, "-c", "x=1"])
    assert E.RRSIAdapterEvolver()._roles(ctx).settings["args"] == [*provider, "-c", "y=2"]   # its own, its provider
    other = {**spec, "backend": "other", "base_url": "https://other.example/v1", "model": "o-1", "price": {"input": 1}}
    ctx = context(ws, 1, dom, tmp_path, roles={"binding": other}, binding=spec, model="glm",
                  args=[*provider, "-c", "x=1"])
    s = E.RRSIAdapterEvolver()._roles(ctx).settings                # the roles' own binding: its provider, model, price
    assert s["args"] == Binding.from_spec(other).codex_args() and s["model"] == "o-1" and s["price"] == {"input": 1}
    ctx = context(ws, 1, dom, tmp_path, roles={"binding": other}, login="chatgpt", model="gpt-5.6-luna")
    assert "login" not in E.RRSIAdapterEvolver()._roles(ctx).settings      # an API provider: not the ChatGPT login
    ctx = context(ws, 1, dom, tmp_path, roles={"model": "m", "login": None}, login="chatgpt", model="gpt-5.6-luna")
    assert "login" not in E.RRSIAdapterEvolver()._roles(ctx).settings      # cleared by `roles.login: null`
    ctx = context(ws, 1, dom, tmp_path, roles={"binding": other}, model="proposer-model")
    models = E.RRSIAdapterEvolver()._models(ctx)                    # what the run records: the binding's model
    assert models["analyst_model"] == models["critic_model"] == "o-1"


def test_the_roles_see_neither_the_records_nor_the_proposers_directories(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    assert ev.step(context(ws, 1, dom, tmp_path, sandbox=Box())).status == "done"
    hidden = set(ROLE_CALLS[-1]["limits"].hidden)                 # the last critic's, after both drafts
    assert {ws.learning_material, ws.state, ws.adapter, ws.output, ws.scratch / "rrsi"} <= hidden


def test_a_role_call_sees_neither_other_calls_nor_traces_it_is_not_given(tmp_path, monkeypatch):
    ROLE_CALLS.clear()
    monkeypatch.setattr(R, "make", lambda name, settings=None, model=None: Role(settings, lambda *a: "ok"))
    traces = tmp_path / "roles" / "rendered_r0"
    traces.mkdir(parents=True)
    r = roles(tmp_path, sandbox=SimpleNamespace(enabled=True), traces=traces)
    r.generate("p", system="the analyst")
    r.generate("p", system="the critic")
    r.generate("p", system="You are a trajectory digester")
    analyst, critic, digester = (set(c["limits"].hidden) for c in ROLE_CALLS)
    calls = tmp_path / "roles"
    assert traces in analyst and calls / "00001" not in analyst
    assert {traces, calls / "00001"} <= critic and calls / "00002" not in critic
    assert calls / "00002" in digester and traces not in digester and ROLE_CALLS[2]["workspace"] == traces
    assert tmp_path / "state" in analyst & critic & digester


def test_a_role_call_whose_session_times_out_twice_fails(tmp_path, monkeypatch):
    calls = []

    class Hangs(BaseAgent):
        def run(self, prompt, **kw):
            calls.append(1)
            return Result(reply="", exit_code=1, timed_out=True, extra=dict(SERVED))
    monkeypatch.setattr(R, "make", lambda name, settings=None, model=None: Hangs(settings))
    monkeypatch.setattr(R.Roles, "_wait", lambda self, seconds: False)
    with pytest.raises(RuntimeError, match="ran out of role_timeout_s"):
        roles(tmp_path).generate("p", system="s")
    assert len(calls) == 2                                      # not max_retries (6) sessions of role_timeout_s each


def test_a_role_call_the_provider_keeps_failing_ends_the_step_uncounted(tmp_path, monkeypatch):
    outcomes = []

    class Down(BaseAgent):
        def run(self, prompt, **kw):
            unavailable = outcomes.pop(0)
            return Result(reply="", exit_code=1, extra={**SERVED, "served": False, "unavailable": unavailable,
                                                        "output_tail": "unexpected status 503"})
    monkeypatch.setattr(R, "make", lambda name, settings=None, model=None: Down(settings))
    monkeypatch.setattr(R.Roles, "_wait", lambda self, seconds: False)
    outcomes[:] = [True] * 3
    with pytest.raises(R.Stop, match="provider kept failing"):    # as a proposer's: not counted
        roles(tmp_path).generate("p", system="s", max_retries=3)
    outcomes[:] = [True, False, True]
    with pytest.raises(RuntimeError, match="generate failed after 3"):   # not the provider alone: counted
        roles(tmp_path / "b").generate("p", system="s", max_retries=3)


def test_steps_the_trainer_stopped_count_toward_max_failures(tmp_path, monkeypatch):
    import dataclasses
    import threading
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    ev = evolver(monkeypatch, Script({}))

    def hangs(self, ctx, *a):
        raise R.Stop("stopped by the Trainer")
    monkeypatch.setattr(E.RRSIAdapterEvolver, "_open", hangs)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0]}))
    cancel = threading.Event()
    cancel.set()                                                # the Trainer's clock ended each call
    calls = []
    monkeypatch.setattr(E.RRSIAdapterEvolver, "_open", lambda self, ctx, *a: calls.append(1) or hangs(self, ctx))
    for i in (1, 2, 3):                                         # the Trainer replaces these steps' results
        ev.step(dataclasses.replace(context(ws, i, dom, tmp_path), cancel=cancel))
    assert state(ws)["failures"] == 3 and len(calls) == 3
    r = ev.step(context(ws, 4, dom, tmp_path))                  # the next step, which the Trainer takes as it is
    assert r.status == "stop" and "3 failed steps in a row" in r.summary and len(calls) == 3
    assert "ended early" in ev.finalize(context(ws, 4, dom, tmp_path)).summary
    # a Trainer without allow_early_stop runs on: the Evolver tries again rather than idle for the rest of the run
    ev.step(dataclasses.replace(context(ws, 5, dom, tmp_path), cancel=cancel))
    assert len(calls) == 4 and state(ws)["failures"] == 1           # tried again, and counted afresh
    raised = workspace(tmp_path / "raised")                     # an ordinary error after the clock ended the step:
    dom3 = domain_files(tmp_path / "raised")                     # counted, and its stop not taken as heard
    material(raised, 1, rows({"q1": [0, 1], "q2": [1, 0]}))
    monkeypatch.setattr(E.RRSIAdapterEvolver, "_open", lambda self, ctx, *a: (_ for _ in ()).throw(
        RuntimeError("the tool refused a stopped call")))
    for i in (1, 2, 3):
        ev.step(dataclasses.replace(context(raised, i, dom3, tmp_path / "raised"), cancel=cancel))
    r = ev.step(context(raised, 4, dom3, tmp_path / "raised"))
    assert r.status == "stop" and "3 failed steps in a row" in r.summary
    monkeypatch.setattr(E.RRSIAdapterEvolver, "_open", lambda self, ctx, *a: calls.append(1) or hangs(self, ctx))
    held = workspace(tmp_path / "held")                          # a plan's refusal (no cancel) is not counted
    dom2 = domain_files(tmp_path / "held")
    material(held, 1, rows({"q1": [0, 1], "q2": [1, 0]}))
    statuses = [ev.step(context(held, i, dom2, tmp_path / "held")).status for i in (1, 2, 3)]
    assert statuses == ["failed", "failed", "failed"] and state(held)["failures"] == 0


def test_a_redraft_starts_without_the_failed_drafts_files(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    leak = {"files": {"ADAPTER.md": "SECRET-ANSWER\n"}, "done": {"summary": "s", "edits": [edit()]}}
    script = Script({
        # A: rejected, repaired (still rejected), and the second repair session fails; at the next step a clean draft
        (0, "A"): [leak, {"files": {"ADAPTER.md": "SECRET-ANSWER, again\n"},
                          "done": {"summary": "s", "edits": [edit()]}},
                   {"exit": 1},
                   {"files": {"ADAPTER.md": "Check the units.\n"}, "done": {"summary": "s", "edits": [edit()]}}],
        (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}],
    })
    ev = evolver(monkeypatch, script)
    base = rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]})
    material(ws, 1, base)
    assert ev.step(context(ws, 1, dom, tmp_path)).status == "failed"
    vdir = ws.state / "rrsi" / "r0" / "A"
    assert (vdir / "critic_a1.json").is_file() and (vdir / "proposal_r1.json").is_file()
    material(ws, 2, base)
    assert ev.step(context(ws, 2, dom, tmp_path)).extra["next"] == "r00A"
    assert not (vdir / "critic_a1.json").exists() and not (vdir / "proposal_r1.json").exists()
    assert json.loads((vdir / "critic_a0.json").read_text())["verdict"] == "accept"


def test_a_roles_reply_counts_only_from_a_session_that_ended(tmp_path, monkeypatch):
    replies = []

    class Ended(BaseAgent):                    # the first session timed out after an intermediate message
        def run(self, prompt, **kw):
            replies.append(1)
            first = len(replies) == 1
            return Result(reply="half a thought" if first else "the answer", exit_code=1 if first else 0,
                          timed_out=first, extra=dict(SERVED))
    monkeypatch.setattr(R, "make", lambda name, settings=None, model=None: Ended(settings))
    monkeypatch.setattr(R.Roles, "_wait", lambda self, seconds: False)
    assert roles(tmp_path).generate("p", system="s") == "the answer" and len(replies) == 2


def test_officeqa_denylist_names_values_and_skips_years_and_round_numbers():
    import importlib.util
    spec = importlib.util.spec_from_file_location("oqa_domain", ROOT / "examples/officeqa/rrsi_adapter/domain.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    records = [{"judge": {"feedback": f"predicted 'x' but expected '{v}'"}}
               for v in ("1987", "100", "1000", "2,000", "4.73", "57,615.04", "[10102000000, 4.73]")]
    pats = module.OfficeQADomain().leak_patterns(records)
    hits = lambda line: [why for pat, why in pats if re.search(pat, line)]  # noqa: E731
    assert not hits("+ in 1987 the series") and not hits("+ at most 100 rows")
    assert not hits("+DOLLARS_PER_THOUSAND = 1000") and not hits("+ about 2,000 rows")   # round answers, whole
    assert hits("+ the value 57615.04") and hits("+ use 10,102,000,000 here")
    assert "'4.73'" in hits("+ ratio 4.73")[0]
    assert hits("+ the value 57,615.04") and hits("+ is 57615.04.")   # the bare answer, a separator, a full stop


def test_a_backend_failure_in_a_proposer_session_is_retried_not_recorded(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({(0, "A"): [{"files": {"ADAPTER.md": "x\n"}, "done": {"summary": "s", "edits": [edit()]}}],
                     (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    calls = {"n": 0}

    class Flaky(BaseAgent):
        def run(self, prompt, *, session=None, **kw):
            calls["n"] += 1
            if calls["n"] == 1:                 # the first session dies on a 429 before doing anything
                line = json.dumps({"type": "error", "message": "unexpected status 429 Too Many Requests"})
                Path(self.settings["log"]).write_text(line + "\n")
                return Result(reply="", exit_code=1, extra={**SERVED, "served": False, "unavailable": True,
                                                            "output_tail": line})
            return Proposer(script, self.settings).run(prompt, session=session, **kw)
    monkeypatch.setattr(E, "make", lambda name, settings=None, model=None: Flaky(settings))
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 1, dom, tmp_path, session_retry_wait_s=0))
    st = state(ws)
    assert r.status == "done" and r.extra["next"] == "r00A" and calls["n"] == 3
    assert st["round"]["variants"]["A"].get("gate_failure") is None
    assert "429" in (ws.output / "r0A" / "session_1.jsonl").read_text()       # the failed attempt's log is kept
    assert (ws.output / "r0A" / "session_1.retry1.jsonl").is_file()


def test_a_repair_that_changes_no_file_is_sent_back(tmp_path, monkeypatch):
    """Upstream starts each repair as a new propose() call with no edits counted: a repair that declares edits but
    writes nothing is bounced, even though the candidate differs from H_t."""
    ws = workspace(tmp_path)
    dom = domain_files(tmp_path)
    script = Script({
        (0, "A"): [{"files": {"ADAPTER.md": "SECRET-ANSWER\n"}, "done": {"summary": "s", "edits": [edit()]}},
                   {"done": {"summary": "s", "edits": [edit()]}},                       # the repair: no file change
                   {"files": {"ADAPTER.md": "Check the units.\n"}, "done": {"summary": "s", "edits": [edit()]}}],
        (0, "B"): [{"files": {"ADAPTER.md": "y\n"}, "done": {"summary": "s", "edits": [edit()]}}]})
    ev = evolver(monkeypatch, script)
    material(ws, 1, rows({"q1": [0, 1], "q2": [1, 0], "q3": [1, 1], "q4": [0, 0]}))
    r = ev.step(context(ws, 1, dom, tmp_path))
    assert any("ZERO file changes" in p for p in script.prompts)
    assert r.extra["next"] == "r00A" and (ws.adapter / "ADAPTER.md").read_text() == "Check the units.\n"
