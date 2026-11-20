"""A task-local Harbor runner: inject frozen user assignment and reject invalid trials."""

from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import threading
import json
import os
from pathlib import Path
import shutil
import uuid

from hermit.harbor.runner import HarborAgent
from hermit.task import RunOutput, RunUnavailable
from binding import fingerprint, read, write


class AheHarborAgent(HarborAgent):
    def __init__(self, params=None, base=None):
        super().__init__(params, base)
        self._binding = threading.local()
        self._binding_lock = threading.Lock()

    def agent_config(self, task, staging, deployed):
        config = super().agent_config(task, staging, deployed)
        spec = copy.deepcopy(config.setdefault("kwargs", {}).get("user") or {})
        spec.setdefault("simulate", "ahe_beta.integrations.hermit.session:simulate")
        spec["params"] = {
            **spec.get("params", {}),
            **task["ahe_session"],
            "session_id": task["id"] + "-" + uuid.uuid4().hex[:12],
            **self._binding.value,
        }
        config["kwargs"]["user"] = spec
        return config

    def run(self, task, adapter):
        try:
            return self._run(task, adapter)
        except RunUnavailable:
            raise
        except Exception as exc:
            raise RunUnavailable(
                f"AHE rollout {task['id']} unavailable: {type(exc).__name__}: {exc}"
            ) from exc

    def _run(self, task, adapter):
        # A completed rollout is reusable only for the same frozen assignment, adapter,
        # endpoint/model and runner settings. Credentials are deliberately not cache keys.
        binding_path = self.path(self.params["binding"])
        if not binding_path.exists():
            delivery_repo = self.params.get("delivery_repo")
            run_id = self.params.get("run_id")
            if not delivery_repo or not run_id:
                raise RunUnavailable(
                    "the first rollout needs delivery_repo and run_id to create its binding"
                )
            with self._binding_lock:
                if not binding_path.exists():
                    write(
                        binding_path,
                        adapter=adapter,
                        repo=self.path(delivery_repo),
                        run_id=str(run_id),
                        round_no=int(task["ahe_session"]["round"]),
                    )
        run_id = json.loads(binding_path.read_text())["run_id"]
        key = hashlib.sha256(
            json.dumps(
                {
                    "task": task,
                    "adapter": fingerprint(adapter),
                    "params": {
                        key: value
                        for key, value in self.params.items()
                        if key not in {"delivery_repo", "run_id"}
                    },
                    "run_id": run_id,
                    "user_model": {
                        k: os.environ.get(k)
                        for k in (
                            "USER_LLM_MODEL",
                            "USER_LLM_BASE_URL",
                            "USER_LLM_MAX_TOKENS",
                            "USER_LLM_REASONING_EFFORT",
                            "USER_LLM_MAX_CONTEXT_TOKENS",
                        )
                    },
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        cached = self.jobs_dir / "completed" / (key + ".json")
        if cached.exists():
            saved = json.loads(cached.read_text())
            log = Path(saved["log"])
            if log.is_dir() and (log / "ahe-session").is_dir():
                return RunOutput(**saved)
            raise RunUnavailable("cached successful rollout lost its native evidence")
        self._binding.value = read(binding_path, adapter)
        if self._binding.value["round"] != task["ahe_session"]["round"]:
            raise RunUnavailable("rollout is not in the current frozen cohort")
        result = super().run(task, adapter)
        output = result.output if isinstance(result.output, dict) else {}
        # Keep native AHE snapshots/binary exports alongside Hermit's curated text logs.
        if result.log:
            log = Path(result.log)
            job = log.parent
            for source in job.glob("*/agent/ahe-session"):
                shutil.copytree(source, log / "ahe-session", dirs_exist_ok=True)
        verdict_file = (
            Path(result.log) / "agent/user-verdict.json" if result.log else None
        )
        verdict = (
            json.loads(verdict_file.read_text())
            if verdict_file and verdict_file.exists()
            else {}
        )
        if (
            output.get("status") != "ok"
            or verdict.get("error")
            or verdict.get("score") is None
        ):
            raise RunUnavailable(
                f"AHE rollout {task['id']} is unscored; inspect {result.log}"
            )
        user_usage = verdict.get("usage", {})
        actor_tokens = result.tokens
        result.extra["actor_tokens"] = actor_tokens
        result.extra["user_usage"] = user_usage
        result.tokens = (
            actor_tokens + user_usage.get("total_tokens", 0)
            if actor_tokens is not None and not user_usage.get("unavailable_count")
            else None
        )
        # Harbor estimates only Actor cost. User-call prices are not supplied here.
        result.cost_usd = None
        cached.parent.mkdir(parents=True, exist_ok=True)
        temporary = cached.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(result), default=str))
        temporary.replace(cached)
        return result
