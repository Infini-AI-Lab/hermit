"""CoEvoSkillsAdapterEvolver: the CoEvoSkills Generator/Verifier loop (Zhang-Henry/CoEvoSkills @ 4380d4b) as an
Evolver on the `coevoskills-adapter` template, confined to the adapter (the acting agent is fixed).

Upstream evolves one executable Skill per task, task-locally: a Generator writes the Skill in the task's container,
a fresh oracle agent runs the task with it, the task's hidden tests decide pass or fail, and only that verdict
returns to the loop; the Skill it keeps is its best snapshot. Here the oracle is the task's own acting agent and
judge, reached through the Trainer's `run_agent` tool, and every model completion of the upstream roles is a call of
the Trainer's `llm` tool (the run's `llm`, metered against the budget). The upstream code is not modified: it is
called at its seams in a worker process per question (worker.py).

One step: the step's training questions (`data.step_size` of them), each in an independent worker, at most `workers`
at a time; each worker's exported packages become skills of the adapter (`skills/<question>-evo-<name>/`, packaging.py)
with upstream's final verdict recorded. A question whose loop failed leaves the adapter as it was; the step fails only
when none of its questions produced a package. The questions do not see each other's packages, so `workers` changes
the wall clock, not what is learned. finalize hands back the last adapter (the base class): every package of the run,
which the host installs as the acting agent's native skills. The task-specific parts come from the example: its task
packages, `deliver` (the acting agent's output into the hidden test's container) and `path_map` (the container's
paths in the acting agent's workspace).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

from hermit.agent.evolver.agent import _run
from hermit.agent.evolver.base import Evolver, StepResult
from hermit.agent.evolver.methods.coevoskills_adapter.upstream import real_tree, safe_name, verify

KEEP = ("all", "passed")


def resolve(value, base: Path) -> Path:
    p = Path(os.path.expandvars(str(value))).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def remove_containers(trial: str, task_dir: str) -> None:
    """What one worker's Harbor sessions left on the Docker daemon: the containers of its Compose projects (Harbor
    names a project after its session: the trial, and `oracle-<task directory>-<random>` for each oracle check; the
    task directory's name is unique to the worker) and the image built for its task (`hb__<task directory>`)."""
    docker = shutil.which("docker")
    if docker is None:
        return
    listing = subprocess.run([docker, "ps", "-a", "--format", '{{.ID}} {{.Label "com.docker.compose.project"}}'],
                             capture_output=True, text=True)
    mine = (trial, f"oracle-{task_dir}-")
    ids = [line.split()[0] for line in listing.stdout.splitlines()
           if len(line.split()) == 2 and (line.split()[1] == trial or line.split()[1].startswith(mine[1]))]
    if ids:
        subprocess.run([docker, "rm", "-f", "-v", *ids], capture_output=True)
    subprocess.run([docker, "image", "rm", "-f", f"hb__{task_dir}"], capture_output=True)


class CoEvoSkillsAdapterEvolver(Evolver):
    supports_resume = False          # every question starts a fresh upstream loop from the starting adapter

    def setup(self, ctx) -> None:
        """Everything a step needs, checked before the first rollout: the parameters, the run's `llm`, the upstream
        checkout and Harbor, the task packages and the example's `deliver` hook."""
        p = ctx.params
        if str(p.get("keep") or "all") not in KEEP:
            raise ValueError(f"evolver.params.keep is one of {KEEP}")
        if int(p.get("workers", 1)) < 1:
            raise ValueError("evolver.params.workers is at least 1")
        if not isinstance(p.get("path_map") or {}, dict):
            raise ValueError("evolver.params.path_map is a mapping {container path: path in the workspace}")
        if getattr(ctx, "agent", None) is None:
            raise ValueError("the CoEvoSkills Adapter's roles call the run's model: set `llm` in the run config")
        base = ctx.base or Path.cwd()
        verify(resolve(p.get("upstream_root") or "$COEVOSKILLS_ROOT", base))
        packages = resolve(p.get("task_packages") or "data/packages", base)
        if not packages.is_dir() or not any((d / "task.toml").is_file() for d in packages.iterdir()):
            raise ValueError(f"no task packages at {packages}: run the example's preparation first")
        if not p.get("deliver"):
            raise ValueError("evolver.params.deliver names the example's hook that writes the acting agent's output "
                             "where the hidden test reads it (file.py:function)")
        from hermit.task import load_callable
        load_callable(str(p["deliver"]), base)

    def step(self, ctx) -> StepResult:
        if ctx.agent is None:
            return StepResult("failed", "the CoEvoSkills Adapter's roles call the run's model: set `llm` in the run "
                                        "config")
        if not ctx.tool_url:
            return StepResult("failed", "the oracle reaches the acting agent through the Trainer's tool endpoint")
        records = sorted((ctx.workspace.learning_material / "current" / "tasks").glob("*.json"))
        if not records:
            return StepResult("failed", "no learning material for this step")
        ids = [safe_name(str(json.loads(r.read_text())["id"])) for r in records]
        base = ctx.base or Path.cwd()
        params = ctx.params
        seed = ctx.workspace.state / "seed-adapter"   # the adapter without packages: every question's probe starts here
        if not seed.exists():
            shutil.copytree(ctx.workspace.adapter, seed, ignore=shutil.ignore_patterns(".git", "skills"))
        real_tree(seed)
        common = {"tool_url": ctx.tool_url, "seed": str(seed), "base": str(base),
                  "model_name": str(ctx.agent.model.model), "deliver": str(params.get("deliver") or ""),
                  "path_map": dict(params.get("path_map") or {}),
                  "upstream_root": str(resolve(params.get("upstream_root") or "$COEVOSKILLS_ROOT", base)),
                  **{k: params[k] for k in ("max_iterations", "max_episodes", "task_timeout_s")}}
        packages = resolve(params.get("task_packages") or "data/packages", base)

        def one(tid: str) -> dict:
            out = ctx.workspace.output / tid
            unique = uuid.uuid4().hex[:8]                     # Harbor's project names have no dot: none here either
            name = tid.lower().replace(".", "-")
            trial, task_dir = f"coevoskills-{name}-{unique}", f"{name}-{unique}"
            request = {**common, "task_id": tid, "task_path": str(packages / tid.lower()), "output": str(out / "loop"),
                       "projection_root": str(ctx.workspace.scratch / tid / "probes"), "trial_name": trial,
                       "task_dir": task_dir}
            path = ctx.workspace.scratch / tid / "request.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(request, indent=2))
            out.mkdir(parents=True, exist_ok=True)
            try:
                code = _run([sys.executable, "-m", "hermit.agent.evolver.methods.coevoskills_adapter.worker",
                             str(path)], {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, ctx, out / "loop.log", base)
            finally:      # a worker stopped before Harbor's own cleanup leaves its containers behind
                remove_containers(trial, task_dir)
            result = out / "loop" / "result.json"
            if code or not result.is_file():
                return {"task": tid, "status": "failed",
                        "error": f"the upstream loop exited {code}; see {tid}/loop.log"}
            return {"task": tid, "status": "done", **json.loads(result.read_text())}

        with ThreadPoolExecutor(max_workers=min(int(params.get("workers") or 1), len(ids))) as pool:
            outcomes = list(pool.map(one, ids))
        if ctx.cancel is not None and ctx.cancel.is_set():
            return StepResult("failed", "stopped by the Trainer", extra={"questions": outcomes})
        keep = str(params.get("keep") or "all")
        installed = []
        for outcome in outcomes:
            if outcome["status"] != "done":
                continue
            skills = Path(outcome.pop("skills"))
            outcome["exported"] = os.path.relpath(skills, ctx.workspace.output)    # as the step's records keep it
            outcome["installed"] = False
            if keep == "passed" and outcome.get("passed") is not True:
                continue
            try:                                      # one question's package must not void the step's others
                outcome["skills"] = self._install(ctx, outcome["task"], skills)
            except (OSError, ValueError, RuntimeError) as exc:
                outcome["install_error"] = f"{type(exc).__name__}: {exc}"[:300]
                continue
            outcome["installed"] = True
            installed.append(outcome["task"])
        (ctx.workspace.output / "questions.json").write_text(json.dumps(outcomes, indent=2))
        summary = (f"{len(installed)}/{len(ids)} questions installed a Skill package"
                   + (f": {', '.join(installed)}" if installed else ""))
        failed = [o["task"] for o in outcomes if o["status"] != "done"]
        if failed:
            summary += f"; loop failed on {', '.join(failed)}"
        refused = [o["task"] for o in outcomes if o.get("install_error")]
        if refused:
            summary += f"; not installable: {', '.join(refused)}"
        # Failed: nothing came of the step's loops. A step whose loops ran but whose packages `keep: passed` left out
        # is done, with the adapter unchanged.
        ran = any(o["status"] == "done" and not o.get("install_error") for o in outcomes)
        return StepResult("done" if installed or ran else "failed", summary[:300], extra={"questions": outcomes})

    @staticmethod
    def _install(ctx, tid: str, source: Path) -> list[str]:
        """The exported packages, as skills (packaging.py), replace the question's own earlier skills in the adapter
        (by name, as `state/installed.json` records them); a name another question's skill has is refused. All or
        nothing: the earlier skills are set aside first and put back when a step of the install fails or the template
        check (the run's `check_spec`) finds a problem in the new ones. Returns the skills' names."""
        from hermit.agent.evolver.methods.coevoskills_adapter.packaging import project
        real_tree(source)
        skills = ctx.workspace.adapter / "skills"
        record = ctx.workspace.state / "installed.json"
        owners = json.loads(record.read_text()) if record.is_file() else {}
        with tempfile.TemporaryDirectory(prefix=".candidate-", dir=ctx.workspace.adapter) as tmp:
            new, old = Path(tmp) / "new", Path(tmp) / "old"
            audit = project(source, new, tid, ctx.params.get("path_map"))
            taken = {name: other for other, names in owners.items() if other != tid for name in names}
            clash = [f"{name} ({taken[name]})" for name in audit["skills"] if name in taken]
            if clash:
                raise ValueError(f"another question's skill has the name: {', '.join(clash)}")
            (ctx.workspace.output / tid / "portability.json").write_text(json.dumps(audit, indent=2))
            skills.mkdir(exist_ok=True)
            old.mkdir()
            aside = [name for name in dict.fromkeys([*owners.get(tid, []), *audit["skills"]])
                     if (skills / name).is_dir() and not (skills / name).is_symlink()]
            placed = []
            try:
                for name in aside:                    # the question's earlier skills, and a same-named orphan
                    (skills / name).rename(old / name)
                for name in audit["skills"]:
                    (new / name).rename(skills / name)
                    placed.append(name)
                check = ctx.tools.get("check_spec") if getattr(ctx, "tools", None) else None
                if check is not None:
                    issues = [i for i in (check(path=str(ctx.workspace.adapter)) or {}).get("issues", [])
                              if any(str(i.get("path", "")).startswith(f"skills/{name}") for name in placed)]
                    if issues:
                        raise ValueError(f"the template check refused the packages: {issues[0].get('problem')}")
            except BaseException:
                for name in placed:
                    shutil.rmtree(skills / name, ignore_errors=True)
                for name in aside:
                    (old / name).rename(skills / name)
                raise
        owners[tid] = audit["skills"]
        record.write_text(json.dumps(owners, indent=1, sort_keys=True))
        return audit["skills"]
