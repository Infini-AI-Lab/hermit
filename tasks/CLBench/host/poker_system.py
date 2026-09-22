"""CL-Bench exploitable-poker host over a Harness Adapter, evolved by the framework.

The seat (conversation, native tools, per-hand reset, evidence recording) is
the native `metaworld_poker` system's; only the world-model side changes:
the interface is the adapter's exports, and learning is `harnessadapter
evolve` every ``learn_every`` hands, plus one bootstrap session before the
first hand. Loaded by the shim clbench/src/systems/harness_adapter_poker/.
"""

from __future__ import annotations

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
from src.systems.metaworld_poker.system import MetaWorldPokerSystem
from src.systems.utils import native_tools

TASK_ROOT = Path(__file__).resolve().parents[1]
FRAMEWORK_ROOT = TASK_ROOT.parents[1]
if str(TASK_ROOT) not in sys.path:
    sys.path.insert(0, str(TASK_ROOT))
if str(TASK_ROOT / "host") not in sys.path:
    sys.path.insert(0, str(TASK_ROOT / "host"))
from clb import poker_evidence  # noqa: E402
from database_system import _AdapterIface, ARCHITECTURES, _meta_tokens, provider_from_env  # noqa: E402

logger = logging.getLogger(__name__)
DOMAIN = "poker"


@register_system("harness_adapter_poker")
class HarnessAdapterPokerSystem(MetaWorldPokerSystem):

    def __init__(
        self,
        model: str = "fireworks_ai/accounts/fireworks/models/glm-5p3-flash",
        learn_every: int = 5,
        name: str = "harness_adapter_poker",
        workspace: Optional[str] = None,
        architecture: str = "mwm",
        framework: Optional[str] = None,
        evolver_model: str = "accounts/fireworks/models/glm-5p3-flash",
        evolver_backend: str = "codex",
        evolver_timeout: int = 1800,
        evolver_network: bool = False,
        deployment: Optional[str] = None,
        bootstrap: bool = True,
        scenario: str = "poker",
    ):
        if architecture not in ARCHITECTURES:
            raise ValueError(f"architecture must be one of {sorted(ARCHITECTURES)}")
        if workspace is None:
            raise ValueError("harness_adapter_poker needs --system.workspace")
        self.framework = Path(framework) if framework else FRAMEWORK_ROOT
        self.evolve_cli = self.framework / ".venv/bin/harnessadapter"
        if not self.evolve_cli.is_file():
            raise ValueError(f"no installed CLI at {self.evolve_cli}")
        self.architecture = architecture
        arch_dir, self.evolver = ARCHITECTURES[architecture]
        self.evolver_model = evolver_model
        self.evolver_backend = evolver_backend
        self.evolver_timeout = int(evolver_timeout)
        self.evolver_network = bool(evolver_network)
        self.provider = provider_from_env()
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
        placeholder = ws / f"wrepo_{ws.name}"
        placeholder.mkdir(exist_ok=True)
        (placeholder / "README.md").write_text(
            "placeholder: this run's world model is the Harness Adapter in adapter_bNNN/\n")
        super().__init__(model=model, learn_every=learn_every, name=name,
                         workspace=str(ws), meta_backend="none", scenario=scenario)
        self.ws = ws
        self.evidence_dir = ws / "evidence"
        self.evidence_dir.mkdir(exist_ok=True)
        self.sessions_dir = ws / "evolve"
        self.sessions_dir.mkdir(exist_ok=True)
        seed = ws / "adapter_seed"
        if not seed.exists():
            shutil.copytree(self.framework / arch_dir, seed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
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

    # ------------------------------------------------------------- interface

    @property
    def _iface(self):
        if self._iface_obj is None:
            self._iface_obj = _AdapterIface(self.wrepo, self.evidence_dir, self.framework,
                                            deployment=self.deployment_host)
        return self._iface_obj

    @staticmethod
    def _transport_note() -> str:
        return ""

    def _sessions_dir(self) -> Path:
        return self.sessions_dir

    def respond(self, query: Query) -> Response:
        if self._interaction_count == 0:
            self._bootstrap_once()
            self._interaction_count = 0
        # The parent's respond would call the native ensure_bootstrapped on
        # the first interaction; that path needs a metaworld wrepo, so the
        # counter is advanced past it here and the seat logic is reused.
        self._interaction_count += 1
        if query.instance_id != self._current_instance:
            self._end_sub_on_episode_end()
            self._current_instance = query.instance_id
            self._messages = []
            self._delegates_used = 0
            self._wm_calls = 0
            self._pending_call_id = None
        self._seat_messages().append({"role": "user", "content": query.prompt or "(no content)"})
        try:
            turn = self._seat_turn(query, native_tools)
        except Exception as exc:                           # noqa: BLE001
            logger.warning("host call exhausted retries (%s); cooling down 120s", exc)
            time.sleep(120)
            turn = self._seat_turn(query, native_tools)
        self._pending_call_id = turn.pending_call_id
        return Response(action=turn.action, metadata={
            "system_type": "harness_adapter_poker", "architecture": self.architecture,
            "adapter": self.wrepo.name, "model": self.model,
            "interaction_count": self._interaction_count, "hands_done": self._hands_done,
            "block": self._block, "brief_chars": len(self._read_brief()),
            "channel_actions": self._wm_calls})

    # --------------------------------------------------------------- learner

    def _evolve_command(self, block: int, tick: int, goal: str) -> list[str]:
        cmd = [str(self.evolve_cli), "evolve", str(self.wrepo),
               "--evidence", str(self.evidence_dir), "--evolver", self.evolver,
               "--backend", self.evolver_backend, "--model", self.evolver_model,
               "--reasoning-effort", "high", "--output", str(self.sessions_dir),
               "--block", str(block), "--tick", str(tick),
               "--timeout", str(self.evolver_timeout), "--goal", goal]
        if not self.evolver_network:
            cmd.append("--no-network")
        if self.deployment_dir is not None:
            cmd += ["--deployment", str(self.deployment_dir)]
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
                         gates=result.get("gates"), validation=(result.get("validation") or {}).get("passed"),
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

    def _append_report(self, entry: dict) -> None:
        try:
            with open(self.ws / "learner_reports.jsonl", "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass

    def _bootstrap_once(self) -> None:
        if not self.bootstrap_enabled:
            return
        state = json.loads(self._bootstrap_state.read_text()) if self._bootstrap_state.exists() else {}
        if state.get("done"):
            return
        poker_evidence.render([], str(self.evidence_dir), 0)
        for attempt in (1, 2):
            result, entry = self._run_evolve(0, 0, "No hands have been played yet.")
            adopted = self._adopt(result, self.ws / "adapter_b000")
            entry["adopted"] = adopted
            self._learner_reports.append(entry)
            self._append_report(entry)
            if adopted:
                state = {"done": True, "attempt": attempt, "session": result.get("session")}
                break
            logger.warning("bootstrap attempt %s not adopted (%s)", attempt, entry.get("status"))
        if not state.get("done"):
            state = {"done": True, "failed": True}
        self._bootstrap_state.write_text(json.dumps(state, indent=1) + "\n")

    def _read_brief(self) -> str:
        """The host-facing text of a framework adapter is ADAPTER.md, whole
        (2026-09-22); the native reader looked for consumption/brief.md under
        a cap. Statistics only (brief_chars)."""
        try:
            return (self.wrepo / "ADAPTER.md").read_text().strip()
        except OSError:
            return ""

    def _learn(self) -> None:
        self._block += 1
        hands = poker_evidence.group_hands(self._pending)
        rendered = poker_evidence.render(hands, str(self.evidence_dir), self._block)
        first, last = ((hands[0]["hand_num"], hands[-1]["hand_num"]) if hands else (0, 0))
        goal = f"This block covers hands {first}-{last}; the current tick is {self._hands_done}.\n\n{rendered}"
        result, entry = self._run_evolve(self._block, self._hands_done, goal)
        # Native rule (poker/database wrappers): retry ONCE on an infrastructure
        # failure — a session that produced nothing and broke no rule (no broker
        # call succeeded, no timeout). A gate or validation outcome is never retried.
        if (result.get("status") != "candidate_ready" and not result.get("timed_out")
                and not (result.get("broker_calls_succeeded") or 0)):
            logger.warning("block %s learner produced nothing (infrastructure); retrying once", self._block)
            entry["retried_after"] = entry.get("status")
            time.sleep(30)
            result, entry2 = self._run_evolve(self._block, self._hands_done, goal)
            entry2["retried_after"] = entry["retried_after"]
            entry = entry2
        adopted = self._adopt(result, self.ws / f"adapter_b{self._block:03d}")
        entry["adopted"] = adopted
        entry["adapter_out"] = self.wrepo.name
        if not adopted:
            logger.warning("block %s: candidate not adopted (%s)", self._block, entry.get("status"))
        self._notes = self._wrapper_notes(result)
        self._log_cost("meta", self._block, _meta_tokens(result))
        self._learner_reports.append(entry)
        self._append_report(entry)
        self._pending = []

    def get_run_artifacts(self) -> dict[str, Any]:
        return {"artifact_type": "harness_adapter_poker", "architecture": self.architecture,
                "framework": str(self.framework), "evolver": self.evolver, "evolver_model": self.evolver_model,
                "provider": self.provider, "deployment": str(self.deployment_dir) if self.deployment_dir else None,
                "adapters": [p.name for p in self._revisions], "final_adapter": str(self.wrepo),
                "briefing": self._read_brief(), "learner_reports": self._learner_reports, "blocks": self._block}
