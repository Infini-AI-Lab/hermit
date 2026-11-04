"""AgentDyn fixed-case replay and defense hooks (no attack generation).

The optional agentdojo distribution must come from AgentDyn, not upstream AgentDojo.
Each call constructs a fresh pipeline, defense and environment. Run untrusted defense
code in a separate networkless container; native tools and judges stay in the trusted episode worker.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid

from hermit.task import JudgeResult, RunOutput
from hermit.agent.evolver.methods.evosafe.hooks import ToolCallContext, ToolCallResult

# The sibling modules (victim, auditor) are imported by file name, however this file was loaded.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

AGENTDYN_REVISION = "5353cf7615b135cace8d07c8f12dac53a16b6db3"


def metered(usage):
    """Expose shared usage totals; incomplete usage/cost stays unknown, never repriced here."""
    if usage is None or usage["unknown_usage_calls"]:
        return None, None
    tokens = usage["input_tokens"] + usage["output_tokens"]
    return tokens, None if usage.get("unknown_cost_calls") else usage.get("cost_usd")


def encode(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def invoke(fn, *args):
    value = fn(*args)
    return asyncio.run(value) if inspect.isawaitable(value) else value



def case_fields(task: dict) -> dict:
    """The EvoSafe contract fields of an AgentDyn case: kind, group (one user task) and domain (the suite)."""
    return {**task, "kind": "injected" if task.get("injection_task_id") is not None else "clean",
            "group": f"{task['suite']}/{task['user_task_id']}", "domain": task["suite"]}


def validate_case(task: dict) -> None:
    if task.get("suite") not in {"shopping", "github", "dailylife"}:
        raise ValueError("suite must be shopping, github or dailylife (AgentDyn)")
    if not isinstance(task.get("user_task_id"), str) or not task["user_task_id"]:
        raise ValueError("user_task_id is required")
    injections = task.get("injections", {})
    if not isinstance(injections, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                 for k, v in injections.items()):
        raise ValueError("injections must map existing vector IDs to frozen strings")
    injected = task.get("injection_task_id") is not None
    if injected and (not isinstance(task["injection_task_id"], str) or not task["injection_task_id"]):
        raise ValueError("invalid injection_task_id")
    if injected != bool(injections):
        raise ValueError("injected cases need both an injection_task_id and nonempty injections; clean cases need neither")
    if "kind" in task and task["kind"] != ("injected" if injected else "clean"):
        raise ValueError("kind contradicts injection_task_id")


class GuardedRuntime:
    """A per-executor facade: no monkey patching of the suite or shared runtime."""

    def __init__(self, runtime, defense, instruction, trace_id, formatter, audit, server="agentdyn", classifier=None):
        self.functions = runtime.functions
        self.runtime, self.defense = runtime, defense
        self.instruction, self.trace_id = instruction, trace_id
        self.formatter, self.audit = formatter, audit
        self.server = server
        self.classifier = classifier

    def _refuse(self, name, args, reason, *, executed=False):
        self.audit.append({"tool": name, "blocked": True, "arguments": copy.deepcopy(args),
                           "phase": "post" if executed else "pre", "executed": executed})
        return "", f"Refused by security policy: {reason or 'outside authorized scope'}"

    def _structured(self, env, name, args):
        tools = {}
        for key, fn in self.functions.items():
            parameters = getattr(fn, "parameters", None)
            tools[key] = {"name": getattr(fn, "name", key),
                          "description": getattr(fn, "description", ""),
                          "parameters": parameters.model_json_schema() if parameters is not None else {}}
        before = copy.deepcopy(args)
        ctx = ToolCallContext("agentdyn", self.server, name, copy.deepcopy(args), self.trace_id,
                              {"instruction": self.instruction, "available_tools": tools})
        if self.classifier is not None:
            ctx.classify_output = lambda text: self.classifier.classify(self.instruction, text)
        try:
            revised_ctx = invoke(self.defense.on_pre_tool_call, ctx)
        except PermissionError as exc:
            return self._refuse(name, before, str(exc))
        if isinstance(revised_ctx, dict):
            # The dict API's decisions keep their meaning in the structured API.
            if set(revised_ctx) - {"arguments", "block"}:
                raise ValueError("unknown pre-hook decision fields")
            if "block" in revised_ctx:
                if not isinstance(revised_ctx["block"], str) or not revised_ctx["block"]:
                    raise ValueError("block must be a nonempty explanation")
                return self._refuse(name, before, revised_ctx["block"])
            if "arguments" in revised_ctx:
                if not isinstance(revised_ctx["arguments"], dict):
                    raise TypeError("arguments must be an object")
                ctx.arguments = copy.deepcopy(revised_ctx["arguments"])
        elif revised_ctx is not None:
            if not isinstance(revised_ctx, ToolCallContext):
                raise TypeError("structured pre-hook must return ToolCallContext, a decision dict or None")
            ctx = revised_ctx
        if (ctx.framework != "agentdyn" or ctx.server != self.server or ctx.tool_name != name
                or ctx.trace_id != self.trace_id or not isinstance(ctx.metadata, dict)
                or ctx.metadata.get("instruction") != self.instruction):
            raise ValueError("pre-hook may rewrite arguments and custom metadata, not call identity or trusted instruction")
        if not isinstance(ctx.arguments, dict):
            raise TypeError("hook arguments must be an object")
        args.clear()
        args.update(copy.deepcopy(ctx.arguments))
        started = time.monotonic()
        try:
            raw, error = self.runtime.run_function(env, name, args)
            result = ToolCallResult(copy.deepcopy(raw), error is not None, time.monotonic() - started, error=error)
        except Exception as exc:
            # Tell the post-hook the call failed (an isolated hook gets is_error only, not the exception),
            # but do not turn unexpected environment failures into a successful episode.
            result = ToolCallResult(is_error=True, duration=time.monotonic() - started, exception=exc)
            try:
                invoke(self.defense.on_post_tool_call, ctx, result)
            finally:
                raise
        original_text = self.formatter(raw)
        try:
            revised = invoke(self.defense.on_post_tool_call, ctx, result)
        except PermissionError as exc:
            return self._refuse(name, args, str(exc), executed=True)
        if revised is not None:
            if not isinstance(revised, ToolCallResult):
                raise TypeError("structured post-hook must return ToolCallResult or None")
            result = revised
        if isinstance(result.exception, PermissionError):
            return self._refuse(name, args, str(result.exception), executed=True)
        if result.exception is not None:
            raise result.exception
        if type(result.is_error) is not bool or (result.error is not None and not isinstance(result.error, str)):
            raise TypeError("invalid structured result error fields")
        text = self.formatter(result.raw)
        error = (result.error or "Tool reported an error") if result.is_error else None
        self.audit.append({"tool": name, "blocked": False, "requested_arguments": before,
                           "arguments": copy.deepcopy(args), "output_changed": text != original_text,
                           "hook_api": "structured-v1", "duration": result.duration})
        return text, error

    def run_function(self, env, name, args):
        if getattr(self.defense, "hook_api", "dict-v1") == "structured-v1":
            return self._structured(env, name, args)
        ctx = {"instruction": self.instruction, "trace_id": self.trace_id,
               "tool_name": name, "arguments": copy.deepcopy(args)}
        before = copy.deepcopy(args)
        try:
            decision = invoke(self.defense.on_pre_tool_call, copy.deepcopy(ctx)) if self.defense else None
        except PermissionError as exc:
            return self._refuse(name, before, str(exc))
        if decision is not None and not isinstance(decision, dict):
            raise TypeError("pre-hook must return None or a decision dict")
        decision = decision or {}
        if set(decision) - {"arguments", "block"}:
            raise ValueError("unknown pre-hook decision fields")
        if "block" in decision:
            if not isinstance(decision["block"], str) or not decision["block"]:
                raise ValueError("block must be a nonempty explanation")
            return self._refuse(name, before, decision["block"])
        if "arguments" in decision:
            if not isinstance(decision["arguments"], dict):
                raise TypeError("arguments must be an object")
            args.clear()
            args.update(copy.deepcopy(decision["arguments"]))
        ctx["arguments"] = copy.deepcopy(args)
        raw, error = self.runtime.run_function(env, name, args)
        # The native suite injected its environment before execution. The hook sees
        # the same serialized content the native executor would send to the model.
        result = {"text": self.formatter(raw), "error": error}
        try:
            revised = invoke(self.defense.on_post_tool_call, copy.deepcopy(ctx), copy.deepcopy(result)) if self.defense else None
        except PermissionError as exc:
            return self._refuse(name, args, str(exc), executed=True)
        if revised is not None:
            if (not isinstance(revised, dict) or set(revised) != {"text", "error"}
                    or not isinstance(revised["text"], str)
                    or (revised["error"] is not None and not isinstance(revised["error"], str))):
                raise TypeError("post-hook must return None or {text: str, error: str | None}")
            result = revised
        self.audit.append({"tool": name, "blocked": False, "requested_arguments": before,
                           "arguments": copy.deepcopy(args), "output_changed": revised is not None})
        return result["text"], result["error"]


def _run_episode(task: dict, adapter: Path | None, *, params: dict, base: Path) -> RunOutput:
    validate_case(task)
    from victim import model_params
    params = model_params(params)
    try:
        from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline, PipelineConfig
        from agentdojo.agent_pipeline.tool_execution import ToolsExecutionLoop, ToolsExecutor, tool_result_to_str
        from agentdojo.task_suite.load_suites import get_suite
    except ImportError as exc:
        raise RuntimeError("Install the pinned AgentDyn checkout in the rollout Python environment; see examples/agentdyn/README.md") from exc
    suite = get_suite(params.get("benchmark_version", "v1.2.2"), task["suite"])
    user_task = suite.get_user_task_by_id(task["user_task_id"])
    injection_task = suite.get_injection_task_by_id(task["injection_task_id"]) if task.get("injection_task_id") else None
    from isolated_hooks import IsolatedDefense
    defense = IsolatedDefense(adapter, image=params.get("hook_image", "python:3.12-slim"),
                              timeout=params.get("hook_timeout_s", 90),
                              name=params.get("_hook_container")) if adapter is not None else None
    logs = (base / params.get("logs", "runs/agentdyn-episodes")).resolve()
    if adapter is not None and logs.is_relative_to(adapter.resolve()):
        raise ValueError("episode logs must be outside the adapter")
    logs.mkdir(parents=True, exist_ok=True)
    log = Path(tempfile.mkdtemp(prefix="episode-", dir=logs))
    from victim import make_victim
    def record_call(event):
        with (log / "llm-calls.jsonl").open("a") as stream:
            stream.write(json.dumps(event) + "\n")
    victim = make_victim(params, base, on_call=record_call)
    classifier = None
    if (params.get("auditor") or {}).get("enabled"):
        from auditor import OutputClassifier
        def record_auditor_call(event):
            with (log / "llm-auditor-calls.jsonl").open("a") as stream:
                stream.write(json.dumps(event) + "\n")
        classifier = OutputClassifier(params, base, on_call=record_auditor_call)
    config = PipelineConfig(llm=victim, model_id=None, defense=None,
                            system_message_name=None, system_message=None, suite_name=task["suite"])
    # A hook failure here is the episode's failure: recorded, reported and cleaned up below like any other.
    transform_error = None
    if adapter is not None:
        prompt = config.system_message + "\n\n" + (adapter / "ADAPTER.md").read_text()
        try:
            transformed = invoke(defense.system_prompt_transform, prompt)
            if not isinstance(transformed, str):
                raise TypeError("system_prompt_transform must return str")
            config.system_message = transformed
        except Exception as exc:
            transform_error = exc
    pipeline = AgentPipeline.from_config(config)
    audit, captured = [], []
    trace_id = uuid.uuid4().hex

    class Executor(ToolsExecutor):
        def query(self, query, runtime, env, messages=(), extra_args=None):
            captured[:] = messages
            facade = GuardedRuntime(runtime, defense, query, trace_id, tool_result_to_str, audit, task["suite"], classifier)
            q, _, e, m, a = super().query(query, facade, env, messages, extra_args or {})
            captured[:] = m
            return q, runtime, e, m, a

    loops = [element for element in pipeline.elements if isinstance(element, ToolsExecutionLoop)]
    if len(loops) != 1:
        raise RuntimeError("AgentDyn pipeline changed: expected exactly one native tools loop")
    loop = loops[0]
    if sum(isinstance(e, ToolsExecutor) for e in loop.elements) != 1:
        raise RuntimeError("AgentDyn pipeline changed: expected exactly one native executor")
    loop.elements = [Executor(lambda text: text) if isinstance(e, ToolsExecutor) else e for e in loop.elements]
    loop.max_iters = int(params.get("max_iters", 15))
    if loop.max_iters < 1:
        raise ValueError("max_iters must be positive")
    original_query = pipeline.query

    def capture(*args, **kwargs):
        result = original_query(*args, **kwargs)
        captured[:] = result[3]
        # Native suites retry a missing terminal answer up to three times on the
        # same mutated environment. Treat exhaustion as an error immediately;
        # otherwise max_iters is not an effective episode bound.
        if (not captured or captured[-1].get("role") != "assistant" or captured[-1].get("tool_calls")
                or not captured[-1].get("content")):
            raise RuntimeError("episode did not produce a terminal assistant response")
        return result

    pipeline.query = capture
    started = time.monotonic()
    try:
        if transform_error is not None:
            raise transform_error
        utility, injection_success = suite.run_task_with_pipeline(
            pipeline, user_task, injection_task, task.get("injections", {}))
    except Exception as exc:
        (log / "failure.json").write_text(json.dumps({"case_id": task.get("id"),
            "user_task_id": task["user_task_id"], "error_type": type(exc).__name__}))
        (log / "trajectory.json").write_text(json.dumps(captured, default=encode))
        (log / "audit.json").write_text(json.dumps(audit))
        reason = "episode did not produce a terminal assistant response" if (
            isinstance(exc, RuntimeError) and str(exc) == "episode did not produce a terminal assistant response"
        ) else f"episode failed: {type(exc).__name__}; inspect local failure artifacts"
        failure = reason
    else:
        failure = None
    finally:
        if defense is not None:
            defense.close()
        if classifier is not None:
            classifier.close()
            (log / "auditor-usage.json").write_text(json.dumps(classifier.usage, indent=2))
            for key, value in classifier.usage.items():
                victim.usage[key] += value
        if victim is not None:
            victim.close()
            (log / "usage.json").write_text(json.dumps(victim.usage, indent=2))
    # Do not count an exhausted loop or missing terminal answer as a secure result.
    if failure is None and (not captured or captured[-1].get("role") != "assistant"
                            or captured[-1].get("tool_calls") or not captured[-1].get("content")):
        failure = "episode did not produce a terminal assistant response"
    if failure is None and (type(utility) is not bool
                            or (injection_task is not None and type(injection_success) is not bool)):
        failure = "native judge returned an invalid verdict"
    tokens, cost = metered(victim.usage)
    if failure is not None:
        # A failed episode is still a RunOutput, so the Trainer meters its usage and keeps its log; `score`
        # raises on it, and the run is recorded as failed (status judge_error), never as a verdict.
        (log / "trajectory.json").write_text(json.dumps(captured, default=encode))
        (log / "audit.json").write_text(json.dumps(audit))
        return RunOutput(output={"episode_error": failure}, messages=None, log=log,
                         wall_s=time.monotonic() - started, tokens=tokens, cost_usd=cost,
                         extra={"usage": victim.usage})
    result = {"utility": utility,
              "attack_success": injection_success if injection_task is not None else None,
              "kind": "injected" if injection_task is not None else "clean"}
    messages = json.loads(json.dumps(captured, default=encode))
    (log / "trajectory.json").write_text(json.dumps(messages, indent=2))
    (log / "audit.json").write_text(json.dumps(audit, indent=2))
    digest = hashlib.sha256()
    if adapter is not None:
        for name in ("ADAPTER.md", "defense.py"):
            digest.update(name.encode() + (adapter / name).read_bytes())
    provenance = {"reference_revision": AGENTDYN_REVISION, "model": params["model"],
                  "generation": {k: params[k] for k in ("max_tokens", "temperature", "top_p", "extra_body", "max_iters") if k in params},
                  "adapter_sha256": digest.hexdigest() if adapter else None,
                  "case_sha256": hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()}
    provenance["usage"] = victim.usage
    if classifier is not None:
        provenance["auditor_usage"] = classifier.usage
    (log / "result.json").write_text(json.dumps({**result, **provenance}, indent=2))
    return RunOutput(output=result, messages=messages, log=log, wall_s=time.monotonic() - started,
                     tokens=tokens, cost_usd=cost, extra=provenance)


def score(task: dict, output: RunOutput) -> JudgeResult:
    """The native verdicts as EvoSafe metrics (in `extra`; the scalar score is a single-number view only)."""
    result = output.output
    if isinstance(result, dict) and result.get("episode_error"):
        raise RuntimeError(result["episode_error"])
    injected = task.get("injection_task_id") is not None
    if type(result.get("utility")) is not bool or (injected and type(result.get("attack_success")) is not bool):
        raise ValueError("missing or invalid native verdicts")
    value = float(not result["attack_success"]) if injected else float(result["utility"])
    metrics = {"kind": "injected" if injected else "clean", "utility": result["utility"],
               "attack_success": result["attack_success"] if injected else None}
    return JudgeResult(value, json.dumps(result), extra={"metrics": metrics})


def run(task: dict, adapter: Path | None, *, params: dict, base: Path) -> RunOutput:
    """Enforce the whole episode deadline even on Hermit's direct-function path.

    episode_timeout_s is explicit because FunctionAgent does not pass agent.timeout_min.
    The supervisor always removes the episode's container, including on cancellation.
    """
    import os
    import signal
    import subprocess
    from isolated_hooks import remove_container
    timeout = float(params.get("episode_timeout_s", 900))
    if timeout <= 0:
        raise ValueError("episode_timeout_s must be positive")
    name = 'hermit-hook-' + uuid.uuid4().hex
    logs = (base / params.get("logs", "runs/agentdyn-episodes")).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='supervisor-', dir=logs))
    registry = os.environ.get('HERMIT_EPISODE_REGISTRY')
    registration = Path(registry) / name if registry and adapter is not None else None
    if registration is not None:
        registration.parent.mkdir(parents=True, exist_ok=True)
        registration.touch()
    result = Path(work) / 'result.json'
    job = {"task": task, "adapter": str(adapter.resolve()) if adapter else None,
           "base": str(base.resolve()), "params": {**params, "_hook_container": name, "logs": str(work / "episodes")},
           "result": str(result)}
    # stderr goes to disk rather than accumulating an unbounded provider log in RAM.
    with (Path(work) / 'stderr').open('wb') as errors:
        env = dict(os.environ)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[3]) + os.pathsep + env.get('PYTHONPATH', '')
        proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name('episode_worker.py'))],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors,
                                start_new_session=True, env=env)
        started = time.monotonic()
        try:
            proc.communicate(json.dumps(job).encode(), timeout=timeout)
            if proc.returncode or not result.is_file():
                return RunOutput(output={'episode_error': 'trusted episode worker failed; inspect supervisor stderr'},
                                 log=work, wall_s=time.monotonic() - started, extra={'usage_incomplete': True})
            value = json.loads(result.read_text())
            value['log'] = Path(value['log']) if value.get('log') else None
            return RunOutput(**value)
        except subprocess.TimeoutExpired:
            # Usage may be incomplete: never report a timeout as a blocked attack or zero cost.
            return RunOutput(output={"episode_error": "episode deadline exceeded"},
                             wall_s=time.monotonic() - started, log=work,
                             extra={"usage_incomplete": True})
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # the worker exited after poll()
                proc.wait(timeout=5)
            if adapter is not None:
                remove_container(name)
                if registration is not None:
                    registration.unlink(missing_ok=True)
