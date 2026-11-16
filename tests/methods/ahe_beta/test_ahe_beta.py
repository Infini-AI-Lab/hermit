"""AHE's full-cohort schedule and adapter surface stay explicit and runnable."""

import json
from pathlib import Path
import shutil

from hermit.agent.evolver.loader import find, load_class
from hermit.config import Config
from hermit.isolate import host_pipeline
from hermit.spec import check_spec

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "hermit/adapter/templates/ahe_beta"


def test_template_deploys_and_refuses_conditional_stop_hooks(tmp_path):
    shutil.copytree(TEMPLATE, tmp_path / "adapter")
    candidate = tmp_path / "adapter"
    assert check_spec(candidate, TEMPLATE).passed
    (candidate / ".codex/hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "anything",
                            "hooks": [{"type": "command", "command": "true"}],
                        }
                    ]
                }
            }
        )
    )
    assert not check_spec(candidate, TEMPLATE).passed


def test_generated_configuration_keeps_one_cohort_per_step(tmp_path):
    from examples.ahe_beta.prepare import prepare

    (tmp_path / "tasks.jsonl").write_text("{}\n")
    (tmp_path / "cohort.json").write_text(
        json.dumps(
            {
                "source_config_sha256": "a" * 64,
                "roles": {
                    "triager": 4,
                    "architect": 1,
                    "divergent": 1,
                    "owner_mode": "mechanical",
                },
                "sessions_per_round": 8,
                "max_parallel_users": 4,
            }
        )
    )
    prepare(
        cohort=tmp_path,
        ahe_root=ROOT,
        ahe_python="/usr/bin/python3",
        harbor="/opt/venv/bin/harbor",
        delivery_repo=tmp_path / "delivery-repo",
        run_id="unit",
        actor_model="test-model",
        actor_url="https://gateway.invalid/v1",
        http_only=True,
    )
    cfg = Config.load(tmp_path / "run.yaml")
    assert cfg.data["data"]["step_size"] == 8 and cfg.data["data"]["shuffle"] is False
    assert cfg.data["data"]["test"] == "final-round.txt"
    assert host_pipeline(cfg).startswith("environment.runner: host")
    assert cfg.data["agent"]["params"]["agent_timeout_sec"] is None
    provider = cfg.data["agent"]["params"]["acting_agent"]["kwargs"]["config"][
        "model_providers"
    ]["gateway"]
    assert provider["supports_websockets"] is False
    assert load_class(find("ahe_beta")).__name__ == "AHEBetaEvolver"
