"""The Gitea and OpenAgents side of AHE-Beta's own delivery Evolver (./delivery/): the adapter repository, its
delivery clone and its worker clone, then prepare.py's configuration with delivery.json and run-delivery.yaml, which
name the worker the Evolver starts in the run's sandbox and the hosts that sandbox reaches."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import uuid

try:
    from .gitea_runtime import ensure_repository, git_url, plain, seed_repository, clone_repository
    from .openagents_runtime import apart
    from .prepare import prepare, start_problem
except ImportError:
    from gitea_runtime import ensure_repository, git_url, plain, seed_repository, clone_repository
    from openagents_runtime import apart
    from prepare import prepare, start_problem


# The developers' Claude Code, on an Anthropic-compatible endpoint: AHE-Beta's credential and configuration variables
# (experiment/controllers/runtime.py, _CLAUDE_CRED_ENVS and _CLAUDE_CONFIG_ENVS); its subagent lock is the Evolver's.
WORKER_ENV = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
              "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "DISABLE_UPDATES", "OPENAGENTS_CLAUDE_IDLE_TIMEOUT",
              "OPENAGENTS_PERMISSION_MODE"]       # unset: bypassPermissions, as AHE-Beta runs its developers
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
    developers=None,
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
    why = start_problem(cohort)                          # before anything is made for it
    if why:
        raise ValueError(why)
    token = os.environ.get(gitea_token_env)
    if not token:
        raise RuntimeError(f"missing Gitea credential in ${gitea_token_env}")
    template = Path(__file__).resolve().parent / "template"
    delivery_repo = Path(delivery_repo or cohort / "delivery-repo").resolve()
    worker_clone = Path(worker_clone or cohort / "openagents-worker").resolve()
    # A folder each, as the delivery grants them (the worker writes its clone and its home beside it, this machine runs
    # Git and uv in the delivery clone and the OpenAgents checkout, the controller writes its state), none the cohort's.
    problems = apart({"--delivery-repo": delivery_repo, "--worker-clone": worker_clone,
                      "the worker's home (beside its clone)": worker_clone.with_name(worker_clone.name + "-home"),
                      "--openagents-checkout": Path(openagents_checkout or cohort / "openagents").resolve(),
                      "the controller's state": cohort / "delivery-state",
                      "--ahe-python's environment": Path(ahe_python or Path(ahe_root) / ".venv/bin/python")
                      .absolute().parent.parent,
                      # what prepare.py and the run put in the cohort: AHE-Beta's snapshots, the sessions' records
                      # and the Trainer's runs
                      "AHE-Beta's snapshots": cohort / "ahe-beta", "the sessions' records": cohort / ".logs",
                      "the Trainer's runs": cohort / "runs"}, cohort)
    if problems:
        raise ValueError("; ".join(problems) + ": each needs a folder of its own, apart from the cohort's personas")
    parallel = roster(cohort, developers)                # before anything is made for it, as the checks above
    # What this cohort's bootstrap made, noted before it makes it: all a retry takes again (no team has had any of it
    # before run-delivery.yaml, above). A repository or clone made otherwise is refused.
    made = cohort / "bootstrap.json"
    record = json.loads(made.read_text()) if made.exists() else {}

    def note(**entries):                                 # written whole: a stop leaves the record before or after
        record.update(entries)
        partial = made.with_name(f".{made.name}.partial")
        partial.write_text(json.dumps(record, indent=2) + "\n")
        os.replace(partial, made)
    # The developers' home beside the worker's clone, which the Evolver makes: this cohort's, noted before it is made,
    # never an earlier cohort's left at this path (its clone removed, its home not).
    home = worker_clone.with_name(worker_clone.name + "-home")
    homes = list(record.get("homes") or [])
    if home.exists() and str(home) not in homes:
        raise ValueError(f"the worker's home {home} (beside --worker-clone) is not this cohort's: its developers "
                         "would start from another cohort's files; remove it, or give another --worker-clone")
    if str(home) not in homes:
        note(homes=sorted({*homes, str(home)}))
    home.mkdir(parents=True, exist_ok=True)              # made now: another cohort's clone cannot take its place
    origin = git_url(gitea_url, gitea_owner, gitea_repo)   # what the Evolver and its sandbox reach, not what the API
    # advertises (its ROOT_URL may be another origin). The description of the repository this cohort's bootstrap
    # creates, noted before it creates it: the one repository a retry takes again (a creation whose answer was lost
    # too), not another creator's, nor an earlier cohort's at this path (a record and a marker of its own).
    marker = record.get("marker") or f"AHE-Beta delivery {uuid.uuid4().hex}"
    if record.get("marker") != marker:
        note(marker=marker)
    repository = ensure_repository(base_url=gitea_url, owner=gitea_owner, name=gitea_repo, token=token, marker=marker)
    repository = {**repository, "clone_url": origin}
    clones = list(record.get("clones") or [])
    ours = {str(path): str(path) in clones for path in (delivery_repo, worker_clone)}
    note(clones=sorted({*clones, *(str(path) for path in (delivery_repo, worker_clone) if not path.exists())}))
    delivery = seed_repository(
        template=template,
        repository=repository,
        destination=delivery_repo,
        token=token,
        owner=gitea_owner,
        existing_ok=ours[str(delivery_repo)],
    )
    worker = clone_repository(
        clone_url=origin,
        destination=worker_clone,
        token=token,
        owner=gitea_owner,
        existing_ok=ours[str(worker_clone)],
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
        worker={"clone": str(worker), "parallel": parallel, "env": [gitea_token_env, *WORKER_ENV],
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


def roster(cohort: Path, developers=None) -> int:
    """The worker's parallel developers: `developers` when given, else the exported experiment's (cohort.json's
    roles.developer, which AHE-Beta's controller runs as `vigil --parallel`)."""
    if developers is None:
        try:
            developers = (json.loads((cohort / "cohort.json").read_text()).get("roles") or {}).get("developer")
        except (OSError, ValueError, AttributeError):
            developers = None
    if not isinstance(developers, int) or isinstance(developers, bool) or developers < 1:
        raise ValueError(f"cohort.json names no developer count ({developers!r}): give --developers")
    return developers


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
    parser.add_argument("--openagents-checkout",
                        help="the pinned OpenAgents source (default: the cohort's, cloned here); one of yours, never "
                             "a worker's clone: Git runs in it on this machine")
    parser.add_argument("--developers", type=int,
                        help="the OpenAgents worker's parallel developers (default: the cohort's, cohort.json)")
    parser.add_argument("--review-seconds", type=float,
                        help="for a cohort not prepared yet: prepare.py's (a prepared one keeps its own)")
    parser.add_argument("--delivery-host", dest="delivery_hosts", action="append", default=[],
                        help="a host AHE-Beta's team may reach besides Gitea's and its models' (repeatable)")
    args = parser.parse_args()
    result = bootstrap(**vars(args))
    print(json.dumps({key: value for key, value in result.items() if key != "config"}, indent=2))


if __name__ == "__main__":
    main()
