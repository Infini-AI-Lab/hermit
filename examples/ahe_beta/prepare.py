"""Hermit's configuration for a cohort AHE-Beta's exporter wrote.

    PYTHONPATH=$AHE_ROOT/backend python -m ahe_beta.integrations.hermit.export --experiment <AHE-Beta experiment json> \
        --output <cohort> --cache <material cache> [--agent-seconds N]
    python examples/ahe_beta/prepare.py --cohort <cohort> --ahe-root $AHE_ROOT --run-id <name> [--delivery-repo ...]

The exporter writes the tasks (Harbor task directories under tasks/), one row per round and slot (tasks.jsonl, with
the session's assignment under `ahe_session`: its persona card, its scenario, its round), train.txt (rounds 1 to N-1)
and final-round.txt (round N). This writes beside them:

    rows.jsonl          the rows the acting agent and the learner read: id, harbor_task, instruction; no persona
    assignments.json    each row's assignment, which only the acting agent reads, on this machine (user.py)
    task-info/          what the learner reads first: AHE-Beta's evolution protocol
    run.yaml            Codex acts with AHE-Beta's simulated user; Hermit's Codex Evolver learns from the users'
                        verdicts (the judge: utils/judge.py:user); one step is one round (step_size: a round's sessions)
    delivery.json, run-delivery.yaml    with --delivery-repo: AHE-Beta's own demand and delivery Evolver (../delivery/)

A model is a Codex model name on the ChatGPT login (`gpt-5.6-luna`) or a `<backend>/<model>` reference.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import yaml

HERE = Path(__file__).resolve().parent
CODEX_MOUNT = "/opt/hermit-codex"
LOGIN_HOSTS = ["chatgpt.com", "auth.openai.com"]
TASK_INFO = """# AHE-Beta: harness evolution from simulated users' requirements

Each training task is one session of a simulated user with the acting agent (Codex with this adapter) in the task's
own environment. The user follows a persona (an acceptance bar, how much autonomy it expects, how it checks results,
how patient it is) that the agent never sees; it gives the task, watches progress, sends follow-ups, inserts messages
while the agent works, interrupts it, checks the result with its own commands, and at the end rates the session from 1
to 4 with a reason and the issues it raises. One training step is one round: every seed of the cohort once.

What a task's record holds:

- `judge.score`: the user's rating as (rating - 1) / 3; `judge.feedback`: the rating, the outcome, the reason and the
  issues the user raised. No task verifier or benchmark score reaches you (AHE-Beta's protocol: benchmarks are for
  evaluation after evolution).
- the trajectory: `user-transcript.jsonl` (every user message, every reply and every tool call the user could see,
  with times) and `user-verdict.json`; the agent's own event stream (`agent-codex.jsonl`).

How to improve the adapter, as AHE-Beta's engineering team does:

1. Read the issues as evidence of requirements, not as instructions: drop those about the user's task itself rather
   than the harness, those the session does not support, and those the adapter already meets.
2. Merge requirements that share a root cause into one structural requirement, and look across sessions for unstated
   requirements (how the agent reports progress, asks before acting, verifies and shows evidence, keeps the user's
   constraints, responds to an interruption).
3. Change the adapter at the layer that owns the behavior (instructions, a skill, a script, a hook), as a change that
   generalizes: no task names, repository names, file paths or answers of a training task.
"""


def model(value: str, effort: str | None, *, cli: bool = False) -> dict | str:
    """A model setting: a `<backend>/<model>` reference, or a Codex model name on the ChatGPT login."""
    if "/" in value:
        return value if cli else {"ref": value, **({"effort": effort} if effort else {})}
    spec = {"name": value, "login": "chatgpt", **({"effort": effort} if effort else {})}
    if cli:
        return {"cli": "codex", "model": value, "login": "chatgpt", **({"effort": effort} if effort else {}),
                "unapplied": ["temperature"]}
    return spec


def prepare(*, cohort, ahe_root, run_id, actor_model="gpt-5.6-luna", actor_effort="medium",
            user_model="gpt-5.6-luna", user_effort="low", learner_model="gpt-5.6-luna", learner_effort="medium",
            codex_version="0.170.0", agent_seconds=None, budget_hours=168.0, delivery_repo=None, ahe_python=None,
            openagents_repository=None, openagents_commit=None, openagents_checkout=None) -> dict:
    cohort, ahe_root = Path(cohort).resolve(), Path(ahe_root).resolve()
    info = json.loads((cohort / "cohort.json").read_text())
    exported = [json.loads(line) for line in (cohort / "tasks.jsonl").read_text().splitlines() if line.strip()]
    for name in ("rows.jsonl", "assignments.json", "run.yaml"):
        if (cohort / name).exists():
            raise FileExistsError(cohort / name)
    rows = [{"id": r["id"], "harbor_task": r["harbor_task"], "instruction": r["instruction"]} for r in exported]
    (cohort / "rows.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    (cohort / "assignments.json").write_text(json.dumps({r["id"]: r["ahe_session"] for r in exported},
                                                        ensure_ascii=False, indent=1) + "\n")
    (cohort / "task-info").mkdir(exist_ok=True)
    (cohort / "task-info/README.md").write_text(TASK_INFO)
    config = {
        "data": {"tasks": "rows.jsonl", "train": "train.txt", "test": "final-round.txt",
                 "step_size": info["sessions_per_round"], "shuffle": False},
        "task_info": "task-info",
        "template": str(HERE / "template"),
        "agent": {
            "name": f"{HERE / 'utils/user.py'}:UserCodexAgent",
            "base": "codex",
            "concurrency": info["max_parallel_users"],
            "model": model(actor_model, actor_effort),
            "codex": {"web_search": False, "bin": f"{CODEX_MOUNT}/bin/codex", "version": codex_version},
            "params": {"user": {
                "simulate": "ahe_user:simulate",
                "pythonpath": [str(ahe_root / "backend")],
                "assignments": "assignments.json",
                "params": {"run_id": run_id, "model": model(user_model, user_effort, cli=True)},
                "wait_s": 3600,
                "exit_wait_s": 120,
            }},
        },
        "environment": {"harbor": {
            "tasks_dir": "tasks",
            "jobs_dir": "jobs",
            "agent_timeout_sec": agent_seconds,
            "docker_cleanup": "images_keep",
            "environment": {"delete": True, "mounts": [
                {"type": "bind", "source": "$HERMIT_CODEX_RELEASE", "target": CODEX_MOUNT, "read_only": True}]},
        }},
        "judge": {"path": str(HERE / "utils/judge.py"), "name": "user"},
        "evolver": {"name": "codex", "base": "codex", "model": model(learner_model, learner_effort),
                    "params": {"tools": ["check_spec"]}, "codex": {"web_search": False}},
        "sandbox": {"enabled": True, "enforce": "required",
                    "evolver": {"allowed_hosts": [] if "/" in learner_model else LOGIN_HOSTS}},
        "trainer": {"run_name": run_id, "output_dir": "runs", "epochs": 1, "call_timeout_min": 240,
                    "budget": {"hours": budget_hours}},
        "eval": {"repeats": 1},
    }
    (cohort / "run.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    if delivery_repo is not None:
        _delivery(cohort, config, info, ahe_root=ahe_root, ahe_python=ahe_python, run_id=run_id,
                  delivery_repo=delivery_repo, openagents_repository=openagents_repository,
                  openagents_commit=openagents_commit, openagents_checkout=openagents_checkout)
    return config


def _delivery(cohort: Path, config: dict, info: dict, *, ahe_root: Path, ahe_python, run_id, delivery_repo,
              openagents_repository, openagents_commit, openagents_checkout) -> None:
    """delivery.json and run-delivery.yaml: run.yaml with AHE-Beta's own demand and delivery Evolver."""
    import sys
    sys.path.insert(0, str(HERE))
    from openagents_runtime import ensure_checkout, from_compatibility, write_manifest
    for name in ("delivery.json", "run-delivery.yaml"):
        if (cohort / name).exists():
            raise FileExistsError(cohort / name)
    openagents = from_compatibility(ahe_root, repository=openagents_repository, commit=openagents_commit)
    manifest = None
    if openagents is not None:
        checkout = Path(openagents_checkout or cohort / "openagents").resolve()
        ensure_checkout(openagents, checkout)
        manifest = write_manifest(cohort / "openagents.json", openagents, checkout)
    state = cohort / "delivery-state"
    request = {
        "ahe_root": str(ahe_root),
        "ahe_python": str(Path(ahe_python or ahe_root / ".venv/bin/python").resolve()),
        "root": str(state),
        "repo": str(Path(delivery_repo).resolve()),
        "binding": str(state / "binding.json"),
        "identity": {
            "experiment_id": run_id, "run_id": run_id, "config_hash": info["source_config_sha256"],
            "controller_sha": subprocess.check_output(["git", "-C", str(ahe_root), "rev-parse", "HEAD"],
                                                      text=True).strip(),
            "plan_hash": hashlib.sha256((cohort / "tasks.jsonl").read_bytes()).hexdigest(),
        },
        "roster": {**{name: info["roles"][name] for name in ("triager", "architect", "divergent")},
                   "owner": 0 if info["roles"]["owner_mode"] == "mechanical" else 1},
        "definitions_root": str(ahe_root / "backend/agent_defs"),
        "assignments": str(cohort / "assignments.json"),
        "sessions_per_round": info["sessions_per_round"],
        "delivery_timeout": 86400,
        "tracker": {"base_url": "http://localhost:3000", "owner": "REPLACE", "repo": "REPLACE",
                    "token_env": "GITEA_TOKEN"},
        **({"openagents": manifest} if manifest is not None else {}),
    }
    delivery = {**config, "evolver": {"name": str(HERE / "delivery"), "params": {"delivery_config": "delivery.json"}},
                "evolver_access": {"send_material_out": True},
                "trainer": {**config["trainer"], "run_name": f"{run_id}-delivery", "call_timeout_min": 1440}}
    (cohort / "delivery.json").write_text(json.dumps(request, indent=2) + "\n")
    (cohort / "run-delivery.yaml").write_text(yaml.safe_dump(delivery, sort_keys=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cohort", required=True)
    parser.add_argument("--ahe-root", required=True, help="the AHE-Beta checkout (its backend/ is the simulator)")
    parser.add_argument("--run-id", required=True)
    for role, effort in (("actor", "medium"), ("user", "low"), ("learner", "medium")):
        parser.add_argument(f"--{role}-model", default="gpt-5.6-luna")
        parser.add_argument(f"--{role}-effort", default=effort)
    parser.add_argument("--codex-version", default="0.170.0", help="the version of $HERMIT_CODEX_RELEASE's Codex")
    parser.add_argument("--agent-seconds", type=float, help="every task's agent budget (default: each task's own)")
    parser.add_argument("--budget-hours", type=float, default=168.0)
    parser.add_argument("--delivery-repo", help="AHE-Beta's own delivery: the clean adapter clone (../delivery/)")
    parser.add_argument("--ahe-python")
    for name in ("openagents-repository", "openagents-commit", "openagents-checkout"):
        parser.add_argument(f"--{name}")
    args = vars(parser.parse_args())
    prepare(**{k.replace("-", "_"): v for k, v in args.items()})


if __name__ == "__main__":
    main()
