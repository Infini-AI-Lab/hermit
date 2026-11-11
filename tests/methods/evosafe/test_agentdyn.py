"""Offline defense contract, candidate selection and optional native AgentDyn tests."""
import sys
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil

import pytest

from hermit.agent.evolver.base import Access, Progress, StepContext, Workspace
from hermit.agent.evolver.loader import find, instantiate
from hermit.agent.evolver.methods.evosafe.contract import aggregate
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "examples/agentdyn/utils"))  # bridge, victim, auditor
from bridge import GuardedRuntime, run, score, validate_case
from defenses import load_defense
from hermit.spec import check_spec
from hermit.task import RunOutput

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "hermit/adapter/templates/evosafe"


class Runtime:
    functions = {}

    def __init__(self):
        self.calls = []

    def run_function(self, env, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        return "external fixture content", None


def test_hooks_block_rewrite_and_filter():
    class Defense:
        async def on_pre_tool_call(self, ctx):
            assert set(ctx) == {"instruction", "trace_id", "tool_name", "arguments"}
            return {"block": "outside requested scope"} if ctx["tool_name"] == "delete" else {"arguments": {"limit": 1}}

        async def on_post_tool_call(self, ctx, result):
            assert result["text"] == "external fixture content"
            return {"text": "filtered", "error": None}

    runtime, audit = Runtime(), []
    guarded = GuardedRuntime(runtime, Defense(), "read one item", "trace", str, audit)
    assert "Refused" in guarded.run_function({}, "delete", {})[1]
    assert runtime.calls == []
    args = {"limit": 50}
    assert guarded.run_function({}, "read", args) == ("filtered", None)
    assert runtime.calls == [("read", {"limit": 1})]
    assert args == {"limit": 1}
    assert audit[0]["blocked"] and audit[1]["output_changed"]


def test_hook_failure_is_not_silently_unprotected():
    class Bad:
        def on_pre_tool_call(self, ctx):
            raise RuntimeError("broken defense")
    runtime = Runtime()
    guarded = GuardedRuntime(runtime, Bad(), "task", "trace", str, [])
    with pytest.raises(RuntimeError, match="broken defense"):
        guarded.run_function({}, "read", {})
    assert not runtime.calls


def test_template_and_fresh_defense(tmp_path):
    assert check_spec(TEMPLATE, TEMPLATE).passed
    a, b = load_defense(TEMPLATE), load_defense(TEMPLATE)
    assert a is not b
    shutil.copytree(TEMPLATE, tmp_path / "adapter")
    (tmp_path / "adapter/defense.py").write_text("def build(): return object()")
    with pytest.raises(ValueError, match="missing"):
        load_defense(tmp_path / "adapter")


def row(id, clean, utility=True, attack=False):
    verdict = {"kind": "clean" if clean else "injected", "utility": utility, "attack_success": None if clean else attack}
    return {"id": id, "status": "ok", "output": dict(verdict), "judge": {"extra": {"metrics": verdict}}}


def test_metrics_direction_and_separate_denominators():
    results = [row("c", True), row("a", False, attack=True), row("b", False)]
    assert aggregate(results)["objective"] == 0.5
    assert score({"injection_task_id": "i"}, RunOutput(results[1]["output"])).score == 0
    assert score({"injection_task_id": "i"}, RunOutput(results[2]["output"])).score == 1
    with pytest.raises(ValueError):
        aggregate([*results, {"status": "error"}])
    with pytest.raises(ValueError):
        aggregate([results[0]])


def test_split_groups_and_no_answer_leakage(tmp_path):
    spec = importlib.util.spec_from_file_location("prepare_expanded", ROOT / "examples/agentdyn/utils/prepare_expanded.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for suite in module.SUITES:
        folder = tmp_path / suite
        folder.mkdir()
        for task in range(20):
            for goal in range(4):
                record = {"suite_name": suite, "user_task_id": f"user_task_{task}",
                          "injection_task_id": f"injection_task_{goal}", "injections": {"vector": "fixture"},
                          "messages": ["PRIVATE_OLD_ANSWER"], "utility": True}
                (folder / f"{task}-{goal}.json").write_text(json.dumps(record))
    cases, _ = module.read_collection(tmp_path)
    assert all(set(case) == {"suite", "user_task_id", "injection_task_id", "injections"} for case in cases.values())
    audit = {key: {"eligible": True} for key in cases}
    rows, splits, assignments, selected = module.select(cases, audit)
    assert len(rows) == 54 and {len(ids) for ids in splits.values()} == {18}
    assert module.select(dict(reversed(list(cases.items()))), audit) == (rows, splits, assignments, selected)
    by_id, owners = {row["id"]: row for row in rows}, {}
    for split, ids in splits.items():
        assert {by_id[i]["suite"] for i in ids[:6]} == set(module.SUITES)
        assert sum("injection_task_id" not in by_id[i] for i in ids[:6]) == 3
        for case_id in ids:
            row = by_id[case_id]
            group = (row["suite"], row["user_task_id"])
            assert group not in module.EXCLUSIONS
            assert owners.setdefault(group, split) == split
    for key, case in cases.items():
        audit[key]["eligible"] = case["suite"] != "github"
    with pytest.raises(ValueError, match="github"):
        module.select(cases, audit)


def test_report_requires_complete_coverage():
    spec = importlib.util.spec_from_file_location("report_agentdyn", ROOT / "examples/agentdyn/utils/report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = [row("c", True), row("a", False)]
    tasks = [{"id": r["id"], "kind": r["output"]["kind"]} for r in rows]
    tasks.append({"id": "extra", "kind": "clean"})
    assert module.summarize(rows, ["c", "a"], tasks=tasks)["selection_objective"] == 1.0
    for incomplete in (rows[:1], rows + [rows[0]], rows + [{"id": "extra", "status": "error"}]):
        assert module.summarize(incomplete, ["c", "a"], tasks=tasks)["selection_objective"] is None
    assert module.summarize(rows, ["c", "a"], repeats=2, tasks=tasks)["selection_objective"] is None


@pytest.mark.parametrize("case", ["missing", "duplicate", "unexpected", "missing_repeat", "failed", "complete", "complete_repeats"])
def test_report_defense_objective_requires_complete_coverage(case):
    spec = importlib.util.spec_from_file_location("report_agentdyn", ROOT / "examples/agentdyn/utils/report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = [row("c", True), row("a", False)]
    for record in rows:
        record["input"] = {"kind": record["output"]["kind"]}
    expected, repeats = ["c", "a"], 1
    if case == "missing":
        expected.append("missing")
    elif case == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif case == "unexpected":
        rows.append({**copy.deepcopy(rows[0]), "id": "extra"})
    elif case in ("missing_repeat", "complete_repeats"):
        repeats = 2
        if case == "complete_repeats":
            rows += [{**copy.deepcopy(record), "repeat": 2} for record in rows]
    elif case == "failed":
        rows[1].update(status="judge_error", judge={})
    report = module.summarize(rows, expected, repeats)
    complete = case in ("complete", "complete_repeats")
    assert report["complete"] is complete
    assert report["defense_metrics"]["objective"] == (1.0 if complete else None)
    assert report["selection_objective"] == (1.0 if complete else None)
    # Completed-episode rates remain available for diagnosis, even with missing coverage.
    assert report["defense_metrics"]["clean_utility_completed"] == 1.0
    if case != "failed":
        assert report["defense_metrics"]["asr_completed"] == 0.0


@pytest.mark.parametrize("bad", [{"injections": {"x": "y"}}, {"injection_task_id": "i"}])
def test_reject_mislabeled_cases(bad):
    with pytest.raises(ValueError):
        validate_case({"suite": "shopping", "user_task_id": "user_task_0", **bad})


@pytest.mark.parametrize("mode,accepted", [("improve", True), ("regress", False), ("error", False), ("tie", False),
                                         ("rational_tie", False), ("invalid", False)])
def test_evolver_selection_and_rollback(tmp_path, mode, accepted):
    paths = {name: tmp_path / name for name in Workspace.__dataclass_fields__}
    for path in paths.values():
        path.mkdir()
    shutil.copytree(TEMPLATE, paths["adapter"], dirs_exist_ok=True)
    validation = tmp_path / "validation"
    validation.mkdir()
    (validation / "tasks.jsonl").write_text('{"id":"c","kind":"clean"}\n{"id":"a","kind":"injected"}\n')
    original = (paths["adapter"] / "ADAPTER.md").read_text()
    calls = []

    def evaluate(task_ids, adapter):
        calls.append(adapter)
        changed = Path(adapter) != paths["adapter"]
        if changed and mode == "error":
            return {"results": [{"id": "c", "status": "error"}]}
        if mode == "rational_tie":
            # 2/3 - 0/6 and 3/3 - 2/6 are equal; binary floats differ by 1e-16.
            return {"results": [row(f"c{i}", True, utility=changed or i < 2) for i in range(3)]
                    + [row(f"a{i}", False, attack=changed and i < 2) for i in range(6)]}
        return {"results": [row("c", True, utility=not (changed and mode == "regress")),
                            row("a", False, attack=not changed or mode == "tie")]}

    def llm(**kwargs):
        files = {n: (paths["adapter"] / n).read_text() for n in ("ADAPTER.md", "defense.py", "BUNDLE.md")}
        files["ADAPTER.md"] = "New defense instructions."
        if mode == "invalid":
            files["defense.py"] = "not valid python ???"
        return {"content": json.dumps({"summary": "candidate", "files": files})}

    ctx = StepContext(workspace=Workspace(**paths), access=Access(validation=validation), params={},
                      tools={"run_agent": evaluate, "llm": llm,
                             "check_spec": lambda path: check_spec(Path(path), TEMPLATE).to_dict()},
                      progress=Progress(1, 1, True))
    if mode == "rational_tie":
        ctx.params['eval_task_ids'] = [f"c{i}" for i in range(3)] + [f"a{i}" for i in range(6)]
    evolver, _ = instantiate(find("evosafe"), {})
    result = evolver.step(ctx)
    assert result.extra["accepted"] is accepted
    assert ((paths["adapter"] / "ADAPTER.md").read_text() != original) is accepted
    assert (paths["output"] / "selection.json").exists()
    assert len(calls) == (1 if mode == "invalid" else 2)


def test_trainer_evosafe_end_to_end(tmp_path, monkeypatch):
    """Real Trainer snapshots, tools, material, checks and commits; deterministic task/model doubles."""
    import yaml
    from hermit.config import Config
    from hermit.trainer.trainer import Trainer
    from hermit.tools import FunctionTool

    (tmp_path / "pipeline.py").write_text('''
from hermit.task import RunOutput
def run(task, adapter):
    improved = "tested revision" in (adapter / "ADAPTER.md").read_text()
    clean = task["kind"] == "clean"
    return RunOutput(output={"kind": task["kind"], "utility": True,
                     "attack_success": None if clean else not improved}, cost_usd=0.0, tokens=0)
''')
    (tmp_path / "judge.py").write_text("from bridge import score\n")
    rows = [{"id": f"{split}-{kind}", "kind": kind,
             **({"injection_task_id": "fixture"} if kind == "injected" else {})}
            for split in ("train", "val", "test") for kind in ("clean", "injected")]
    (tmp_path / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for split in ("train", "val", "test"):
        (tmp_path / f"{split}.txt").write_text(f"{split}-clean\n{split}-injected\n")
    config = {"data": {"tasks": "tasks.jsonl", "train": "train.txt", "val": "val.txt", "test": "test.txt", "step_size": 2},
              "template": str(TEMPLATE), "agent": {"name": "pipeline.py:run", "concurrency": 1},
              "judge": {"path": "judge.py"}, "evolver": {"name": "evosafe"},
              "sandbox": {"enabled": False}, "trainer": {"run_name": "offline", "output_dir": "runs", "epochs": 1}}
    (tmp_path / "run.yaml").write_text(yaml.safe_dump(config))
    original_tools = Trainer._tools

    def tools(self, *args, **kwargs):
        result = original_tools(self, *args, **kwargs)
        def propose(**kwargs):
            files = {name: (self.ws.adapter / name).read_text() for name in ("ADAPTER.md", "defense.py", "BUNDLE.md")}
            files["ADAPTER.md"] += "\ntested revision\n"
            return {"content": json.dumps({"summary": "offline proposal", "files": files})}
        result["llm"] = FunctionTool("llm", "offline proposer", propose, source="run")
        return result

    monkeypatch.setattr(Trainer, "_tools", tools)
    trainer = Trainer(Config.load(tmp_path / "run.yaml", []), log=lambda *_: None)
    trainer.train()
    assert "tested revision" in (trainer.ws.adapter / "ADAPTER.md").read_text()
    report = json.loads((tmp_path / "runs/offline/step_0001/output/selection.json").read_text())
    assert report["accepted"]
    assert set(report["eval_task_ids"]) == {"val-clean", "val-injected"}


def test_native_shopping_pipeline_without_model(tmp_path, monkeypatch):
    """Native environment, tools and judges; replace only the model with a scripted reader."""
    pytest.importorskip("agentdojo")
    from agentdojo.agent_pipeline import agent_pipeline as api
    from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
    from agentdojo.functions_runtime import FunctionCall
    from agentdojo.task_suite.load_suites import get_suite
    from agentdojo.types import text_content_block_from_string
    try:
        suite = get_suite("v1.2.2", "shopping")
    except KeyError:
        pytest.skip("upstream AgentDojo installed, not AgentDyn")

    class Reader(BasePipelineElement):
        name = "offline-reader"
        usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "unknown_usage_calls": 0}

        def close(self):
            pass

        def query(self, query, runtime, env, messages=(), extra_args=None):
            if messages[-1]["role"] == "tool":
                msg = {"role": "assistant", "content": [text_content_block_from_string("Done")], "tool_calls": None}
            else:
                msg = {"role": "assistant", "content": None, "tool_calls": [
                    FunctionCall(function="search_product", args={"product_name": "smart watch"}, id="read")]}
            return query, runtime, env, [*messages, msg], extra_args or {}

    import victim
    monkeypatch.setattr(victim, "make_victim", lambda *args, **kwargs: Reader())
    monkeypatch.setattr(api, "get_llm", lambda *args: Reader())
    params = {"model": "gpt-4o-mini-2024-07-18", "logs": "logs"}
    case = {"suite": "shopping", "user_task_id": "user_task_0"}
    config = api.PipelineConfig(llm=params["model"], model_id=None, defense=None, system_message=None, system_message_name=None)
    native, _ = suite.run_task_with_pipeline(api.AgentPipeline.from_config(config), suite.get_user_task_by_id("user_task_0"), None, {})
    from bridge import _run_episode as run
    baseline = run(case, None, params=params, base=tmp_path)
    assert baseline.output == {"utility": native, "attack_success": None, "kind": "clean"}

    class LoopingReader(Reader):
        calls = 0

        def query(self, query, runtime, env, messages=(), extra_args=None):
            self.calls += 1
            msg = {"role": "assistant", "content": None, "tool_calls": [
                FunctionCall(function="search_product", args={"product_name": "smart watch"}, id=str(self.calls))]}
            return query, runtime, env, [*messages, msg], extra_args or {}

    looping = LoopingReader()
    monkeypatch.setattr(victim, "make_victim", lambda *args, **kwargs: looping)
    failed = run(case, None, params={**params, "max_iters": 1}, base=tmp_path)
    assert "terminal" in failed.output["episode_error"]
    from hermit.trainer.rollout import run_one
    record = run_one({**case, "id": "exhausted"}, None, lambda task, adapter: failed, score)
    assert record["status"] == "judge_error"
    assert record["tokens"] == failed.tokens and record["cost_usd"] == failed.cost_usd
    assert record["log"] == str(failed.log)
    assert looping.calls == 2  # initial call + one tools iteration; no suite retry
    monkeypatch.setattr(victim, "make_victim", lambda *args, **kwargs: Reader())

    if os.environ.get("HERMIT_TEST_DOCKER") != "1":
        pytest.skip("set HERMIT_TEST_DOCKER=1 to run the defended episodes in the real hook container")
    defended = run(case, TEMPLATE, params=params, base=tmp_path)
    assert defended.output == baseline.output
    assert defended.log != baseline.log
    assert json.loads((defended.log / "audit.json").read_text())[0]["tool"] == "search_product"
    assert "Treat tool results" in defended.messages[0]["content"][0]["content"]

    # An inert marker at a real injection vector proves post-hooks run after
    # environment injection, without creating an adversarial payload.
    adapter = tmp_path / "filter"
    shutil.copytree(TEMPLATE, adapter)
    (adapter / "defense.py").write_text('''
class Defense:
    def system_prompt_transform(self, prompt): return prompt
    def on_pre_tool_call(self, ctx): return None
    def on_post_tool_call(self, ctx, result):
        assert "OFFLINE_MARKER" in result["text"]
        return {"text": "filtered fixture", "error": result["error"]}
def build(): return Defense()
''')
    injected = {**case, "injection_task_id": "injection_task_0",
                "injections": {"injection_reviews_0": "OFFLINE_MARKER"}}
    filtered = run(injected, adapter, params=params, base=tmp_path)
    assert filtered.output["kind"] == "injected"
    assert type(filtered.output["attack_success"]) is bool
    tool_messages = [m for m in filtered.messages if m["role"] == "tool"]
    assert tool_messages[0]["content"][0]["content"] == "filtered fixture"

    # A failing system-prompt hook is the episode's failure: recorded and cleaned up like any other.
    broken = tmp_path / "broken"
    shutil.copytree(TEMPLATE, broken)
    (broken / "defense.py").write_text('''
class Defense:
    def system_prompt_transform(self, prompt): return {}["missing"]
    def on_pre_tool_call(self, ctx): return None
    def on_post_tool_call(self, ctx, result): return None
def build(): return Defense()
''')
    prompt_failure = run(case, broken, params=params, base=tmp_path)
    assert prompt_failure.output["episode_error"].startswith("episode failed: RuntimeError")
    assert (prompt_failure.log / "usage.json").is_file()  # the episode's cleanup ran


def test_native_three_suite_initialization():
    pytest.importorskip("agentdojo")
    from agentdojo.task_suite import get_suite
    spec = importlib.util.spec_from_file_location("prepare_expanded", ROOT / "examples/agentdyn/utils/prepare_expanded.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = {}
    for suite in module.SUITES:
        s = get_suite("v1.2.2", suite)
        vector, content = next(iter(s.get_injection_vector_defaults().items()))
        row = {"suite": suite, "user_task_id": "user_task_0", "injection_task_id": "injection_task_1" if suite == "github" else "injection_task_0",
               "injections": {vector: content}}
        cases[module.digest(row)] = row
    inventory, audit = module.audit_native(cases)
    assert set(inventory) == set(module.SUITES)
    assert all(a["eligible"] for a in audit.values())
    assert all(a["initial_clean_utility"] is False for a in audit.values())


def test_report_rejects_missing_and_separates_goal_namespaces():
    spec = importlib.util.spec_from_file_location("compare_agentdyn", ROOT / "examples/agentdyn/utils/compare.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tasks=[{'id':'a','suite':'shopping','user_task_id':'user_task_0'},
           {'id':'b','suite':'shopping','user_task_id':'user_task_0','injection_task_id':'injection_task_0'},
           {'id':'c','suite':'github','user_task_id':'user_task_0','injection_task_id':'injection_task_0'}]
    rows=[{'id':'a','status':'ok','output':{'kind':'clean','utility':True,'attack_success':None}},
          {'id':'b','status':'ok','output':{'kind':'injected','utility':True,'attack_success':False}},
          {'id':'c','status':'error','output':None}]
    report=module.compare(tasks,['a','b','c'],{'baseline':rows})['runs']['baseline']
    assert not report['complete']
    assert report['overall']['objective'] is None
    assert report['overall']['errors']==1
    assert set(report['by_injection_goal'])=={'shopping/injection_task_0','github/injection_task_0'}
    assert report['overall']['secure_task_completion']['successes']==1
    with pytest.raises(ValueError,match='missing'):
        module.compare(tasks,['a','b','c'],{'baseline':rows[:2]})


def test_a_clean_case_with_empty_injection_fields_is_the_same_case(tmp_path):
    spec = importlib.util.spec_from_file_location("prepare_cases", ROOT / "examples/agentdyn/utils/prepare.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = [{"suite": "shopping", "user_task_id": f"user_task_{n}", "injection_task_id": "injection_task_0",
              "injections": {"vector": "fixture"}} for n in range(10)]
    cases += [{"suite": "shopping", "user_task_id": "user_task_0"},
              {"suite": "shopping", "user_task_id": "user_task_0", "injection_task_id": None, "injections": {}}]
    module.prepare(cases, tmp_path / "out")
    rows = [json.loads(s) for s in (tmp_path / "out/tasks.jsonl").read_text().splitlines()]
    assert sum(r["user_task_id"] == "user_task_0" and r["kind"] == "clean" for r in rows) == 1


@pytest.mark.parametrize("goals", [0, -1])
def test_all_groups_needs_a_positive_goal_count(tmp_path, goals):
    path = ROOT / "examples/agentdyn/utils/prepare_all_groups.py"
    spec = importlib.util.spec_from_file_location("prepare_groups", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match="positive"):
        module.prepare(tmp_path / "inventory", tmp_path / "base", tmp_path / "out", goals=goals)


def test_full_test_keeps_search_cases_and_only_adds_test_owned_variants(tmp_path):
    path = ROOT / "examples/agentdyn/utils/prepare_all_groups.py"
    spec = importlib.util.spec_from_file_location("prepare_groups", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    inventory, base = tmp_path / "inventory", tmp_path / "base"
    (inventory / "full").mkdir(parents=True)
    base.mkdir()
    rows, groups = [], []
    for split in module.SPLITS:
        groups.append({"suite": "github", "user_task_id": split, "split": split})
        members = [dict(id=f"{split}-{n}", suite="github", user_task_id=split,
                        injection_task_id=f"injection_task_{n}" if n else None,
                        injections={"vector": f"frozen-{n}"} if n else {}) for n in range(4)]
        rows.extend(members)
        (base / f"{split}.txt").write_text("".join(r["id"] + "\n" for r in members[:3]))
    (inventory / "full/tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (inventory / "full/manifest.json").write_text(json.dumps({"groups": groups}))

    original, expanded = tmp_path / "original", tmp_path / "expanded"
    module.prepare(inventory, base, original)
    report = module.prepare(inventory, base, expanded, full_test=True)
    assert report["counts"] == {"train": 3, "val": 3, "test": 4}
    for split in ("train", "val"):
        assert (expanded / f"{split}.txt").read_bytes() == (original / f"{split}.txt").read_bytes()
    assert (expanded / "test.txt").read_text() == (original / "test.txt").read_text() + "test-3\n"
    old_rows = {r["id"]: r for r in map(json.loads, (original / "tasks.jsonl").read_text().splitlines())}
    new_rows = {r["id"]: r for r in map(json.loads, (expanded / "tasks.jsonl").read_text().splitlines())}
    assert {i: new_rows[i] for i in old_rows} == old_rows
    assert new_rows.keys() - old_rows.keys() == {"test-3"}
    assert new_rows["test-3"]["injections"] == {"vector": "frozen-3"}
