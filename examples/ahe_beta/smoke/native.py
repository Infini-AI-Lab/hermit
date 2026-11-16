"""One real Codex rollout with the native AHE user/reviewer and artifact collector.

Credentials come from ACTOR_API_KEY, USER_LLM_API_KEY and USER_LLM_BASE_URL.
The temporary Git commit is the frozen test adapter, not a source-repository commit.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "utils"))
from acting import AheHarborAgent
from binding import write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ahe-root", required=True, type=Path)
    parser.add_argument("--harbor", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--http-only",
        action="store_true",
        help="Use Responses HTTP for gateways without WebSocket support",
    )
    parser.add_argument(
        "--interaction", choices=["direct", "simulated_user"], default="direct"
    )
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    template = HERE.parents[2] / "hermit/adapter/templates/ahe_beta"
    adapter = root / "adapter"
    shutil.copytree(template, adapter)

    def git(*cmd):
        subprocess.run(
            ["git", "-C", str(adapter), *cmd], check=True, capture_output=True
        )

    git("init", "-b", "main")
    git("config", "user.name", "Integration fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("add", ".")
    git("commit", "-m", "Freeze the smoke adapter")
    write(
        root / "binding.json",
        adapter=adapter,
        repo=adapter,
        run_id=root.name,
        round_no=1,
    )
    task_dir = root / "tasks/native-demo"
    for directory in ("environment", "tests"):
        (task_dir / directory).mkdir(parents=True)
    instruction = "Create /app/answer.txt containing exactly the single line hello hermit. Read it back to verify the contents. Do not modify other files."
    (task_dir / "instruction.md").write_text(instruction)
    (task_dir / "environment/Dockerfile").write_text(
        "FROM ahe-hermit-smoke:local\nWORKDIR /app\n"
    )
    (task_dir / "task.toml").write_text(
        'schema_version = "1.4"\n[agent]\ntimeout_sec = 600\n[verifier]\ntimeout_sec = 60\n[environment]\ncpus = 1\nmemory_mb = 1024\nbuild_timeout_sec = 300\n'
    )
    (task_dir / "tests/test.sh").write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /logs/verifier\ntest -s /logs/agent/user-reward.txt\ncp /logs/agent/user-reward.txt /logs/verifier/reward.txt\n"
    )
    digest = hashlib.sha256(instruction.encode()).hexdigest()
    card = json.loads((args.ahe_root / "examples/personas/library.json").read_text())[0]
    artifacts = [
        {
            "id": "answer",
            "source": "workspace",
            "path": "answer.txt",
            "content_type": "text/plain",
            "required": True,
            "max_bytes": 1024,
        }
    ]
    row = {
        "id": "r01-s00-native-demo",
        "harbor_task": "native-demo",
        "agent_timeout_sec": 600,
        "build_timeout_sec": 300,
        "verifier_timeout_sec": 60,
        "ahe_session": {
            "task_id": "native-demo",
            "task_digest": digest,
            "task_kind": "ordinary",
            "round": 1,
            "slot": 0,
            "persona": card,
            "interaction": args.interaction,
            "environment": {"workdir": "/app", "patch": "not_applicable"},
            "scenario": {
                "description": instruction,
                "workspace": {
                    "kind": "external",
                    "params": {
                        "task_id": "native-demo",
                        "task_digest": digest,
                        "task_kind": "ordinary",
                        "interaction": args.interaction,
                        "evaluation_artifacts": artifacts,
                        "execution": {
                            "task_budget_seconds": 600,
                            "budget_source": "operator_quota",
                        },
                    },
                },
            },
        },
    }
    runner = AheHarborAgent(
        {
            "harbor": args.harbor,
            "tasks_dir": str(root / "tasks"),
            "jobs_dir": str(root / "jobs"),
            "binding": str(root / "binding.json"),
            "disk_gate_gb": 5,
            "model": {
                "model": args.model,
                "base_url": args.base_url,
                "api_key_env": "ACTOR_API_KEY",
            },
            "codex_version": "0.164.0",
            "codex_login": False,
            "agent_timeout_sec": None,
            "environment": {"delete": True, "kwargs": {"auto_mounting": True}},
            "acting_agent": {
                "import_path": "hermit_user_codex:HermitCodex",
                "pythonpath": [
                    str(HERE.parent / "utils"),
                    str(args.ahe_root / "backend"),
                ],
                "kwargs": {
                    **(
                        {
                            "config": {
                                "model_provider": "gateway",
                                "model_providers": {
                                    "gateway": {
                                        "name": "gateway",
                                        "base_url": args.base_url,
                                        "env_key": "OPENAI_API_KEY",
                                        "wire_api": "responses",
                                        "supports_websockets": False,
                                    }
                                },
                            }
                        }
                        if args.http_only
                        else {}
                    ),
                    "user": {
                        "simulate": "ahe_beta.integrations.hermit.session:simulate",
                        "wait_s": 300,
                    },
                },
            },
        },
        base=root,
    )
    result = runner.run(row, adapter)
    verdict = json.loads((Path(result.log) / "agent/user-verdict.json").read_text())
    summary = {
        "output": result.output,
        "log": str(result.log),
        "rating": verdict["rating"],
        "score_kind": verdict["score_kind"],
        "user_usage": verdict["usage"],
        "actor_tokens": result.extra.get("actor_tokens", result.tokens),
        "total_tokens": result.tokens,
    }
    (root / "report.json").write_text(json.dumps(summary, indent=2) + "\n")
    session = json.loads((Path(result.log) / "agent/user-session.json").read_text())
    assert session["workspace_evidence"]["evaluation_artifacts"]["complete"]
    assert session["workspace_evidence"]["actor_freeze"]["status"] == "frozen"
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
