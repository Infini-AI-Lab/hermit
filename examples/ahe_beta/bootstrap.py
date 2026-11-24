"""The Gitea and OpenAgents side of AHE-Beta's own delivery Evolver (./delivery/): the adapter repository, its
delivery clone and its worker clone, then prepare.py's configuration with delivery.json and run-delivery.yaml."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from .gitea_runtime import ensure_repository, seed_repository, clone_repository
    from .prepare import prepare
except ImportError:
    from gitea_runtime import ensure_repository, seed_repository, clone_repository
    from prepare import prepare


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
    **models,
):
    cohort = Path(cohort).resolve()
    token = os.environ.get(gitea_token_env)
    if not token:
        raise RuntimeError(f"missing Gitea credential in ${gitea_token_env}")
    template = Path(__file__).resolve().parent / "template"
    repository = ensure_repository(
        base_url=gitea_url,
        owner=gitea_owner,
        name=gitea_repo,
        token=token,
    )
    delivery_repo = Path(delivery_repo or cohort / "delivery-repo").resolve()
    worker_clone = Path(worker_clone or cohort / "openagents-worker").resolve()
    delivery = seed_repository(
        template=template,
        repository=repository,
        destination=delivery_repo,
        token=token,
        owner=gitea_owner,
    )
    worker = clone_repository(
        clone_url=repository.get("clone_url") or repository["html_url"],
        destination=worker_clone,
        token=token,
        owner=gitea_owner,
    )
    worker_config = worker / ".openagents/openagents.yml"
    worker_config.parent.mkdir(parents=True, exist_ok=True)
    worker_config.write_text(
        "\n".join(
            [
                "version: 1",
                "forge: gitea",
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
                "  triage: agent",
                "  rfc: agent",
                "  conflict: agent",
                "  ci: agent",
                "  e2e: agent",
                "  review: agent",
                "  merge: agent",
                "release: manual",
                "defaults:",
                "  interval: 5",
                "  base: main",
                "  labels: [openagents]",
                "repos:",
                "  - path: .",
                "",
            ]
        )
    )
    config = prepare(
        cohort=cohort,
        ahe_root=ahe_root,
        ahe_python=ahe_python,
        delivery_repo=delivery,
        run_id=run_id,
        openagents_checkout=openagents_checkout,
        **{key: value for key, value in models.items() if value is not None},
    )
    delivery_config = json.loads((cohort / "delivery.json").read_text())
    delivery_config["tracker"] = {
        "base_url": gitea_url,
        "owner": gitea_owner,
        "repo": gitea_repo,
        "token_env": gitea_token_env,
    }
    (cohort / "delivery.json").write_text(json.dumps(delivery_config, indent=2) + "\n")
    (cohort / "worker.json").write_text(
        json.dumps(
            {
                "repository": repository.get("clone_url") or repository["html_url"],
                "owner": gitea_owner,
                "repo": gitea_repo,
                "worker_clone": str(worker),
                "config": str(worker_config),
                "token_env": gitea_token_env,
            },
            indent=2,
        )
        + "\n"
    )
    return {
        "run": str(cohort / "run-delivery.yaml"),
        "delivery": str(cohort / "delivery.json"),
        "worker": str(worker_config),
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
    args = parser.parse_args()
    result = bootstrap(**vars(args))
    print(json.dumps({key: value for key, value in result.items() if key != "config"}, indent=2))


if __name__ == "__main__":
    main()
