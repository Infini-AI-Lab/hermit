"""Harness² on Codex: the harness2-codex template (upstream's Codex tree as an adapter), the Codex acting agent's
workspace and home, the upstream Evolver's Codex layout, and the paper's protocol end to end on the Trainer's toy
task with a scripted session channel (no model, no Docker, no network)."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

import pytest

from hermit.config import Config
from hermit.spec import check_spec
from hermit.trainer.trainer import Trainer
from test_training import make_task

pytest.importorskip("harness2.core.schedule")

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "hermit" / "algorithms" / "templates" / "harness2_codex"
AGENT_DIR = ROOT / "examples" / "officeqa" / "utils" / "agent"
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
    from hermit.algorithms.evolvers.harness2_upstream import surface as U
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
        U.layout_of(ROOT / "hermit" / "algorithms" / "templates" / "prompt_only")


def test_codex_agent_deploys_the_tree_and_writes_the_home(tmp_path):
    sys.path.insert(0, str(AGENT_DIR))
    import codex_agent
    from hermit.algorithms.evolvers.harness2_upstream import surface as U
    adapter = tmp_path / "adapter"
    U.layout_of(TEMPLATE).to_adapter(full_tree(), TEMPLATE, adapter)
    work, home = tmp_path / "work", tmp_path / "home"
    work.mkdir(), home.mkdir()
    deployed = codex_agent.deploy_harness(adapter, work)
    assert (work / "AGENTS.md").read_text() == full_tree()["AGENTS.md"]
    assert (work / ".agents" / "skills" / "tables" / "scripts" / "sum.py").is_file()
    assert (work / "scripts" / "check.py").is_file() and (work / ".codex" / "hooks.json").is_file()
    assert not (work / "sub_agents").exists() and not (work / "ADAPTER.md").exists()   # roles go to the home; no framework files
    assert deployed["skills"] == ["tables"] and deployed["scripts"] == ["check.py"] and deployed["hooks"]
    assert [r["name"] for r in deployed["sub_agents"]] == ["reader"]
    assert deployed["sub_agents"][0]["instructions"] == "Summarise what you read."
    binding = SimpleNamespace(codex_args=lambda: ["-c", 'model_provider="fw"', "-c", 'model_providers.fw.env_key="FW_KEY"'],
                              key_env_if_set=lambda: "FW_KEY")
    assert codex_agent.write_home(home, binding, deployed["sub_agents"]) == ["FW_KEY"]
    import tomllib
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["model_provider"] == "fw" and config["features"] == {"shell_snapshot": False, "multi_agent": True}
    assert config["shell_environment_policy"]["inherit"] == "core"
    assert config["agents"]["reader"] == {"description": "reads long files", "config_file": "agents/reader.toml"}
    role = tomllib.loads((home / "agents" / "reader.toml").read_text())
    assert role["developer_instructions"] == "Summarise what you read."
    work2 = tmp_path / "work2"
    work2.mkdir()
    assert codex_agent.deploy_harness(None, work2) == {"agents_md": False, "skills": [], "scripts": [], "hooks": False,
                                                       "sub_agents": [], "memory": False, "context": False}
    assert "multi_agent" not in (codex_agent.write_home(tmp_path / "home2", binding, []), (tmp_path / "home2" / "config.toml").read_text())[1]


def test_bench_reads_a_codex_session(tmp_path):
    """The bench renders a Codex session's event stream as the trajectory and task.md as the task prompt."""
    from hermit.algorithms.evolvers.harness2_upstream.bench import FrameworkBench
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


# The scripted channel on the Codex tree: the INV and DOM editors add a triggered skill, the composer writes the rule
# the toy pipeline needs into AGENTS.md, the reflector writes a lesson naming AGENTS.md.
PROPOSER = r'''
import json, shutil, tempfile
from pathlib import Path
from harness2.core.proposer import Reply
from hermit.algorithms.evolvers.harness2_upstream.channel import FrameworkProposer

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
