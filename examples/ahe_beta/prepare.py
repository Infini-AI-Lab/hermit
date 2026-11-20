"""Generate a runnable Hermit configuration for a cohort exported by AHE."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import yaml

try:
    from .openagents_runtime import from_compatibility, ensure_checkout, write_manifest
except ImportError:
    from openagents_runtime import from_compatibility, ensure_checkout, write_manifest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def prepare(
    *,
    cohort,
    ahe_root,
    ahe_python,
    harbor,
    delivery_repo,
    run_id,
    actor_model,
    actor_url,
    http_only=False,
    openagents_repository=None,
    openagents_commit=None,
    openagents_checkout=None,
):
    cohort = Path(cohort).resolve()
    for name in ("delivery.json", "run.yaml"):
        if (cohort / name).exists():
            raise FileExistsError(cohort / name)
    info = json.loads((cohort / "cohort.json").read_text())
    openagents = from_compatibility(
        ahe_root,
        repository=openagents_repository,
        commit=openagents_commit,
    )
    openagents_manifest = None
    if openagents is not None:
        checkout = Path(openagents_checkout or cohort / "openagents").resolve()
        ensure_checkout(openagents, checkout)
        openagents_manifest = write_manifest(cohort / "openagents.json", openagents, checkout)
    state = cohort / "delivery-state"
    binding = state / "binding.json"
    request = {
        "ahe_root": str(Path(ahe_root).resolve()),
        "ahe_python": str(Path(ahe_python).resolve()),
        "root": str(state),
        "repo": str(Path(delivery_repo).resolve()),
        "binding": str(binding),
        "identity": {
            "experiment_id": run_id,
            "run_id": run_id,
            "config_hash": info["source_config_sha256"],
            "controller_sha": subprocess.check_output(
                ["git", "-C", str(ahe_root), "rev-parse", "HEAD"], text=True
            ).strip(),
            "plan_hash": hashlib.sha256(
                (cohort / "tasks.jsonl").read_bytes()
            ).hexdigest(),
        },
        "roster": {
            **{
                name: info["roles"][name]
                for name in ("triager", "architect", "divergent")
            },
            "owner": 0 if info["roles"]["owner_mode"] == "mechanical" else 1,
        },
        "definitions_root": str(Path(ahe_root).resolve() / "backend/agent_defs"),
        "sessions_per_round": info["sessions_per_round"],
        "delivery_timeout": 86400,
        "tracker": {
            "base_url": "http://localhost:3000",
            "owner": "REPLACE",
            "repo": "REPLACE",
            "token_env": "GITEA_TOKEN",
        },
    }
    if openagents_manifest is not None:
        request["openagents"] = openagents_manifest
    config = {
        "data": {
            "tasks": "tasks.jsonl",
            "train": "train.txt",
            "test": "final-round.txt",
            "step_size": info["sessions_per_round"],
            "shuffle": False,
        },
        "task_info": "task-info",
        "template": str(ROOT / "hermit/adapter/templates/ahe_beta"),
        "agent": {
            "name": str(HERE / "utils/acting.py") + ":AheHarborAgent",
            "concurrency": info["max_parallel_users"],
            "params": {
                "tasks_dir": str(cohort / "tasks"),
                "jobs_dir": str(cohort / "jobs"),
                "harbor": str(Path(harbor).resolve()),
                "binding": str(binding),
                "delivery_repo": str(Path(delivery_repo).resolve()),
                "run_id": run_id,
                "model": {
                    "model": actor_model,
                    "base_url": actor_url,
                    "api_key_env": "ACTOR_API_KEY",
                },
                "codex_login": False,
                "codex_version": "0.164.0",
                "agent_timeout_sec": None,
                "environment": {
                    "type": "docker",
                    "delete": True,
                    "kwargs": {"auto_mounting": True},
                },
                "acting_agent": {
                    "import_path": "hermit_user_codex:HermitCodex",
                    "pythonpath": [
                        str(HERE / "utils"),
                        str(Path(ahe_root).resolve() / "backend"),
                    ],
                    "kwargs": {
                        **(
                            {
                                "config": {
                                    "model_provider": "gateway",
                                    "model_providers": {
                                        "gateway": {
                                            "name": "gateway",
                                            "base_url": actor_url,
                                            "env_key": "OPENAI_API_KEY",
                                            "wire_api": "responses",
                                            "supports_websockets": False,
                                        }
                                    },
                                }
                            }
                            if http_only
                            else {}
                        ),
                        "user": {
                            "simulate": "ahe_beta.integrations.hermit.session:simulate",
                            "wait_s": 3600,
                        },
                    },
                },
            },
        },
        "judge": {"path": str(ROOT / "hermit/harbor/judge.py"), "name": "score"},
        "evolver": {
            "name": "ahe_beta",
            "params": {
                "delivery_config": "delivery.json",
                **(
                    {"openagents_manifest": "openagents.json"}
                    if openagents_manifest is not None
                    else {}
                ),
            },
        },
        "evolver_access": {"send_material_out": True},
        "sandbox": {"enforce": "required"},
        "environment": {"runner": "host"},
        "trainer": {
            "run_name": run_id,
            "output_dir": str(cohort / "runs"),
            "epochs": 1,
            "call_timeout_min": 1440,
            "budget": {"hours": 168},
        },
    }
    (cohort / "task-info").mkdir(exist_ok=True)
    (cohort / "task-info/README.md").write_text(
        "# AHE adapter evolution\n\nEach step is one complete user cohort. Preserve direct and interactive use.\n"
        "The final split is a same-cohort observation round, not a held-out benchmark.\n"
    )
    for name, value in [
        ("delivery.json", json.dumps(request, indent=2) + "\n"),
        ("run.yaml", yaml.safe_dump(config, sort_keys=False)),
    ]:
        path = cohort / name
        if path.exists():
            raise FileExistsError(path)
        path.write_text(value)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "cohort",
        "ahe-root",
        "ahe-python",
        "harbor",
        "delivery-repo",
        "run-id",
        "actor-model",
        "actor-url",
        "openagents-repository",
        "openagents-commit",
        "openagents-checkout",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument(
        "--http-only",
        action="store_true",
        help="Responses HTTP for gateways without WebSocket support",
    )
    prepare(**vars(parser.parse_args()))


if __name__ == "__main__":
    main()
