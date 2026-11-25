"""Hermit's configuration for a cohort AHE-Beta's exporter wrote.

    PYTHONPATH=$AHE_ROOT/backend python -m ahe_beta.integrations.hermit.export --experiment <AHE-Beta experiment json> \
        --output <cohort> --cache <material cache> [--agent-seconds N]
    python examples/ahe_beta_adapter/prepare.py --cohort <cohort> --ahe-root $AHE_ROOT --run-id <name>

The exporter writes the tasks (Harbor task directories under tasks/), one row per round and slot (tasks.jsonl, with
the session's assignment under `ahe_session`: its persona card, its scenario, its round, the task's budget), train.txt
(rounds 1 to N-1) and final-round.txt (round N); the agent's budget is the exporter's (`--agent-seconds`), which writes
it into the task and the assignment alike. This writes beside them:

    rows.jsonl          the rows the acting agent and the learner read: id, harbor_task, instruction; no persona
    assignments.json    each row's assignment, which only the acting agent reads, on this machine (user.py)
    rows-train.txt, rows-final-round.txt    the splits, without the sessions left out (below)
    left-out.json       the sessions left out: those whose task runs a service in a companion container, which a
                        Hermit environment does not reach yet (AHE-Beta's session would fail at its end, after the
                        whole conversation): one a round in u8 and u16 (kv-live-surgery), two in u32
    task-info/          what the learner reads first of the tasks and their records (AHE-Beta's protocol for
                        changing the adapter is in the template's learner guide, template/.hermit/EVOLVER.md)
    run.yaml            Codex acts with AHE-Beta's simulated user; Hermit's Codex Evolver learns from the users'
                        verdicts (the judge: utils/judge.py:user); one step is one round (step_size: a round's sessions)
    delivery.json, run-delivery.yaml, delivery-binding.json, rounds.json    AHE-Beta's own demand and delivery
                        Evolver (delivery/), from bootstrap.py, which provisions its Gitea repository and OpenAgents
                        worker; on a cohort prepared before, only these

A model is a Codex model name on the ChatGPT login (`gpt-5.6-luna`) or a `<backend>/<model>` reference.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse

import yaml

HERE = Path(__file__).resolve().parent
sys.path[:0] = [p for p in (str(HERE), str(HERE / "utils")) if p not in sys.path]
from binding import ignored  # noqa: E402
CODEX_MOUNT = "/opt/hermit-codex"
ACTING_S = 300                       # the least a session's budget leaves Codex: after Harbor's delivery margin
DELIVERY_MARGIN_S = 60               # (hermit.environment.harbor: min(60 s, a tenth)) and the user's review (review_s)
MODEL_URLS = ("HARNESS_LLM_BASE_URL", "DEMAND_LLM_BASE_URL", "ANTHROPIC_BASE_URL")   # the team's models (env file)
DEFAULT_URLS = {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}   # the developers' Claude Code without one
CONTROLLER_ENV = ["HARNESS_LLM_BASE_URL", "HARNESS_LLM_API_KEY", "HARNESS_LLM_MODEL", "HARNESS_LLM_REASONING_EFFORT",
                  "HARNESS_LLM_MAX_CONTEXT_TOKENS", "DEMAND_LLM_BASE_URL", "DEMAND_LLM_API_KEY", "DEMAND_LLM_MODEL",
                  "DEMAND_LLM_REASONING_EFFORT"]   # what AHE-Beta's controller reads (demand/agent_runtime.py, ...)
LOGIN_HOSTS = ["chatgpt.com", "auth.openai.com"]
TASK_INFO = """# AHE-Beta: sessions with simulated users

Each training task is one session of a simulated user with the acting agent (Codex with this adapter) in the task's
own environment. The user follows a persona (an acceptance bar, how much autonomy it expects, how it checks results,
how patient it is) that neither the agent nor you see; it gives the task, watches progress, sends follow-ups, inserts
messages while the agent works, interrupts it, checks the result with its own commands, and at the end rates the
session from 1 to 4 with a reason and the issues it raises. One training step is one round: every seed of the cohort
once.

What a task's record holds:

- `judge.score`: the user's rating as (rating - 1) / 3; `judge.feedback`: the rating, the outcome, the reason and the
  issues the user raised.
- `pipeline_extra.verified`: the task's verifier verdict. On these tasks the verifier only copies the user's score, so
  it adds nothing to the rating; no benchmark score is in the records.
- the trajectory: `user-transcript.jsonl` (every user message, every reply and every tool call the user could see,
  with times) and `user-verdict.json`; the agent's own event stream (`agent-codex.jsonl`).
"""


def model(value: str, effort: str | None, *, cli: bool = False, shared_plan: bool = False) -> dict:
    """A model setting: a `<backend>/<model>` reference, or a Codex model name on the ChatGPT login; for the simulated
    user (`cli`), utils/ahe_user.py's form, with `shared_plan` when the actor holds the same login's plan."""
    effort_ = {"effort": effort} if effort else {}
    if "/" in value:
        return {"ref": value, **effort_}
    if cli:
        return {"cli": "codex", "model": value, "login": "chatgpt", **effort_, "unapplied": ["temperature"],
                **({"shared_plan": True} if shared_plan else {})}
    return {"name": value, "login": "chatgpt", **effort_}


def budget(row: dict) -> float | None:
    """A session's agent budget as the exporter wrote it into its assignment (and its task)."""
    scenario = (row.get("ahe_session") or {}).get("scenario") or {}
    execution = ((scenario.get("workspace") or {}).get("params") or {}).get("execution") or {}
    value = execution.get("task_budget_seconds") or row.get("agent_seconds")
    return float(value) if value else None


def publish(path: Path, text: str) -> None:
    """A file written whole or not at all (a temporary beside it, then a rename)."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def companion(row: dict) -> bool:
    """Whether a session's task runs a service in a companion container (its recipe's `placement: companion`)."""
    services = ((row.get("ahe_session") or {}).get("environment") or {}).get("services") or []
    return any(isinstance(service, dict) and service.get("placement") == "companion" for service in services)


def prepare(*, cohort, ahe_root, run_id, actor_model="gpt-5.6-luna", actor_effort="medium",
            user_model="gpt-5.6-luna", user_effort="low", learner_model="gpt-5.6-luna", learner_effort="medium",
            codex_version="0.170.0", review_seconds=600.0, budget_hours=168.0, delivery_repo=None, ahe_python=None,
            openagents_repository=None, openagents_commit=None, openagents_checkout=None, tracker=None, worker=None,
            delivery_hosts=()) -> dict:
    cohort, ahe_root = Path(cohort).resolve(), Path(ahe_root).resolve()
    if delivery_repo is not None and not worker:
        raise ValueError("AHE-Beta's own delivery needs its OpenAgents worker: bootstrap.py provisions it with the "
                         "Gitea repository and calls this")
    prepared = (cohort / "run.yaml").exists() and delivery_repo is not None   # the delivery files only, on this run
    config = yaml.safe_load((cohort / "run.yaml").read_text()) if prepared else None
    if prepared:                                         # its review time, not this call's default
        review_seconds = float(config["agent"]["params"]["user"].get("review_s", review_seconds))
    info = json.loads((cohort / "cohort.json").read_text())
    exported = [json.loads(line) for line in (cohort / "tasks.jsonl").read_text().split("\n") if line.strip()]
    kept = [r for r in exported if not companion(r)]
    per_round = Counter({int(r["ahe_session"]["round"]): 0 for r in exported})   # every round, an emptied one too
    per_round.update(int(r["ahe_session"]["round"]) for r in kept)
    if not kept or len(set(per_round.values())) > 1:
        raise ValueError(f"without the sessions left out, the rounds hold {dict(sorted(per_round.items()))} sessions: "
                         "one step cannot be one round")
    sessions = next(iter(per_round.values()))            # a round's sessions: one step
    if not math.isfinite(review_seconds) or review_seconds < 0:
        raise ValueError(f"--review-seconds must be a number of seconds, not {review_seconds!r}")
    acting = lambda b: b - min(DELIVERY_MARGIN_S, 0.1 * b) - review_seconds   # noqa: E731 - before setup takes any
    short = sorted({b for b in map(budget, kept) if b is not None and acting(b) < ACTING_S})
    if short:                                            # Codex could never start, or hardly
        raise ValueError(f"the exported budgets {short} s leave Codex less than {ACTING_S} s once Harbor's delivery "
                         f"margin and the user's review ({review_seconds:g} s) are taken: export with a larger "
                         "--agent-seconds or give a smaller --review-seconds")
    rounds = {r["id"]: int(r["ahe_session"]["round"]) for r in kept}
    if prepared:
        _delivery(cohort, config, info, sessions=sessions, rounds=rounds, ahe_root=ahe_root, ahe_python=ahe_python,
                  run_id=run_id, delivery_repo=delivery_repo, openagents_repository=openagents_repository,
                  openagents_commit=openagents_commit, openagents_checkout=openagents_checkout, tracker=tracker,
                  worker=worker, hosts=delivery_hosts)
        return config
    if (cohort / "run.yaml").exists():                   # written last: an interrupted run of this is redone whole
        raise FileExistsError(cohort / "run.yaml")
    left_out = ignored(HERE / "template")
    if left_out:                                         # Hermit's tree (git add -A) would leave them out
        raise ValueError(f"this machine's Git ignores the template's {', '.join(left_out)} (its excludes file): "
                         "Hermit's adapter tree would leave them out; unignore them")
    (cohort / "left-out.json").unlink(missing_ok=True)
    rows = [{"id": r["id"], "harbor_task": r["harbor_task"], "instruction": r["instruction"]} for r in kept]
    (cohort / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))   # escaped: one row, one line
    (cohort / "assignments.json").write_text(json.dumps({r["id"]: r["ahe_session"] for r in kept},
                                                        ensure_ascii=False, indent=1) + "\n")
    ids = {r["id"] for r in kept}
    for source, target in (("train.txt", "rows-train.txt"), ("final-round.txt", "rows-final-round.txt")):
        (cohort / target).write_text("".join(f"{i}\n" for i in (cohort / source).read_text().split() if i in ids))
    left = [r for r in exported if companion(r)]
    if left:
        (cohort / "left-out.json").write_text(json.dumps(
            {"why": "the task runs a service in a companion container, which a Hermit environment does not reach yet",
             "sessions": [r["id"] for r in left]}, indent=1) + "\n")
        print(f"left out {len(left)} sessions whose task needs a companion container "
              f"({', '.join(sorted({r['harbor_task'] for r in left}))}): a step is a round's {sessions} sessions")
    (cohort / "task-info").mkdir(exist_ok=True)
    (cohort / "task-info/README.md").write_text(TASK_INFO)
    config = {
        "data": {"tasks": "rows.jsonl", "train": "rows-train.txt", "test": "rows-final-round.txt",
                 "step_size": sessions, "shuffle": False},
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
                "params": {"run_id": run_id, "model": model(user_model, user_effort, cli=True,
                                                             shared_plan="/" not in actor_model)},   # its plan
                "wait_s": 3600,
                "exit_wait_s": 120,
                "review_s": review_seconds,
            }},
        },
        "environment": {"harbor": {
            "tasks_dir": "tasks",
            "jobs_dir": "jobs",
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
    publish(cohort / "run.yaml", yaml.safe_dump(config, sort_keys=False))   # last, and whole: it marks the run done
    if delivery_repo is not None:
        _delivery(cohort, config, info, sessions=sessions, rounds=rounds, ahe_root=ahe_root, ahe_python=ahe_python,
                  run_id=run_id, delivery_repo=delivery_repo, openagents_repository=openagents_repository,
                  openagents_commit=openagents_commit, openagents_checkout=openagents_checkout, tracker=tracker,
                  worker=worker, hosts=delivery_hosts)
    return config


def _hosts(*urls) -> set[str]:
    """The host names of URLs or of bare `host[:port]` values, as the sandbox's proxy compares them."""
    names = (urlparse(url if "://" in url else f"//{url}").hostname for url in urls if url)
    return {name for name in names if name}


def _delivery(cohort: Path, config: dict, info: dict, *, sessions: int, rounds: dict, ahe_root: Path, ahe_python,
              run_id, delivery_repo, openagents_repository, openagents_commit, openagents_checkout, tracker=None,
              worker=None, hosts=()) -> None:
    """delivery.json and run-delivery.yaml: run.yaml with AHE-Beta's own demand and delivery Evolver, its team in the
    run's sandbox (the network: the Gitea host, the team's model hosts the environment names, `hosts`); rounds.json
    (each session's round, which the Evolver reads instead of the assignments); and delivery-binding.json, the binding
    of the run's seed adapter (the template) to the delivery clone's commit, which round 1's sessions carry, kept where
    none of the team's processes reaches. run-delivery.yaml, written last, marks them done."""
    from binding import write as bind
    from gitea_runtime import loopback
    from openagents_runtime import ensure_checkout, from_compatibility, write_manifest
    if (cohort / "run-delivery.yaml").exists():          # an interrupted run of this is redone
        raise FileExistsError(cohort / "run-delivery.yaml")
    openagents = from_compatibility(ahe_root, repository=openagents_repository, commit=openagents_commit)
    manifest = None
    if openagents is not None:
        checkout = Path(openagents_checkout or cohort / "openagents").resolve()
        ensure_checkout(openagents, checkout)
        manifest = write_manifest(cohort / "openagents.json", openagents, checkout)
    state, repo, binding = cohort / "delivery-state", Path(delivery_repo).resolve(), cohort / "delivery-binding.json"
    bind(binding, adapter=HERE / "template", repo=repo, run_id=run_id, round_no=1)   # anew: no team ran on it yet
    publish(cohort / "rounds.json", json.dumps(rounds, indent=1) + "\n")
    tracker = {"base_url": "http://localhost:3000", "owner": "REPLACE", "repo": "REPLACE", "token_env": "GITEA_TOKEN",
               **(tracker or {})}
    request = {
        "ahe_root": str(ahe_root),
        "ahe_python": str(Path(ahe_python or ahe_root / ".venv/bin/python").absolute()),   # a venv's, not its base's
        "root": str(state),
        "repo": str(repo),
        "binding": str(binding),
        "rounds": str(cohort / "rounds.json"),
        "identity": {
            "experiment_id": run_id, "run_id": run_id, "config_hash": info["source_config_sha256"],
            "controller_sha": subprocess.check_output(["git", "-C", str(ahe_root), "rev-parse", "HEAD"],
                                                      text=True).strip(),
            "plan_hash": hashlib.sha256((cohort / "tasks.jsonl").read_bytes()).hexdigest(),
        },
        "roster": {**{name: info["roles"][name] for name in ("triager", "architect", "divergent")},
                   "owner": 0 if info["roles"]["owner_mode"] == "mechanical" else 1},
        "definitions_root": str(ahe_root / "backend/agent_defs"),
        "sessions_per_round": sessions,
        "delivery_timeout": 86400,
        "tracker": tracker,
        "controller": {"env": CONTROLLER_ENV},
        **({"worker": worker} if worker else {}),
        **({"openagents": manifest} if manifest is not None else {}),
    }
    agent = copy.deepcopy(config["agent"])
    agent["params"]["user"]["params"]["binding"] = str(binding)
    agent["params"]["user"]["params"]["run_id"] = run_id   # the sessions' and the delivery's: one run
    agent["params"]["user"]["pythonpath"] = [str(ahe_root / "backend")]   # and one AHE-Beta: its simulator, its team
    allowed = sorted(host for host in _hosts(*hosts, tracker["base_url"], *(os.environ.get(name) or
                                                                             DEFAULT_URLS.get(name)
                                                                             for name in MODEL_URLS))
                     if not loopback(host))          # never on the proxy's list: the Evolver forwards Gitea's port
    delivery = {**config, "agent": agent,
                "evolver": {"name": str(HERE / "delivery"), "params": {"delivery_config": "delivery.json"}},
                "evolver_access": {"send_material_out": True},
                "sandbox": {**config["sandbox"], "evolver": {**config["sandbox"].get("evolver", {}),
                                                             "allowed_hosts": allowed}},
                "trainer": {**config["trainer"], "run_name": f"{run_id}-delivery", "call_timeout_min": 1440}}
    publish(cohort / "delivery.json", json.dumps(request, indent=2) + "\n")
    publish(cohort / "run-delivery.yaml", yaml.safe_dump(delivery, sort_keys=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cohort", required=True)
    parser.add_argument("--ahe-root", required=True, help="the AHE-Beta checkout (its backend/ is the simulator)")
    parser.add_argument("--run-id", required=True)
    for role, effort in (("actor", "medium"), ("user", "low"), ("learner", "medium")):
        parser.add_argument(f"--{role}-model", default="gpt-5.6-luna")
        parser.add_argument(f"--{role}-effort", default=effort)
    parser.add_argument("--codex-version", default="0.170.0", help="the version of $HERMIT_CODEX_RELEASE's Codex")
    parser.add_argument("--review-seconds", type=float, default=600.0,
                        help="of each session's budget, what Codex leaves the user for its last turn and review")
    parser.add_argument("--budget-hours", type=float, default=168.0)
    args = vars(parser.parse_args())                     # AHE-Beta's own delivery: bootstrap.py
    prepare(**{k.replace("-", "_"): v for k, v in args.items()})


if __name__ == "__main__":
    main()
