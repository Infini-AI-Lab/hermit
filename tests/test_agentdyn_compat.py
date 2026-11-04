"""Portable EvoSafe structured hooks and native bridge compatibility."""
import sys
from pathlib import Path
import copy
import json
from dataclasses import replace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples/agentdyn/utils"))  # bridge, victim, auditor
from bridge import GuardedRuntime
from hermit.agent.evolver.methods.evosafe.hooks import Defense, ToolCallResult


class Runtime:
    functions = {}

    def __init__(self, error=None):
        self.calls = []
        self.raw = {"value": 1, "items": ["public fact"]}
        self.error = error

    def run_function(self, env, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        return self.raw, self.error


def guarded(runtime, defense, audit=None):
    return GuardedRuntime(runtime, defense, "complete my request", "trace", str,
                          audit if audit is not None else [], "shopping")


def test_structured_mutation_context_raw_and_state():
    class Stateful(Defense):
        def __init__(self):
            self.calls = 0

        async def on_pre_tool_call(self, ctx):
            assert ctx.framework == "agentdyn" and ctx.server == "shopping"
            assert ctx.metadata == {"instruction": "complete my request", "available_tools": {}}
            self.calls += 1
            ctx.arguments["limit"] = self.calls
            ctx.metadata["checked"] = True
            return replace(ctx)

        async def on_post_tool_call(self, ctx, result):
            assert ctx.metadata["checked"] is True
            assert result.duration >= 0 and result.exception is None
            assert isinstance(result.raw, dict)
            result.raw["items"] = ["sanitized"]

    runtime, defense, audit = Runtime(), Stateful(), []
    for count in (1, 2):
        args = {"limit": 30}
        result = guarded(runtime, defense, audit).run_function({}, "read", args)
        assert "sanitized" in result[0]
        assert args == {"limit": count}
    assert runtime.raw["items"] == ["public fact"]  # no mutation of environment-owned output
    assert all(a["output_changed"] for a in audit)


def test_native_model_list_can_be_classified_without_changing_observation():
    pytest.importorskip('agentdojo')
    from pydantic import BaseModel
    from agentdojo.agent_pipeline.tool_execution import tool_result_to_str

    class NativeMessage(BaseModel):
        body: str

    seen = []

    class Classifier:
        def classify(self, instruction, text):
            seen.append(json.loads(text))
            return {'status': 'ok', 'has_injected_instruction': False}

    class Observe(Defense):
        def on_post_tool_call(self, ctx, result):
            payload = [item.model_dump(mode='json') for item in result.raw]
            ctx.classify_output(json.dumps(payload))

    runtime, audit = Runtime(), []
    runtime.raw = [NativeMessage(body='ordinary task fact')]
    original = tool_result_to_str(runtime.raw)
    wrapped = GuardedRuntime(runtime, Observe(), 'read my message', 'trace', tool_result_to_str,
                             audit, 'dailylife', Classifier())
    assert wrapped.run_function({}, 'read', {}) == (original, None)
    assert seen == [[{'body': 'ordinary task fact'}]]
    assert not audit[0]['output_changed']
    assert isinstance(runtime.raw[0], NativeMessage)


@pytest.mark.parametrize("phase", ["pre", "post"])
def test_permission_refusal_has_explicit_execution_boundary(phase):
    class Refuse(Defense):
        def on_pre_tool_call(self, ctx):
            if phase == "pre":
                raise PermissionError("outside user scope")

        def on_post_tool_call(self, ctx, result):
            raise PermissionError("do not reveal this result")

    runtime, audit = Runtime(), []
    text, error = guarded(runtime, Refuse(), audit).run_function({}, "read", {})
    assert not text and "Refused by security policy" in error
    assert bool(runtime.calls) == (phase == "post")
    assert audit[0]["phase"] == phase
    assert audit[0]["executed"] == (phase == "post")


def test_original_error_text_and_replacement_result():
    class Repair(Defense):
        def on_post_tool_call(self, ctx, result):
            assert result.is_error and result.error == "missing value"
            assert result.exception is None
            return ToolCallResult(raw="try another item")

    runtime = Runtime(error="missing value")
    assert guarded(runtime, Defense()).run_function({}, "read", {})[1] == "missing value"
    assert guarded(runtime, Repair()).run_function({}, "read", {}) == ("try another item", None)


def test_invalid_context_and_hook_failure_do_not_execute():
    class Rename(Defense):
        def on_pre_tool_call(self, ctx):
            ctx.tool_name = "other"

    class Crash(Defense):
        def on_pre_tool_call(self, ctx):
            raise RuntimeError("bug")

    runtime = Runtime()
    with pytest.raises(ValueError, match="call identity"):
        guarded(runtime, Rename()).run_function({}, "read", {})
    with pytest.raises(RuntimeError, match="bug"):
        guarded(runtime, Crash()).run_function({}, "read", {})
    assert runtime.calls == []


def test_unexpected_runtime_failure_reaches_hook_and_remains_failure():
    class BrokenRuntime(Runtime):
        def run_function(self, env, name, args):
            raise RuntimeError("environment failed")

    seen = []

    class Observe(Defense):
        def on_post_tool_call(self, ctx, result):
            seen.append(result.exception)
            return ToolCallResult(raw="cannot conceal this failure")

    with pytest.raises(RuntimeError, match="environment failed"):
        guarded(BrokenRuntime(), Observe()).run_function({}, "read", {})
    assert isinstance(seen[0], RuntimeError)


def test_public_schema_only_no_implementation_dependency():
    from types import SimpleNamespace

    class Parameters:
        @classmethod
        def model_json_schema(cls):
            return {"type": "object", "properties": {"query": {"type": "string"}}}

    class Inspect(Defense):
        def on_pre_tool_call(self, ctx):
            assert ctx.metadata["available_tools"] == {"read": {
                "name": "read", "description": "Read public data", "parameters": Parameters.model_json_schema()}}
            assert not ({"task_id", "injection_task_id", "risk_category", "threat_model"} & ctx.metadata.keys())

    runtime = Runtime()
    runtime.functions = {"read": SimpleNamespace(name="read", description="Read public data", parameters=Parameters,
                                                run=lambda: None, dependencies={"hidden": object()})}
    guarded(runtime, Inspect()).run_function({}, "read", {})


def test_bounded_frozen_classifier_no_tools_and_unknowns(tmp_path, monkeypatch):
    import auditor as module
    calls = []

    class Victim:
        usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "unknown_usage_calls": 0}

        def query(self, query, runtime, env, messages):
            assert runtime.functions == {} and env is None
            assert "untrusted_output" in messages[1]["content"][0]["content"]
            self.usage = {**self.usage, "calls": 1, "input_tokens": 10, "output_tokens": 5}
            return None, None, None, [{"content": [{"content": json.dumps({
                "has_injected_instruction": False, "reason": "ordinary fixture"})}], "tool_calls": None}], None

        def close(self):
            calls.append("closed")

    def factory(params, base, on_call=None):
        assert params["model"] == "frozen-model"
        assert params["max_retries"] == 0
        calls.append("created")
        return Victim()

    monkeypatch.setattr(module, "make_victim", factory)
    auditor = module.OutputClassifier({"model": "frozen-model", "auditor": {"max_calls": 1, "max_chars": 20}}, tmp_path)
    assert auditor.classify("request", "a" * 21)["status"] == "input_too_large"
    assert calls == []
    assert auditor.classify("request", "plain fact")["has_injected_instruction"] is False
    assert auditor.classify("request", "plain fact")["status"] == "budget_exhausted"
    assert auditor.usage["input_tokens"] == 10
    auditor.close()
    assert calls == ["created", "closed"]

    def failure(*args, **kwargs):
        raise RuntimeError("secret must not propagate")

    monkeypatch.setattr(module, "make_victim", failure)
    result = module.OutputClassifier({"model": "frozen-model"}, tmp_path).classify("request", "fact")
    assert result == {"status": "error", "has_injected_instruction": None, "reason": "RuntimeError"}


def test_episode_meter_counts_known_usage_only():
    from bridge import metered
    usage = {"input_tokens": 15, "output_tokens": 2, "unknown_usage_calls": 0,
             "cost_usd": 0.000009, "unknown_cost_calls": 0}
    assert metered(usage) == (17, 0.000009)
    assert metered({**usage, "unknown_cost_calls": 1}) == (17, None)
    assert metered({**usage, "unknown_usage_calls": 1}) == (None, None)


def test_failed_episode_is_metered_and_recorded_as_a_failed_run(tmp_path):
    """Without a core change: the bridge returns the failed episode's usage and log, its judge raises, and the
    Trainer's rollout records a failed run (never a verdict) that keeps them."""
    from bridge import score
    from hermit.task import RunOutput
    from hermit.trainer.rollout import run_one
    task = {"id": "c1", "kind": "clean", "suite": "shopping", "user_task_id": "user_task_0"}
    out = RunOutput(output={"episode_error": "episode did not produce a terminal assistant response"},
                    log=tmp_path, tokens=17, cost_usd=0.5)
    row = run_one(task, None, lambda task, adapter: out, score)
    assert row["status"] != "ok" and "terminal assistant response" in row["error"]
    assert row["tokens"] == 17 and row["cost_usd"] == 0.5 and row["log"] == str(tmp_path)
    from hermit.agent.evolver.methods.evosafe.contract import summary, verdict
    assert verdict(row) == {} and summary([row])["errors_clean"] == 1


def test_candidate_dataclass_with_postponed_annotations(tmp_path):
    import sys
    from conftest import load_defense
    (tmp_path / "defense.py").write_text('''
from __future__ import annotations
from dataclasses import dataclass, field
from hermit.agent.evolver.methods.evosafe.hooks import Defense
@dataclass
class Guard(Defense):
    observed: list[str] = field(default_factory=list)
def build(): return Guard()
''')
    before = {name for name in sys.modules if name.startswith("_evosafe_candidate_")}
    first, second = load_defense(tmp_path), load_defense(tmp_path)
    first.observed.append("first")
    assert second.observed == []
    assert {name for name in sys.modules if name.startswith("_evosafe_candidate_")} == before


def test_classifier_outcomes_are_audited_without_output_or_model_reason(tmp_path, monkeypatch):
    import hashlib
    import auditor as module
    from types import SimpleNamespace
    private = 'private fixture that must not be logged'
    def query(*args, **kwargs):
        return None, None, None, [{'content': json.dumps({
            'has_injected_instruction': True, 'reason': private}), 'tool_calls': None}], None
    victim = SimpleNamespace(query=query, close=lambda: None, usage={
        'calls': 1, 'input_tokens': 10, 'output_tokens': 4, 'unknown_usage_calls': 0})
    monkeypatch.setattr(module, 'make_victim', lambda *args, **kwargs: victim)
    events = []
    classifier = module.OutputClassifier({'model': 'frozen', 'auditor': {
        'max_calls': 1, 'max_chars': 100}}, tmp_path, on_call=events.append)
    assert classifier.classify('trusted request', 'x' * 101)['status'] == 'input_too_large'
    assert classifier.classify('trusted request', private)['has_injected_instruction'] is True
    assert classifier.classify('trusted request', private)['status'] == 'budget_exhausted'
    assert [e['status'] for e in events] == ['input_too_large', 'ok', 'budget_exhausted']
    assert [e['attempt_count'] for e in events] == [0, 1, 1]
    assert events[1]['output_sha256'] == hashlib.sha256(private.encode()).hexdigest()
    assert events[1]['reason'] == 'redirection_detected'
    assert all(e['event'] == 'classification' for e in events)
    assert private not in json.dumps(events) and 'trusted request' not in json.dumps(events)

    def failure(*args, **kwargs):
        raise RuntimeError(private)
    monkeypatch.setattr(module, 'make_victim', failure)
    module.OutputClassifier({'model': 'frozen'}, tmp_path, on_call=events.append).classify('request', private)
    assert events[-1]['status'] == 'error' and events[-1]['has_injected_instruction'] is None
    assert events[-1]['reason'] == 'classification_failed' and events[-1]['attempt_count'] == 1
    assert private not in json.dumps(events)


def test_structured_hooks_accept_the_dict_decisions():
    """A structured Defense that returns the dict API's decisions refuses or rewrites as that API
    would, instead of failing the episode (a correct refusal must never count as an attack)."""
    class DictStyle(Defense):
        def on_pre_tool_call(self, ctx):
            if ctx.tool_name == "git_star":
                return {"block": "not part of the user's request"}
            if ctx.tool_name == "read":
                return {"arguments": {"limit": 1}}
            return None

    runtime, audit = Runtime(), []
    text, error = guarded(runtime, DictStyle(), audit).run_function({}, "git_star", {"repo": "x"})
    assert text == "" and "not part of the user's request" in error
    assert runtime.calls == [] and audit[-1]["blocked"] is True
    args = {"limit": 30}
    guarded(runtime, DictStyle(), audit).run_function({}, "read", args)
    assert runtime.calls == [("read", {"limit": 1})] and args == {"limit": 1}

    class Undefined(Defense):
        def on_pre_tool_call(self, ctx):
            return "block"
    with pytest.raises(TypeError):
        guarded(Runtime(), Undefined()).run_function({}, "read", {})
