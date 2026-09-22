"""The learner: one framework evolve session per block, the same pipeline as
the CLBench hosts (bootstrap before the first question, retry once on an
infrastructure failure, adopt only candidate_ready). Nothing here calls a
model directly; the framework's codex learner does, through the meter."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from paths import REPOSITORY, DEPLOYMENT

EVOLVE = REPOSITORY / ".venv/bin/harnessadapter"
EVOLVERS = {"mwm": "mwm", "prompt-only": "basic"}
ARCHITECTURES = {"mwm": REPOSITORY / "harnessadapter/architectures/mwm",
                 "prompt-only": REPOSITORY / "harnessadapter/architectures/prompt_only"}


def provider_from_env() -> dict | None:
    name = os.environ.get("HA_PROVIDER_NAME")
    if not name:
        return None
    return {"name": name, "base_url": os.environ["HA_PROVIDER_BASE_URL"],
            "env_key": os.environ.get("HA_PROVIDER_ENV_KEY", "HA_FW_KEY"),
            "wire_api": os.environ.get("HA_PROVIDER_WIRE_API", "responses")}


class Learner:
    def __init__(self, run: Path, arm: str, evidence_dir: Path, *, model: str, timeout: int = 1800, mounts: dict | None = None):
        self.run, self.arm = Path(run), arm
        self.mounts = dict(mounts or {})      # /environment/<name> -> host path, read-only
        self.evidence_dir = Path(evidence_dir)
        self.model, self.timeout = model, int(timeout)
        self.sessions = self.run / "evolve"
        self.sessions.mkdir(exist_ok=True)
        self.provider = provider_from_env()
        # A run-local copy of the deployment: the environment README names the
        # corpus by the path the hosts use, the same path the learner mounts.
        self.deployment = self.run / "deployment"
        if not self.deployment.exists():
            shutil.copytree(DEPLOYMENT, self.deployment)
            readme = self.deployment / "environment" / "README.md"
            text = readme.read_text()
            for name, target in self.mounts.items():
                text = text.replace("{" + name + "}", str(Path(target).resolve()))
            readme.write_text(text)
        seed = self.run / "adapter_seed"
        if not seed.exists():
            shutil.copytree(ARCHITECTURES[arm], seed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # Resume: the latest adopted revision serves; earlier ones are history.
        self.revisions: list[Path] = sorted(self.run.glob("adapter_b[0-9][0-9][0-9]"))
        self.adapter = self.revisions[-1] if self.revisions else seed
        self.notes = ""
        self.reports = self.run / "learner_reports.jsonl"

    def command(self, block: int, tick: int, goal: str) -> list[str]:
        cmd = [str(EVOLVE), "evolve", str(self.adapter), "--evidence", str(self.evidence_dir),
               "--evolver", EVOLVERS[self.arm], "--backend", "codex", "--model", self.model,
               "--reasoning-effort", "high", "--output", str(self.sessions),
               "--block", str(block), "--tick", str(tick), "--timeout", str(self.timeout),
               "--goal", goal, "--no-network", "--deployment", str(self.deployment)]
        for name, target in self.mounts.items():
            cmd += ["--mount", f"{name}={target}"]
        if self.notes:
            cmd += ["--notes", self.notes]
        if self.provider:
            cmd += ["--provider-name", self.provider["name"], "--provider-base-url", self.provider["base_url"],
                    "--provider-env-key", self.provider["env_key"], "--provider-wire-api", self.provider["wire_api"]]
        return cmd

    def _run(self, block: int, tick: int, goal: str) -> tuple[dict, dict]:
        t0 = time.time()
        entry: dict = {"block": block, "adapter_in": self.adapter.name, "model": self.model}
        result: dict = {}
        try:
            proc = subprocess.run(self.command(block, tick, goal), capture_output=True, text=True,
                                  timeout=self.timeout + 300, cwd=str(REPOSITORY))
            out = proc.stdout
            i = out.find("{")
            result = json.loads(out[i:]) if i >= 0 else {}
            entry.update(status=result.get("status", "no_result"), exit_code=proc.returncode,
                         session=result.get("session"), changes=result.get("changes"),
                         unchanged=result.get("unchanged"), gates=result.get("gates"),
                         usage=result.get("usage"), broker_calls=result.get("broker_calls_succeeded"),
                         stderr_tail=proc.stderr[-800:] if proc.returncode else "")
        except subprocess.TimeoutExpired:
            entry.update(status="evolve_timeout")
        except Exception as exc:                            # noqa: BLE001
            entry.update(status="evolve_error", error=str(exc)[:800])
        entry["wall_s"] = round(time.time() - t0, 1)
        return result, entry

    def session(self, block: int, tick: int, goal: str) -> dict:
        result, entry = self._run(block, tick, goal)
        if (result.get("status") != "candidate_ready" and not result.get("timed_out")
                and not (result.get("broker_calls_succeeded") or 0)):
            # Native rule: an infrastructure failure (nothing done, no rule broken) is retried once.
            entry["retried_after"] = entry.get("status")
            time.sleep(30)
            result, entry2 = self._run(block, tick, goal)
            entry2["retried_after"] = entry["retried_after"]
            entry = entry2
        candidate = result.get("candidate")
        adopted = bool(result.get("status") == "candidate_ready" and candidate and Path(candidate).is_dir())
        if adopted:
            revision = self.run / f"adapter_b{block:03d}"
            if revision.exists():
                shutil.rmtree(revision)
            shutil.copytree(candidate, revision, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            self.adapter = revision
            self.revisions.append(revision)
        entry.update(adopted=adopted, adapter_out=self.adapter.name)
        parked = [t for t in ((result.get("admission") or {}).get("tools") or []) if t.get("parked_at")]
        self.notes = ("Executables parked by the wrapper (their own test failed; already moved to _rejected/):\n"
                      + "\n".join(f"- {t['tool']}: {str(t.get('output', ''))[-300:]}" for t in parked)) if parked else ""
        with self.reports.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def bootstrap(self) -> dict:
        marker = self.run / "bootstrap.json"
        if marker.exists():
            return json.loads(marker.read_text())
        entry = self.session(0, 0, "No episodes have been played yet.")
        marker.write_text(json.dumps({"done": True, "attempt": 1, "session": entry.get("session")}) + "\n")
        return entry
