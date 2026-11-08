"""ASPIRE's actor on one Robosuite task: write a Code-as-Policy program, run it in the robot execution engine on the
task's development seeds, diagnose from the per-primitive traces and keyframes, repair, and keep the best version
(ASPIRE §2.1, the iterative-debugging loop of NVlabs/ASPIRE .claude/skills/iterative-debugging/subagent-prompt.md);
optionally continue with the evolutionary search over the program (ASPIRE §2.3, Algorithm 1;
.claude/skills/evosearch/); then write the findings the coordinator distills skills from. The judge runs the
selected program on the held-out seeds (utils/judge.py).

The adapter is the shared skill library: its context (ADAPTER.md, and the skills it exports) is the "Shared skill
library" section of the actor's instructions, the only thing that carries from one task to the next. adapter=None
runs without one.

Every model call is one call of the `direct` Base Agent (docs/framework/agent-layers.md); the loop is this file's.
The robot execution engine is ASPIRE's harness in its own venv (sim.py), started in this process: this actor needs
the GPU, so its configs run it on the host (environment.runner: host), a comparison, not the method.

Output: the development rate of the selected program. Log: a compact directory (every program version, each sweep's records and
per-seed digests, the keyframes the model was shown, findings.md, skill_report.json, the transcript); ASPIRE's full
run directories stay under `agent.params.scratch`.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time

from hermit.agent.acting import Agent
from hermit.task import RunOutput

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sim  # noqa: E402
from sim import seeds_of  # noqa: E402,F401

DEFAULT_MODEL = "fireworks/accounts/fireworks/models/glm-5p3-flash"
PROMPTS = HERE / "prompts"
API_SECTIONS_END = "## Host-only adapter and provider interfaces"   # what a generated program never sees
UNAVAILABLE = "acting model unavailable"
# The settings a task row may carry itself (the verifier's rows: its seeds and its attempt budget)
ROW_KEYS = ("debug_seeds", "eval_seeds", "max_versions", "search", "findings", "role")
_UNSERVED = re.compile(r"chat/completions (?:HTTP (?:408|409|429|5\d\d)|failed)")


def program_of(reply: str) -> str | None:
    """The program in a reply: the outermost ```python fence (first opening to last closing), so a program whose own
    text holds a fence is not cut short."""
    start = re.search(r"```(?:python|py)[^\n]*\n", reply)
    if not start:
        return None
    end = reply.rfind("```")
    if end <= start.end():
        return None
    return reply[start.end():end].strip() + "\n"


def _clip_middle(text: str, limit: int) -> str:
    """Keep the start (what the program did first) and the end (how it failed)."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 5
    return text[:head] + f"\n  ... {len(text) - limit} characters omitted ...\n" + text[-(limit - head):]


def json_of(reply: str):
    match = re.search(r"```json[^\n]*\n(.*)```", reply, re.S)
    text = match.group(1) if match else reply[reply.find("{"): reply.rfind("}") + 1]
    try:
        return json.loads(text)
    except ValueError:
        return None


class AspireActor(Agent):
    """`agent.name: ../utils/agent/agent.py:AspireActor`

    params: aspire_root (an ASPIRE checkout with .venv-robosuite), sim_env (the engine's environment), model,
    effort, max_tokens, debug_seeds, eval_seeds (a task row's own seeds win), max_versions, search (debug | evo),
    evo {k, rounds, threshold, qualify_below, parents}, workers, max_steps, failed_shown, keyframes, evidence_chars,
    unserved_retries, sweep_timeout_s, scratch, logs.
    """

    def __init__(self, params=None, base=None):
        super().__init__(params, base)
        self._lock = threading.Lock()
        self._agent = None

    # --- the model ---------------------------------------------------------------------------------------------
    def _base_agent(self):
        with self._lock:
            if self._agent is None:
                from hermit.agent.base.interface import make
                from hermit.agent.model.backends import Binding, registry, resolve
                from hermit.agent.model.llm import ChatClient
                model = self.params.get("model") or DEFAULT_MODEL
                binding = Binding.from_spec(model) if isinstance(model, dict) else resolve(model, registry())
                spec = {**binding.spec(), "extra_body": {"reasoning_effort": self.params.get("effort", "medium")}}
                self._agent = make("direct", {"max_tokens": int(self.params.get("max_tokens", 32000))},
                                   ChatClient.from_spec(spec, max_retries=4, retry_wait=4.0, retry_wait_max=60.0))
            return self._agent

    def _ask(self, run: "_Run", messages: list[dict], stage: str) -> str:
        """One model call; a request the endpoint did not serve (after the client's own retries) is tried again
        after a pause, `unserved_retries` times, before the task is recorded as an error: one outage must not cost a
        task's hours of sweeps."""
        retries = int(self.params.get("unserved_retries", 3))
        for attempt in range(retries + 1):
            try:
                result = self._base_agent().run(messages)
                break
            except RuntimeError as exc:
                if not _UNSERVED.match(str(exc)):
                    raise
                if attempt == retries:
                    raise RuntimeError(f"{UNAVAILABLE}: {exc}") from None
                time.sleep(60.0 * (attempt + 1))
        run.account(result.usage, stage, result.reply, result.reasoning, result.finish_reason)
        return result.reply or ""

    # --- one task ----------------------------------------------------------------------------------------------
    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        started = time.time()
        p = self.params
        root = p.get("aspire_root") or os.environ.get("ASPIRE_ROOT")
        if not root:
            raise ValueError("agent.params.aspire_root (or ASPIRE_ROOT): an ASPIRE checkout")
        engine = sim.Engine(self.path(os.path.expandvars(root)), env=p.get("sim_env"),
                            workers=int(p.get("workers", 4)), max_steps=p.get("max_steps"),
                            timeout_s=float(p.get("sweep_timeout_s", 3600)))
        # A task row's own settings win over agent.params: the verifier's rows carry its seeds and budget.
        p = {**p, **{k: task[k] for k in ROW_KEYS if k in task}}
        debug = seeds_of(p.get("debug_seeds") or "101-125")
        if not debug:
            raise ValueError(f"{task['id']}: no development seeds")
        stamp = time.strftime("%Y%m%dT%H%M%S") + f"-{time.time_ns() % 10**6:06d}"
        log = self.log_dir() / f"{task['id']}-{stamp}"
        scratch = self.path(p.get("scratch") or (self.log_dir() / "scratch")) / f"{task['id']}-{stamp}"
        run = _Run(self, engine, task, log, scratch, debug, self._skills(adapter, task), p)

        run.explore()
        best = run.debug_loop(int(p.get("max_versions", 8)))
        if p.get("search", "debug") == "evo":
            best = run.evolve(best, **(p.get("evo") or {}))
        findings, report = run.findings(best) if p.get("findings", True) else ("", None)
        run.save()

        rate = best.passed / len(debug)       # the development rate; the judge runs the held-out seeds (Stage 2)
        role = "verifier" if p.get("role") == "verifier" else "actor"
        # the facts in the Codex actor's schema (task.py:collect), which the judge and the coordinator read; a
        # verifier's verdict is ASPIRE's rule on its own seeds (>= 4 of 5)
        facts = {"role": role, "suite": task["suite"], "findings": findings, "skill_report": report,
                 "fix_code": best.source, "unseen": None,
                 **({"verification": {"verdict": "verified" if best.passed >= 0.8 * len(debug) else "refuted"}}
                    if role == "verifier" else {})}
        extra = {"task": facts, "selected": best.name, "debug_passed": best.passed, "debug_total": len(debug),
                 "versions": [v.summary() for v in run.versions], "usage": run.totals}
        return RunOutput(output=rate, log=log, wall_s=time.time() - started, cost_usd=run.totals["cost_usd"],
                         tokens=(run.totals["input_tokens"] + run.totals["output_tokens"]) or None,
                         messages=run.transcript_messages(), extra=extra)

    def _skills(self, adapter: Path | None, task: dict) -> str:
        session = self.session(adapter)
        if session is None:
            return "(no skill library)"
        text = session.context(query=task.get("suite"), task=task).strip() or "(the library is empty)"
        staged = Path(adapter) / "skills" / "proposed.md"      # a verifier's proposals under test (not the library)
        if task.get("role") == "verifier" and staged.is_file():
            text += "\n\n## Proposals under test (not yet in the library)\n\n" + staged.read_text()
        return text


class _Version:
    def __init__(self, name: str, source: str, records: list[dict], parent: str | None = None, note: str = ""):
        self.name, self.source, self.records, self.parent, self.note = name, source, records, parent, note

    @property
    def passed(self) -> int:
        return sum(r["task_success"] for r in self.records)

    @property
    def crashed(self) -> int:
        return sum(not r["program_ok"] for r in self.records)

    def failed(self) -> list[dict]:
        return [r for r in self.records if not r["task_success"]]

    def rank(self):
        # ASPIRE Step 3: most passes, then fewer program_ok=false, then the simpler program
        return (-self.passed, self.crashed, len(self.source))

    def summary(self) -> dict:
        return {"name": self.name, "parent": self.parent, "passed": self.passed, "total": len(self.records),
                "crashed": self.crashed, "note": self.note[:300]}


class _Run:
    """The state of one task's debugging: the versions, the transcript, the log directory."""

    def __init__(self, actor: AspireActor, engine: sim.Engine, task: dict, log: Path, scratch: Path,
                 debug: list[int], skills: str, params: dict):
        self.actor, self.engine, self.task, self.log, self.scratch = actor, engine, task, log, scratch
        self.debug, self.skills = debug, skills
        self.suite, self.task_id = task["suite"], int(task.get("task_id", 0))
        self.versions: list[_Version] = []
        self.scene = ""
        self.scene_images: list[dict] = []
        self.transcript: list[dict] = []
        self.totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "cut_off": 0}
        self._lock = threading.Lock()
        self.params = params
        for d in ("code", "sweeps", "keyframes"):
            (log / d).mkdir(parents=True, exist_ok=True)
        api = (Path(engine.root) / "docs" / "api-reference.md").read_text()
        self.api = api.split(API_SECTIONS_END)[0].strip()
        template = (PROMPTS / "actor_system.md").read_text()
        self.system = template.split("-->", 1)[1].strip().replace("{skills}", skills) + \
            "\n\n## API reference (ASPIRE docs/api-reference.md, verbatim)\n\n" + self.api

    # --- bookkeeping ------------------------------------------------------------------------------------------
    def account(self, usage, stage, reply, reasoning, finish_reason):
        with self._lock:
            for u in usage or []:
                self.totals["calls"] += 1
                self.totals["input_tokens"] += u.input_tokens
                self.totals["output_tokens"] += u.output_tokens
                self.totals["cost_usd"] += u.cost_usd or 0.0
            self.totals["cut_off"] += finish_reason == "length"
            self.transcript.append({"stage": stage, "reply": reply, "reasoning_chars": len(reasoning or ""),
                                    "finish_reason": finish_reason})

    def transcript_messages(self) -> list[dict]:
        return [{"role": "assistant", "content": f"[{t['stage']}]\n{t['reply']}"} for t in self.transcript]

    def save(self):
        (self.log / "transcript.jsonl").write_text("".join(json.dumps(t) + "\n" for t in self.transcript))
        (self.log / "CHANGELOG.md").write_text(self.changelog() + "\n")

    def changelog(self) -> str:
        lines = [f"- {v.name}{' (from ' + v.parent + ')' if v.parent else ''}: {v.passed}/{len(v.records)} "
                 f"development seeds, {v.crashed} crashed. {v.note[:200]}" for v in self.versions]
        return "\n".join(lines) or "(no versions yet)"

    # --- the engine ---------------------------------------------------------------------------------------------
    def sweep(self, name: str, source: str, seeds: list[int], *, parent=None, note="") -> _Version:
        code = self.log / "code" / f"{name}.py"
        code.write_text(source)
        refused = self.engine.validate(code)
        if refused:
            records = [{"seed": s, "task_success": False, "program_ok": False, "termination_reason": "rejected",
                        "run_dir": None, "error": f"program rejected before running: {refused}"} for s in seeds]
        else:
            records = self.engine.run(code, suite=self.suite, seeds=seeds, out=self.scratch / name,
                                      task_id=self.task_id)
        out = self.log / "sweeps" / name
        out.mkdir(parents=True, exist_ok=True)
        (out / "records.json").write_text(json.dumps(records, indent=1))
        (out / "digest.txt").write_text("\n\n".join(sim.digest(r) for r in records))
        return _Version(name, source, records, parent, note)

    def evidence(self, version: _Version) -> list[dict]:
        """What the model sees of a sweep: the score, a digest of every failed seed (up to failed_shown), and the
        keyframes of the first few."""
        failed = version.failed()
        shown = failed[: int(self.params.get("failed_shown", 4))]
        text = [f"{version.name}: {version.passed}/{len(version.records)} development seeds passed; "
                f"failed seeds: {[r['seed'] for r in failed] or 'none'}"]
        parts: list[dict] = []
        budget = int(self.params.get("evidence_chars", 40000))   # per version shown: the digests share it
        for record in shown:
            text.append(_clip_middle(sim.digest(record), max(2000, budget // max(1, len(shown)))))
        n_frames = int(self.params.get("keyframes", 2))
        for record in shown[:2]:
            for label, png in sim.keyframes(record.get("run_dir"), limit=n_frames):
                kept = self.log / "keyframes" / version.name / f"seed{record['seed']}-{label}.png"
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(png, kept)
                parts.append({"type": "text", "text": f"keyframe {version.name} seed {record['seed']} {label}:"})
                parts.append(sim.image_part(kept))
        return [{"type": "text", "text": "\n\n".join(text)}, *parts]

    # --- Stage 0: explore once ----------------------------------------------------------------------------------
    def explore(self):
        probe = (Path(self.engine.root) / ".claude/skills/iterative-debugging/scripts/scene_snapshot.py").read_text()
        version = self.sweep("probe", probe, self.debug[:1])
        record = version.records[0]
        self.scene = sim.digest(record, call_chars=1500)
        for camera in ("agentview", "robot0_eye_in_hand"):
            for label, png in sim.keyframes(record.get("run_dir"), camera=camera, limit=1):
                kept = self.log / "keyframes" / "probe" / f"{camera}-{label}.png"
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(png, kept)
                self.scene_images += [{"type": "text", "text": f"initial scene, camera {camera}:"},
                                      sim.image_part(kept)]

    def _task_text(self) -> str:
        return (f"Task: robosuite suite `{self.suite}`, task id {self.task_id}. Development seeds: {self.debug}.\n\n"
                f"A scene-snapshot program ran once on seed {self.debug[0]}; its trace:\n\n{self.scene}")

    # --- Stage 1: the iterative-debugging loop -------------------------------------------------------------------
    def debug_loop(self, max_versions: int) -> _Version:
        best: _Version | None = None
        last: _Version | None = None
        for n in range(max_versions):
            content = [{"type": "text", "text": self._task_text()}, *self.scene_images]
            if last is None:
                content.append({"type": "text", "text": "Write the initial program (v00) for this task."})
            else:
                content += [{"type": "text", "text": f"Versions so far:\n{self.changelog()}"}]
                if best is not last:
                    content += [{"type": "text", "text": f"The best version so far, {best.name}:\n"
                                                         f"```python\n{best.source}```"}]
                content += [{"type": "text", "text": f"The last version, {last.name}:\n```python\n{last.source}```"},
                            *self.evidence(last),
                            {"type": "text", "text": f"Write v{n:02d}: fix the largest failure mode; keep what "
                                                     f"works. Budget: {max_versions - n} versions left."}]
            reply = self.actor._ask(self, [{"role": "system", "content": self.system},
                                           {"role": "user", "content": content}], f"v{n:02d}")
            source = program_of(reply)
            if source is None:
                self.transcript[-1]["note"] = "no program in the reply"
                continue
            note = reply[: reply.find("```")].strip().replace("\n", " ")
            last = self.sweep(f"v{n:02d}", source, self.debug, parent=best.name if best else None, note=note)
            self.versions.append(last)
            if best is None or last.rank() < best.rank():
                best = last
            if best.passed == len(self.debug):
                break
        if best is None:
            # ASPIRE Step 3: if every version failed to produce a program, a minimal legal one still runs
            best = self.sweep("v_minimal", "get_observation()\nresult = {}\n", self.debug, note="minimal program")
            self.versions.append(best)
        return best

    # --- the evolutionary search (Algorithm 1) -------------------------------------------------------------------
    def evolve(self, start: _Version, k: int = 8, rounds: int = 5, threshold: float = 0.8,
               qualify_below: float = 0.9, parents: int = 3) -> _Version:
        """Propose `k` programs per round conditioned on the top `parents` programs so far and their residual
        failures; run each on the development seeds; stop when the best reaches `threshold` of them or after
        `rounds` rounds. Tasks whose fix-loop rate is already `qualify_below` or higher skip the search."""
        total = len(self.debug)
        if start.passed >= qualify_below * total:
            return start
        pool = list(self.versions)
        best = start
        for r in range(1, int(rounds) + 1):
            top = sorted(pool, key=_Version.rank)[: int(parents)]
            content = [{"type": "text", "text": self._task_text()}, *self.scene_images,
                       {"type": "text", "text": f"History:\n{self.changelog()}"}]
            for v in top:
                content += [{"type": "text", "text": f"Program {v.name} ({v.passed}/{len(v.records)}):\n"
                                                     f"```python\n{v.source}```"}, *self.evidence(v)]
            content.append({"type": "text", "text": "Write one new candidate program that improves on these: "
                                                    "combine what works, fix the residual failures, and try a "
                                                    "different strategy where they all fail the same way."})
            messages = [{"role": "system", "content": self.system}, {"role": "user", "content": content}]
            with ThreadPoolExecutor(max_workers=int(k)) as pool_ex:
                replies = list(pool_ex.map(lambda i: self.actor._ask(self, messages, f"evo{r:02d}{chr(65 + i)}"),
                                           range(int(k))))
            for i, reply in enumerate(replies):
                source = program_of(reply)
                if source is None:
                    continue
                note = reply[: reply.find("```")].strip().replace("\n", " ")
                cand = self.sweep(f"evo{r:02d}_{chr(65 + i)}", source, self.debug,
                                  parent=",".join(v.name for v in top), note=note)
                self.versions.append(cand)
                pool.append(cand)
                if cand.rank() < best.rank():
                    best = cand
            if best.passed >= threshold * total:
                break
        return best

    # --- findings for the coordinator ------------------------------------------------------------------------------
    def findings(self, best: _Version):
        template = (PROMPTS / "findings.md").read_text().split("-->", 1)[1].strip()
        history = self.changelog() + "\n\n" + "\n\n".join(
            f"{v.name} failures:\n" + "\n".join(sim.digest(r, call_chars=200) for r in v.failed()[:2])
            for v in self.versions if v.failed())
        prompt = template.format(selected=best.name, debug_passed=best.passed, debug_total=len(best.records),
                                 suite=self.suite, history=history[-60000:], program=best.source)
        reply = self.actor._ask(self, [{"role": "system", "content": self.system},
                                       {"role": "user", "content": prompt}], "findings")
        report = json_of(reply[reply.rfind("```json"):] if "```json" in reply else reply)
        findings = reply[: reply.rfind("```json")].strip() if "```json" in reply else reply.strip()
        (self.log / "findings.md").write_text(findings + "\n")
        (self.log / "skill_report.json").write_text(json.dumps(report, indent=1) if report else "null")
        return findings, report
