"""CL-Bench database host over a Harness Adapter, evolved by the framework.

Loaded into the clbench process by a thin registration shim
(clbench/src/systems/harness_adapter/__init__.py), so the venue code lives
with the task, not in the framework package and not in the benchmark tree.

Two roles, as in the native metaworld system:

* Host: a fresh, bare API call per question. Its system prompt is the
  adapter's Context export (the MWM five-section package for the mwm
  architecture); its native tools are the adapter's declared tools. The
  framework loader calls the adapter in-process.
* Learner: `harnessadapter evolve` after every ``learn_every`` questions
  (and once as a bootstrap before the first question), with this run's
  evidence read-only, the venue's deployment texts, a read-only `sql`
  binding to the current instance database, and an explicit model
  provider (GLM flash through the wire meter). The candidate directory,
  with its git history, becomes the adapter the next questions are served
  from; a session that is not `candidate_ready` (gate findings, timeout,
  broken export) leaves the previous adapter serving.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from src.interface import Query, Response
from src.registry import register_system
from src.systems.metaworld.system import MetaWorldV6System
from src.systems.utils import native_tools

logger = logging.getLogger(__name__)

TASK_ROOT = Path(__file__).resolve().parents[1]          # tasks/CLBench
FRAMEWORK_ROOT = TASK_ROOT.parents[1]                      # the framework checkout
if str(TASK_ROOT) not in sys.path:
    sys.path.insert(0, str(TASK_ROOT))
from clb import clbench_evidence  # noqa: E402

ARCHITECTURES = {"mwm": ("harnessadapter/architectures/mwm", "mwm"),
                 "prompt-only": ("harnessadapter/architectures/prompt_only", "basic")}
DOMAIN = "clbench"


def _meta_tokens(result: dict) -> dict:
    """The learner's token usage for one session, as the framework sums it from
    the backend's turn events (session result `usage`); zeros when a session
    died before its first turn completed."""
    u = (result or {}).get("usage") or {}
    return {"input": int(u.get("input") or 0), "cached": int(u.get("cached") or 0),
            "output": int(u.get("output") or 0)}


def provider_from_env() -> Optional[dict]:
    """The learner's model provider, from the launcher's environment:
    HA_PROVIDER_NAME / HA_PROVIDER_BASE_URL / HA_PROVIDER_ENV_KEY
    (+ HA_PROVIDER_WIRE_API, HA_PROVIDER_CONTEXT). Unset = the Codex login."""
    name = os.environ.get("HA_PROVIDER_NAME")
    if not name:
        return None
    return {"name": name, "base_url": os.environ["HA_PROVIDER_BASE_URL"],
            "env_key": os.environ["HA_PROVIDER_ENV_KEY"],
            "wire_api": os.environ.get("HA_PROVIDER_WIRE_API", "responses")}


class _AdapterIface:
    """The seat's view of a Harness Adapter: the interface surface mwm.seat
    expects (host_tool_specs / host_tool_names / resolve / call / package),
    served by the framework's Adapter loader."""

    delegation = "none"
    delegate = None

    def __init__(self, root: Path, evidence_dir: Path, framework: Path, deployment: Optional[dict] = None):
        fw = str(framework)
        if fw not in sys.path:
            sys.path.insert(0, fw)
        from harnessadapter.adapter import Adapter, EvidenceStore   # noqa: E402
        evidence_dir.mkdir(parents=True, exist_ok=True)
        resources = {"evidence": EvidenceStore(evidence_dir)}
        if deployment:
            resources["deployment"] = dict(deployment)
        self.adapter = Adapter(root, resources)
        self.tools = list(self.adapter.manifest.get("tools", []))

    def host_tool_specs(self) -> list:
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in self.tools]

    def host_tool_names(self) -> set:
        return {t["name"] for t in self.tools}

    def resolve(self, name: str, args) -> tuple:
        args = args if isinstance(args, dict) else {}
        if name == "wm":
            inner = args.get("args")
            return str(args.get("name", "")), (inner if isinstance(inner, dict) else {})
        return name, args

    def call(self, wname: str, wargs: Optional[dict] = None, transport_note=None) -> str:
        if not any(t["name"] == "wm" for t in self.tools):
            return "no world-model tool is declared by this adapter"
        result = asyncio.run(self.adapter.call("wm", {"name": wname, "args": wargs or {}}))
        if isinstance(result, dict) and "text" in result:
            text = result["text"]
            # A paged read: the host gets the page and the way to the next one.
            if result.get("eof") is False and result.get("next_offset") is not None:
                followup = {"path": result.get("path"), "offset": result["next_offset"],
                            "limit": result.get("limit", 4000)}
                if result.get("sha256"):
                    followup["expected_sha256"] = result["sha256"]
                text += ('\n[More text available; continue when useful: '
                         'wm(name="read", args=' + json.dumps(followup) + ')]')
            return text
        return json.dumps(result, ensure_ascii=False)

    def package(self, transport_note: str = "", with_delegate: bool = True) -> str:
        return self.adapter.context()


@register_system("harness_adapter")
class HarnessAdapterSystem(MetaWorldV6System):
    """Fresh-context hosts over a Harness Adapter; the framework evolves it."""

    supports_baseline = False
    parallel_safe = False

    def __init__(
        self,
        model: str = "fireworks_ai/accounts/fireworks/models/glm-5p3-flash",
        learn_every: int = 1,
        name: str = "harness_adapter",
        workspace: Optional[str] = None,
        architecture: str = "mwm",
        framework: Optional[str] = None,
        evolver_model: str = "accounts/fireworks/models/glm-5p3-flash",
        evolver_backend: str = "codex",
        evolver_timeout: int = 1800,
        evolver_network: bool = False,
        deployment: Optional[str] = None,
        sql_url: Optional[str] = None,
        bootstrap: bool = True,
        scenario: str = "database_exploration",
    ):
        if architecture not in ARCHITECTURES:
            raise ValueError(f"architecture must be one of {sorted(ARCHITECTURES)}")
        if workspace is None:
            raise ValueError("harness_adapter needs --system.workspace")
        self.framework = Path(framework) if framework else FRAMEWORK_ROOT
        self.evolve_cli = self.framework / ".venv/bin/harnessadapter"
        if not (self.framework / "harnessadapter/cli.py").is_file():
            raise ValueError(f"no harnessadapter package at {self.framework}")
        if not self.evolve_cli.is_file():
            raise ValueError(f"no installed CLI at {self.evolve_cli} (pip install -e . in the framework checkout)")
        self.architecture = architecture
        arch_dir, self.evolver = ARCHITECTURES[architecture]
        self.evolver_model = evolver_model
        self.evolver_backend = evolver_backend
        self.evolver_timeout = int(evolver_timeout)
        self.evolver_network = bool(evolver_network)
        self.provider = provider_from_env()
        self.sql_url = sql_url
        self.bootstrap_enabled = bool(bootstrap) and architecture == "mwm"
        self.deployment_dir = Path(deployment) if deployment else (
            TASK_ROOT / "deployments" / DOMAIN if architecture == "mwm" else None)
        if self.deployment_dir is not None and not self.deployment_dir.is_dir():
            raise ValueError(f"deployment directory missing: {self.deployment_dir}")
        self.deployment_host: dict = {}
        if self.deployment_dir is not None and (self.deployment_dir / "host.json").is_file():
            self.deployment_host = json.loads((self.deployment_dir / "host.json").read_text())

        ws = Path(workspace)
        ws.mkdir(parents=True, exist_ok=True)
        # The parent initialises a metaworld wrepo when its directory is
        # missing; an empty placeholder makes it skip that.
        placeholder = ws / f"wrepo_{ws.name}"
        placeholder.mkdir(exist_ok=True)
        (placeholder / "README.md").write_text(
            "placeholder: this run's world model is the Harness Adapter in "
            "adapter_bNNN/, not a metaworld wrepo\n")
        super().__init__(model=model, learn_every=learn_every, name=name,
                         workspace=str(ws), meta_backend="none", scenario=scenario)
        self.ws = ws
        self.evidence_dir = ws / "evidence"
        self.evidence_dir.mkdir(exist_ok=True)
        self.sessions_dir = ws / "evolve"
        self.sessions_dir.mkdir(exist_ok=True)
        seed = ws / "adapter_seed"
        if not seed.exists():
            shutil.copytree(self.framework / arch_dir, seed,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            if str(self.framework) not in sys.path:
                sys.path.insert(0, str(self.framework))
            from harnessadapter.adapter import versioning   # noqa: E402
            versioning.ensure_repo(seed, "adapter: architecture seed")
        self.wrepo = seed
        self._revisions = [seed]
        self._notes = ""
        self._bootstrap_state = ws / "bootstrap.json"
        if (ws / "adapter_b000").exists():
            self.wrepo = ws / "adapter_b000"
            self._revisions.append(self.wrepo)

    # ------------------------------------------------------------------ host

    def respond(self, query: Query) -> Response:
        if self._interaction_count == 0:
            self._register_db(query)
            self._bootstrap_once()
        self._interaction_count += 1
        self._register_db(query)
        if query.instance_id != self._current_instance:
            self._end_sub_on_episode_end()
            self._current_instance = query.instance_id
            self._messages = []
            self._delegates_used = 0
            self._wm_calls = 0
            self._pending_call_id = None
        prompt = query.prompt if query.prompt else "(no content)"
        self._seat_messages().append({"role": "user", "content": prompt})
        try:
            turn = self._seat_turn(query, native_tools)
        except Exception as exc:                           # noqa: BLE001
            logger.warning("host call exhausted retries (%s); cooling "
                           "down 120s for one final attempt", exc)
            time.sleep(120)
            turn = self._seat_turn(query, native_tools)
        self._pending_call_id = turn.pending_call_id
        return Response(
            action=turn.action,
            metadata={
                "system_type": "harness_adapter",
                "architecture": self.architecture,
                "adapter": self.wrepo.name,
                "model": self.model,
                "interaction_count": self._interaction_count,
                "questions_done": self._questions_done,
                "block": self._block,
                "brief_chars": len(self._read_brief()),
                "channel_actions": self._wm_calls,
            },
        )

    @property
    def _iface(self):
        if self._iface_obj is None:
            self._iface_obj = _AdapterIface(self.wrepo, self.evidence_dir, self.framework,
                                            deployment=self.deployment_host)
        return self._iface_obj

    @staticmethod
    def _transport_note() -> str:
        return ""        # the adapter's Context carries its own access text

    # --------------------------------------------------------------- evolver

    def _evolve_command(self, block: int, tick: int, goal: str) -> list[str]:
        cmd = [str(self.evolve_cli), "evolve", str(self.wrepo),
               "--evidence", str(self.evidence_dir), "--evolver", self.evolver,
               "--backend", self.evolver_backend, "--model", self.evolver_model,
               "--reasoning-effort", "high",
               "--output", str(self.sessions_dir),
               "--block", str(block), "--tick", str(tick),
               "--timeout", str(self.evolver_timeout), "--goal", goal]
        if not self.evolver_network:
            cmd.append("--no-network")
        if self.deployment_dir is not None:
            cmd += ["--deployment", str(self.deployment_dir)]
        if self.sql_url:
            cmd += ["--bind", f"sql={self.sql_url}"]
        if self._notes:
            cmd += ["--notes", self._notes]
        if self.provider:
            cmd += ["--provider-name", self.provider["name"], "--provider-base-url", self.provider["base_url"],
                    "--provider-env-key", self.provider["env_key"], "--provider-wire-api", self.provider["wire_api"]]
        return cmd

    def _run_evolve(self, block: int, tick: int, goal: str) -> tuple[dict, dict]:
        cmd = self._evolve_command(block, tick, goal)
        t0 = time.time()
        entry: dict[str, Any] = {"block": block, "adapter_in": self.wrepo.name, "argv_model": self.evolver_model}
        result: dict = {}
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.evolver_timeout + 300, cwd=str(self.framework))
            out = proc.stdout
            i = out.find("{")
            result = json.loads(out[i:]) if i >= 0 else {}
            entry.update(status=result.get("status", "no_result"), exit_code=proc.returncode,
                         session=result.get("session"), changes=result.get("changes"),
                         unchanged=result.get("unchanged"), gates=result.get("gates"),
                         validation=(result.get("validation") or {}).get("passed"),
                         timed_out=result.get("timed_out"), candidate_commit=result.get("candidate_commit"),
                         broker_calls=result.get("broker_calls"),
                         stderr_tail=proc.stderr[-800:] if proc.returncode else "")
        except subprocess.TimeoutExpired:
            entry.update(status="evolve_timeout", exit_code=None)
        except Exception as exc:                            # noqa: BLE001
            entry.update(status="evolve_error", error=str(exc)[:800])
        entry["wall_s"] = round(time.time() - t0, 1)
        return result, entry

    def _adopt(self, result: dict, revision: Path) -> bool:
        candidate = result.get("candidate")
        if not (result.get("status") == "candidate_ready" and candidate and Path(candidate).is_dir()):
            return False
        if revision.exists():
            shutil.rmtree(revision)
        # The candidate carries its git history (framework-committed).
        shutil.copytree(candidate, revision, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.wrepo = revision
        self._revisions.append(revision)
        self._iface_obj = None
        return True

    def _wrapper_notes(self, result: dict) -> str:
        parked = [t for t in ((result.get("admission") or {}).get("tools") or []) if t.get("parked_at")]
        if not parked:
            return ""
        return ("Executables parked by the wrapper (their own test failed; already moved to _rejected/):\n"
                + "\n".join(f"- {t['tool']}: {str(t.get('output', ''))[-300:]}" for t in parked))

    def _bootstrap_once(self) -> None:
        """The native rule: one exploration session before any episode,
        retried once on failure, then the run continues without it."""
        if not self.bootstrap_enabled:
            return
        state = json.loads(self._bootstrap_state.read_text()) if self._bootstrap_state.exists() else {}
        if state.get("done"):
            return
        # The learner reads the environment README and the deployment texts;
        # evidence is still empty at this point, so the store holds only its README.
        clbench_evidence.render([], str(self.evidence_dir), 0)
        goal = "No episodes have been played yet."
        for attempt in (1, 2):
            result, entry = self._run_evolve(0, 0, goal)
            adopted = self._adopt(result, self.ws / "adapter_b000")
            entry["adopted"] = adopted
            self._learner_reports.append(entry)
            self._append_report(entry)
            if adopted:
                state = {"done": True, "attempt": attempt, "session": result.get("session")}
                break
            logger.warning("bootstrap attempt %s not adopted (%s)", attempt, entry.get("status"))
        if not state.get("done"):
            # Continue from the seed, as the native wrapper does after two failures.
            state = {"done": True, "failed": True}
        self._bootstrap_state.write_text(json.dumps(state, indent=1) + "\n")

    def _append_report(self, entry: dict) -> None:
        try:
            with open(self.ws / "learner_reports.jsonl", "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass

    def _learn(self) -> None:
        self._block += 1
        qs = clbench_evidence.group_questions(self._pending)
        evidence = clbench_evidence.render(qs, str(self.evidence_dir), self._block)
        first, last = ((qs[0]["question_num"], qs[-1]["question_num"]) if qs else (0, 0))
        goal = (f"This block covers questions {first}-{last}; the current tick is "
                f"{self._questions_done}.\n\n{evidence}")
        result, entry = self._run_evolve(self._block, self._questions_done, goal)
        # Native rule (poker/database wrappers): retry ONCE on an infrastructure
        # failure — a session that produced nothing and broke no rule (no broker
        # call succeeded, no timeout). A gate or validation outcome is never retried.
        if (result.get("status") != "candidate_ready" and not result.get("timed_out")
                and not (result.get("broker_calls_succeeded") or 0)):
            logger.warning("block %s learner produced nothing (infrastructure); retrying once", self._block)
            entry["retried_after"] = entry.get("status")
            time.sleep(30)
            result, entry2 = self._run_evolve(self._block, self._questions_done, goal)
            entry2["retried_after"] = entry["retried_after"]
            entry = entry2
        adopted = self._adopt(result, self.ws / f"adapter_b{self._block:03d}")
        entry["adopted"] = adopted
        entry["adapter_out"] = self.wrepo.name
        if not adopted:
            logger.warning("block %s: candidate not adopted (%s)", self._block, entry.get("status"))
        self._notes = self._wrapper_notes(result)
        self._log_cost(role="meta", round_no=self._block, tokens=_meta_tokens(result))
        self._learner_reports.append(entry)
        self._append_report(entry)
        self._pending = []

    # ------------------------------------------------------------ lifecycle

    def get_run_artifacts(self) -> dict[str, Any]:
        return {
            "artifact_type": "harness_adapter",
            "architecture": self.architecture,
            "framework": str(self.framework),
            "evolver": self.evolver,
            "evolver_model": self.evolver_model,
            "provider": self.provider,
            "deployment": str(self.deployment_dir) if self.deployment_dir else None,
            "sql_url": self.sql_url,
            "adapters": [p.name for p in self._revisions],
            "final_adapter": str(self.wrepo),
            "evidence_dir": str(self.evidence_dir),
            "questions_done": self._questions_done,
            "blocks": self._block,
            "learner_reports": self._learner_reports,
        }
