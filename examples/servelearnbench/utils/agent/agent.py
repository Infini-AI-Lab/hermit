"""ServeLearnBench: one episode of a scenario, played by the benchmark's own agent loop over an adapter.

The loop, the budgets, the tools, the stamped instruction and the verifier are ServeLearnBench's (`servelearnbench.agent`
and `.engine`). The adapter enters at the two places the benchmark leaves open to a method: its context is appended
to the system prompt (the benchmark's `extra_system`), and the tools it declares (MWM's `wm`) are added to the tool
list, served outside the environment's 50-call budget and capped at WM_CALL_CAP per episode, as in every MWM venue.

Settings come from the environment (the launcher sets them) or, for the model, from `agent.params.model` (a
`<backend>/<model>` reference, resolved by the framework; SLB_MODEL, SLB_BASE_URL and SLB_API_KEY override it):
  SLB_WORKDIR            the run's task directory: evidence/ (the learner's store), rows/, episodes/
  SLB_EVIDENCE           the evidence store the adapter's tools read (default <SLB_WORKDIR>/evidence); a replayed
                         test binds a snapshot of what the adapter had been trained on (windows.py)
  SLB_MODEL              the host model, as the endpoint names it (default GLM 5.3 flash on Fireworks)
  SLB_BASE_URL           the endpoint (default Fireworks; a wire meter in the examples). The launcher sets both
                         from a backend reference (MW_MODEL, harnessadapter.backends)
  SLB_API_KEY            the key (ServeLearnBench's own variable; the launcher takes it from the backend's)
  SLB_REASONING_EFFORT   default high (the paper's setting)

Per episode this writes: the benchmark's result row (rows/<id>.json: phase, window, category, reward, verifier
detail, trace, usage — the host-side archive windows.py assembles into a `slb score` file), the record the learner
may read (episodes/<id>/record.json, the task's log), and for a serving episode its evidence file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

from harnessadapter import Session
from harnessadapter.agents import Agent
from harnessadapter.task import RunOutput

HERE = Path(__file__).resolve().parent

UTILS = HERE.parent
MWM = HERE.parent / "task-info" / "mwm"     # the venue's texts for MWM


from harnessadapter.algorithms.evolvers.mwm.evidence import servelearnbench as evidence   # the MWM learner's records

WM_CALL_CAP = 100
CAP_NOTICE = f"wm call limit reached for this episode ({WM_CALL_CAP}); act on the environment."
DEFAULT_MODEL = "fireworks/accounts/fireworks/models/glm-5p3-flash"      # a backend reference


def _default_endpoint() -> tuple[str, str]:
    from harnessadapter.backends import registry, resolve
    binding = resolve(DEFAULT_MODEL, registry())
    return binding.model, binding.base_url

_lock = threading.Lock()
_state: dict = {}


def _setup(scenario: str, params: dict | None = None) -> dict:
    """The bundle, its tasks by id, the window map and the model client, once per process."""
    with _lock:
        if _state.get("scenario") == scenario:
            return _state
        model, base_url = _default_endpoint()
        spec = (params or {}).get("model")
        if isinstance(spec, dict) and spec.get("base_url") and spec.get("model"):     # agent.params.model, resolved
            from harnessadapter.backends import Binding
            binding = Binding.from_spec(spec)
            model, base_url = binding.model, binding.base_url
            if binding.key() and not os.environ.get("SLB_API_KEY"):
                os.environ["SLB_API_KEY"] = binding.key()           # the benchmark's client reads its own variable
        from servelearnbench import llm
        from servelearnbench.scenarios import get_bundle, n_windows
        bundle = get_bundle(scenario, fresh=True)
        tasks = {t.task_id: t for t in bundle["serving"]}
        for w in range(n_windows(bundle)):
            for t in bundle["test_set"](w):
                tasks[t.task_id] = t
        windows = json.loads((UTILS / "windows.json").read_text())
        if windows["scenario"] != scenario:
            raise RuntimeError(f"windows.json is for {windows['scenario']}, the task row says {scenario}")
        window_of = {}
        for w in windows["windows"]:
            for tid in w["serving"] + w["test"]:
                window_of[tid] = w["window"]
        cfg = llm.ModelConfig(model=os.environ.get("SLB_MODEL", model), base_url=os.environ.get("SLB_BASE_URL", base_url),
                              reasoning_effort=os.environ.get("SLB_REASONING_EFFORT", "high") or None)
        _state.update({"scenario": scenario, "bundle": bundle, "tasks": tasks, "window_of": window_of,
                       "model": llm.LLM(cfg), "model_name": cfg.model})
        return _state


def _deployment() -> dict:
    from harnessadapter.algorithms.evolvers.mwm import deployment as _dep
    return _dep.host_settings(MWM)


def wm_text(result) -> str:
    """A tool result as the host reads it; a paged `read` of the mwm template gets the way to the next page."""
    text = Session.text(result)
    if isinstance(result, dict) and "text" in result and result.get("eof") is False \
            and result.get("next_offset") is not None:
        followup = {"path": result.get("path"), "offset": result["next_offset"], "limit": result.get("limit", 4000)}
        if result.get("sha256"):
            followup["expected_sha256"] = result["sha256"]
        text += '\n[More text available; continue when useful: wm(name="read", args=' + json.dumps(followup) + ')]'
    return text


class ServeLearnBenchAgent(Agent):
    """The benchmark's own agent loop with the adapter plugged in: its context on the system prompt, its tools
    beside the environment's, served through Session. `agent.name: ../utils/agent/agent.py:ServeLearnBenchAgent`."""

    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        from servelearnbench import agent, llm, scoring
        from servelearnbench.engine.types import FINISH_ACTION_NAME, Action

        st = _setup(task["scenario"], self.params)
        bundle, meta = st["bundle"], st["bundle"]["meta"]
        domain = meta["domain"]
        workdir = Path(os.environ["SLB_WORKDIR"])
        evidence_dir = Path(os.environ.get("SLB_EVIDENCE") or workdir / "evidence")
        t = st["tasks"][task["id"]]
        t.instruction = task["instruction"]          # the stamped instruction, as prepare.py exported it

        session = self.session(adapter, evidence=evidence_dir, deployment=_deployment())
        env = agent.new_env(bundle)
        env.reset(t)
        specs = agent.tool_specs(env.tools_info, domain)
        system = meta["system_prompt"]
        if session is not None:
            context = session.context()
            if context:
                system += "\n\n" + context
            specs = specs + session.tool_specs("openai")
        adapter_tools = session.tool_names if session is not None else set()
        package_chars = len(system) - len(meta["system_prompt"])
        wm_calls = {"n": 0}

        def execute(name: str, args: dict) -> str:
            if name in adapter_tools:
                if wm_calls["n"] >= WM_CALL_CAP:
                    return CAP_NOTICE
                wm_calls["n"] += 1
                try:
                    return wm_text(session.call(name, args if isinstance(args, dict) else {}))
                except Exception as exc:                        # noqa: BLE001 — a tool error is an observation
                    return f"Error: {type(exc).__name__}: {exc}"
            if name not in env.tools_map:
                raise agent.UnknownTool(agent.MSG_UNKNOWN.format(name=name))
            return env.step(Action(name=name, kwargs=args)).observation

        llm.usage_reset()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": t.instruction}]
        t0 = time.time()
        trace = agent.run_turns(st["model"], messages, specs, execute,
                                on_finish=lambda args: env.step(Action(name=FINISH_ACTION_NAME, kwargs=args)))
        wall = round(time.time() - t0, 1)
        reward = env.compute_reward()
        usage = llm.agent_usage()

        # The benchmark's own result row (host side; never mounted for the learner).
        z = t.z or {}
        window = st["window_of"].get(task["id"])
        row = {"scenario": task["scenario"], "method": os.environ.get("SLB_METHOD", "adapter"),
               "model": st["model_name"], "phase": task["phase"], "window": window, "timestamp": task["timestamp"],
               "task_id": task["id"], "tier": z.get("tier"), "family": z.get("family"), "slice": z.get("slice"),
               "category": scoring.category(domain, z.get("slice")), "reward": reward.reward,
               "answer_detail": reward.answer_detail, "instruction": t.instruction, "tokens": usage,
               "judge_tokens": llm.judge_usage(), "trace": trace, "wm_calls": wm_calls["n"],
               "wm_tools": sorted(adapter_tools), "package_chars": package_chars,
               "adapter": str(adapter) if adapter else None}
        rows = workdir / "rows"
        rows.mkdir(parents=True, exist_ok=True)
        (rows / f"{task['id']}.json").write_text(json.dumps(row, ensure_ascii=False, default=str))

        # The learner's view: the record (the task's log), and for a serving episode its evidence file and entry.
        i = int(task["timestamp"])
        block = int(task.get("block") or 0)
        ep = evidence.episode_record(i, i, block, t.instruction, reward.reward, trace, domain)
        log = workdir / "episodes" / task["id"]
        log.mkdir(parents=True, exist_ok=True)
        (log / "record.json").write_text(json.dumps(ep, indent=1, ensure_ascii=False))
        extra = {"reward": reward.reward, "r_state": reward.r_state, "r_answer": reward.r_answer,
                 "wm_calls": wm_calls["n"], "package_chars": package_chars, "usage": usage}
        if task["phase"] == "serving":
            evidence.write_readme(evidence_dir, (MWM / "contract" / "env_evidence.md").read_text())
            raw = evidence.write_raw(evidence_dir, ep)
            extra["mwm"] = {"unit": "cases", "number": i, "entry": evidence.entry(ep, raw)}
        return RunOutput(output=ep["submitted"], log=log, wall_s=wall,
                         tokens=(usage["input"] + usage["output"]) or None, extra=extra)
