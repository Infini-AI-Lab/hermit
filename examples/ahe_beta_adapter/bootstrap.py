"""The Gitea and OpenAgents side of AHE-Beta's own delivery Evolver (./delivery/): the adapter repository, its
delivery clone and its worker clone, then prepare.py's configuration with delivery.json and run-delivery.yaml, which
name the worker the Evolver starts in the run's sandbox and the hosts that sandbox reaches."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from .gitea_runtime import ensure_repository, git_url, plain, seed_repository, clone_repository
    from .prepare import prepare
except ImportError:
    from gitea_runtime import ensure_repository, git_url, plain, seed_repository, clone_repository
    from prepare import prepare


# The developers' Claude Code, on an Anthropic-compatible endpoint: AHE-Beta's credential and configuration variables
# (experiment/controllers/runtime.py, _CLAUDE_CRED_ENVS and _CLAUDE_CONFIG_ENVS); its subagent lock is the Evolver's.
WORKER_ENV = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
              "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "DISABLE_UPDATES", "OPENAGENTS_CLAUDE_IDLE_TIMEOUT"]
# The worker's configuration as AHE-Beta writes it (runtime.py, write_openagents_config and _VIGIL_STAGES): AHE-Beta's
# own roles triage, the CI and end-to-end stages are skipped, each delivery shows its execution evidence.
STAGES = {"triage": "skip", "rfc": "agent", "conflict": "agent", "ci": "skip", "e2e": "skip", "review": "agent",
          "merge": "agent"}
CONFIGURED = ("actor_model", "actor_effort", "user_model", "user_effort", "codex_version", "review_seconds")


def keep_out(clone: Path, pattern: str) -> None:
    """`pattern` in a clone's local exclude file (once)."""
    exclude = Path(clone) / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if pattern not in lines:
        exclude.write_text("\n".join([*lines, pattern]) + "\n")


def bootstrap(
    *,
    cohort,
    ahe_root,
    ahe_python,
    run_id,
    gitea_url,
    gitea_owner,
    gitea_repo,
    gitea_token_env="GITEA_TOKEN",
    delivery_repo=None,
    worker_clone=None,
    openagents_checkout=None,
    developers=4,
    **models,
):
    cohort = Path(cohort).resolve()
    if (cohort / "run-delivery.yaml").exists():          # done: its clones have been the team's since, and nothing
        raise FileExistsError(cohort / "run-delivery.yaml")   # here runs Git in them or writes there again
    given = sorted(name for name in CONFIGURED if models.get(name) is not None)
    if given and (cohort / "run.yaml").exists():
        raise ValueError(f"the cohort is prepared (run.yaml): the delivery runs its settings, not {', '.join(given)}; "
                         "prepare another cohort for others")
    if plain(gitea_url):                                  # the token would cross the network in the clear
        raise ValueError(f"{gitea_url}: a Gitea on another machine is reached over https")
    token = os.environ.get(gitea_token_env)
    if not token:
        raise RuntimeError(f"missing Gitea credential in ${gitea_token_env}")
    template = Path(__file__).resolve().parent / "template"
    delivery_repo = Path(delivery_repo or cohort / "delivery-repo").resolve()
    worker_clone = Path(worker_clone or cohort / "openagents-worker").resolve()
    # A folder each: the worker writes its clone, and this machine runs Git and uv in the other two.
    folders = {"--delivery-repo": delivery_repo, "--worker-clone": worker_clone,
               "--openagents-checkout": Path(openagents_checkout or cohort / "openagents").resolve()}
    for (a, one), (b, other) in ((x, y) for x in folders.items() for y in folders.items() if x[0] < y[0]):
        if one == other or one in other.parents or other in one.parents:
            raise ValueError(f"{a} and {b} overlap ({one}, {other}): each needs a folder of its own")
    repository = ensure_repository(
        base_url=gitea_url,
        owner=gitea_owner,
        name=gitea_repo,
        token=token,
    )
    origin = git_url(gitea_url, gitea_owner, gitea_repo)   # what the Evolver and its sandbox reach, not what the API
    repository = {**repository, "clone_url": origin}       # advertises (its ROOT_URL may be another origin)
    delivery = seed_repository(
        template=template,
        repository=repository,
        destination=delivery_repo,
        token=token,
        owner=gitea_owner,
    )
    worker = clone_repository(
        clone_url=origin,
        destination=worker_clone,
        token=token,
        owner=gitea_owner,
    )
    keep_out(worker, ".openagents/")     # OpenAgents' own runtime state: a retry still finds the clone clean
    worker_config = worker / ".openagents/openagents.yml"
    worker_config.parent.mkdir(parents=True, exist_ok=True)
    kept = cohort / "openagents-worker.yml"   # this machine's copy, which the Evolver shows the worker read-only
    kept.write_text(
        "\n".join(
            [
                "version: 1",
                "forge: gitea",
                "execution_evidence:",
                "  required: true",
                "coordination: soft",
                "gitea:",
                f"  base_url: {gitea_url}",
                f"  owner: {gitea_owner}",
                f"  repo: {gitea_repo}",
                f"  token_env: {gitea_token_env}",
                "agents:",
                "  triager: {backend: claude_code}",
                "  drafter: {backend: claude_code}",
                "  implementer: {backend: claude_code}",
                "  reviewer: {backend: claude_code}",
                "  replier: {backend: claude_code}",
                "  deliver: {backend: claude_code}",
                "stages:",
                *(f"  {stage}: {owner}" for stage, owner in STAGES.items()),
                "release: manual",
                "defaults:",
                "  interval: 30",
                "  base: main",
                "  labels: [openagents]",
                "repos:",
                "  - path: .",
                "",
            ]
        )
    )
    worker_config.write_text(kept.read_text())
    config = prepare(
        cohort=cohort,
        ahe_root=ahe_root,
        ahe_python=ahe_python,
        delivery_repo=delivery,
        run_id=run_id,
        openagents_checkout=openagents_checkout,
        tracker={"base_url": gitea_url, "owner": gitea_owner, "repo": gitea_repo, "token_env": gitea_token_env},
        worker={"clone": str(worker), "parallel": int(developers), "env": [gitea_token_env, *WORKER_ENV],
                "config": str(kept)},
        **{key: value for key, value in models.items() if value is not None},
    )
    return {
        "run": str(cohort / "run-delivery.yaml"),
        "delivery": str(cohort / "delivery.json"),
        "worker": str(worker),
        "repository": repository.get("full_name", f"{gitea_owner}/{gitea_repo}"),
        "openagents": str(cohort / "openagents.json"),
        "config": config,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "cohort",
        "ahe-root",
        "ahe-python",
        "run-id",
        "gitea-url",
        "gitea-owner",
        "gitea-repo",
    ):
        parser.add_argument("--" + name, required=True)
    for name in ("actor-model", "actor-effort", "user-model", "user-effort", "codex-version"):
        parser.add_argument("--" + name)
    parser.add_argument("--gitea-token-env", default="GITEA_TOKEN")
    parser.add_argument("--delivery-repo")
    parser.add_argument("--worker-clone")
    parser.add_argument("--openagents-checkout")
    parser.add_argument("--developers", type=int, default=4, help="the OpenAgents worker's parallel developers")
    parser.add_argument("--review-seconds", type=float,
                        help="for a cohort not prepared yet: prepare.py's (a prepared one keeps its own)")
    parser.add_argument("--delivery-host", dest="delivery_hosts", action="append", default=[],
                        help="a host AHE-Beta's team may reach besides Gitea's and its models' (repeatable)")
    args = parser.parse_args()
    result = bootstrap(**vars(args))
    print(json.dumps({key: value for key, value in result.items() if key != "config"}, indent=2))


if __name__ == "__main__":
    main()
