"""AHE-Beta's own demand and delivery pipeline as an Evolver, against a dedicated adapter repository (../README.md:
"AHE-Beta's own Evolver"). Unvalidated beyond scripted runs: the validated route is Hermit's Codex Evolver
(../prepare.py's run.yaml).

A trusted supervisor on this machine: it runs AHE-Beta's controller (its own checkout and Python) on a provisioned
Gitea repository and OpenAgents worker; neither credentials nor delivery tools enter a task's environment. It reads
each session's native records where the acting agent kept them (the user's private folder, user.py), which AHE-Beta's
requirement roles read by design, and a round's rows from the cohort's assignments.json. A step that cannot deliver
raises: the Trainer records it as failed and keeps the adapter.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from hermit.adapter.manifest import files_of, read_manifest, structure_of
from hermit.agent.evolver.base import Evolver, StepResult
from hermit.agent.model.usage import Usage

EXAMPLE = Path(__file__).resolve().parents[1]           # examples/ahe_beta: its helpers, beside this Evolver
for folder in (EXAMPLE / "utils", EXAMPLE):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
from binding import git, write  # noqa: E402
from openagents_runtime import OpenAgentsSpec, ensure_checkout  # noqa: E402


class RunUnavailable(RuntimeError):
    """AHE-Beta's delivery could not run (its configuration, its checkout, its controller): the step fails and the
    adapter stays as it was; the native receipts stay for the next attempt."""


class AHEBetaEvolver(Evolver):
    def config(self, ctx):
        path = Path(ctx.params["delivery_config"]).expanduser()
        if not path.is_absolute():
            path = ctx.base / path
        cfg = json.loads(path.read_text())
        frozen = Path(cfg["root"]) / "effective-delivery.json"
        if frozen.exists() and json.loads(frozen.read_text()) != cfg:
            raise RunUnavailable(
                "delivery configuration changed after this run started"
            )
        return cfg

    def setup(self, ctx):
        cfg = self.config(ctx)
        self._verify_openagents(cfg)
        # Resume uses the existing binding; never reset a delivered round to round one.
        Path(cfg["root"]).mkdir(parents=True, exist_ok=True)
        (Path(cfg["root"]) / "effective-delivery.json").write_text(
            json.dumps(cfg, indent=2) + "\n"
        )
        # The Trainer creates the writable adapter workspace after setup and
        # exports the current commit immediately before the first step. Bind
        # the delivery checkout in _step, when that workspace has content.

    @staticmethod
    def _verify_openagents(cfg):
        info = cfg.get("openagents")
        if not info:
            return
        try:
            ensure_checkout(
                OpenAgentsSpec(str(info["repository"]), str(info["commit"])),
                Path(info["checkout"]),
            )
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            raise RunUnavailable(f"OpenAgents checkout is not pinned and ready: {exc}") from exc

    def step(self, ctx):
        try:
            return self._step(ctx)
        except RunUnavailable:
            raise
        except Exception as exc:
            raise RunUnavailable(
                f"AHE delivery could not complete: {type(exc).__name__}: {exc}"
            ) from exc

    def _step(self, ctx):
        cfg = self.config(ctx)
        material = ctx.workspace.learning_material / "current"
        summary = json.loads((material / "summary.json").read_text())
        rows = [
            json.loads(path.read_text())
            for path in sorted((material / "tasks").glob("*.json"))
        ]
        if len(rows) != cfg["sessions_per_round"] or any(
            row["status"] != "ok" or not ((row.get("pipeline_extra") or {}).get("user") or {}).get("private")
            for row in rows
        ):
            raise RunUnavailable("AHE requires a complete valid cohort before feedback")
        assignments = json.loads(Path(cfg["assignments"]).read_text())
        rounds = {assignments[row["id"]]["round"] for row in rows}
        if len(rounds) != 1:
            raise RunUnavailable("one AHE step must contain one complete logical round")
        round_no = rounds.pop()
        binding_path = Path(cfg["binding"])
        if not binding_path.exists():
            write(
                binding_path,
                adapter=ctx.workspace.adapter,
                repo=cfg["repo"],
                run_id=cfg["identity"]["run_id"],
                round_no=1,
            )
        binding = json.loads(binding_path.read_text())
        if binding["round"] != round_no:
            # A crash after delivery may leave the new binding but an unfinished Hermit step.
            if binding["round"] != round_no + 1:
                raise RunUnavailable("delivery binding is outside this logical round")
            # Use the frozen receipt for the old tested commit; do not repeat model execution.
            receipt_path = Path(cfg["root"]) / f"round-{round_no}-adapter.json"
            binding["harness_sha"] = json.loads(receipt_path.read_text())[
                "tested_adapter"
            ]
        request = {
            key: cfg[key]
            for key in (
                "root",
                "repo",
                "tracker",
                "identity",
                "roster",
                "definitions_root",
                "delivery_timeout",
                "sessions_per_round",
            )
        }
        request.update(
            round=round_no,
            user_sha=binding["harness_sha"],
            session_roots=[str(Path(row["pipeline_extra"]["user"]["private"]).resolve()) for row in rows],
        )
        call = Path(cfg["root"]) / "calls" / f"round-{round_no}"
        call.mkdir(parents=True, exist_ok=True)
        (call / "request.json").write_text(json.dumps(request, indent=2) + "\n")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(cfg["ahe_root"]) / "backend")
        argv = [
            cfg["ahe_python"],
            "-m",
            "ahe_beta.integrations.hermit.feedback",
            "--request",
            str(call / "request.json"),
            "--result",
            str(call / "result.json"),
        ]
        with (call / "controller.log").open("a") as log:
            process = subprocess.Popen(
                argv,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                while True:
                    try:
                        code = process.wait(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        if ctx.cancel is not None and ctx.cancel.is_set():
                            raise RunUnavailable(
                                "AHE delivery cancelled; native receipts retained"
                            )
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        if code:
            raise RunUnavailable(
                f"AHE feedback unavailable; inspect {call / 'controller.log'}"
            )
        receipt = json.loads((call / "result.json").read_text())
        repo = Path(cfg["repo"])
        git(repo, "fetch", "origin", "main")
        if git(repo, "status", "--porcelain"):
            raise RunUnavailable(
                "delivery checkout has local changes; refusing to replace them"
            )
        git(repo, "switch", "--detach", receipt["delivered_adapter"])
        # Copy only tracked files covered by the adapter contract.  The delivery
        # repository also carries OpenAgents' forge metadata and specification
        # documents, which are useful to the developer side but are outside the
        # adapter's learnable/fixed paths.
        structure = structure_of(read_manifest(ctx.workspace.adapter))
        tracked = [
            relative
            for relative in git(repo, "ls-files", "-z").split("\0")
            if relative and (
                structure.is_fixed(relative)
                or structure.learnable_for(relative) is not None
            )
        ]
        with tempfile.TemporaryDirectory(prefix="ahe-delivery-") as tmp:
            candidate = Path(tmp)
            for relative in filter(None, tracked):
                source = repo / relative
                if source.is_symlink():
                    raise RunUnavailable("linked delivered adapter is unsupported")
                target = candidate / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            for relative in files_of(ctx.workspace.adapter):
                if relative != ".hermit/CARD.md":
                    (ctx.workspace.adapter / relative).unlink()
            shutil.copytree(candidate, ctx.workspace.adapter, dirs_exist_ok=True)
        checked = ctx.tools["check_spec"](path=str(ctx.workspace.adapter))
        if not checked.get("passed"):
            raise RunUnavailable(
                "delivered adapter violates the fixed template; inspect check_spec"
            )
        write(
            cfg["binding"],
            adapter=ctx.workspace.adapter,
            repo=repo,
            run_id=cfg["identity"]["run_id"],
            round_no=round_no + 1,
        )
        (ctx.workspace.output / "delivery.json").write_text(
            json.dumps(
                {**receipt, "hermit_tested_commit": summary["adapter_commit"]}, indent=2
            )
            + "\n"
        )
        raw = receipt.get("usage", {})
        usage = (
            [
                Usage(
                    model="ahe-demand",
                    input_tokens=raw.get("input_tokens", 0),
                    output_tokens=raw.get("output_tokens", 0),
                    calls=raw.get("event_count", 0),
                    unreported_calls=raw.get("unavailable_count", 0),
                )
            ]
            if raw
            else []
        )
        return StepResult(
            status="done",
            summary=f"AHE round {round_no}: {len(rows)} user sessions delivered",
            usage=usage,
            extra={"delivery": receipt},
        )
