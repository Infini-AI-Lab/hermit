"""prepare.py on a cohort as AHE-Beta's exporter writes it (a stand-in: cohort.json and two rows of tasks.jsonl, no task
directories), the example's template under check_spec, and AHE-Beta's own delivery Evolver loaded from the example."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

from hermit.agent.evolver.loader import find, load_class
from hermit.config import Config
from hermit.spec import check_spec

EXAMPLE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(EXAMPLE))
from prepare import prepare  # noqa: E402

TEMPLATE = EXAMPLE / "template"
COHORT = {"source_config_sha256": "a" * 64, "sessions_per_round": 2, "max_parallel_users": 2,
          "roles": {"triager": 1, "architect": 1, "divergent": 1, "owner_mode": "mechanical"}}


def cohort(tmp_path: Path) -> Path:
    root = tmp_path / "cohort"
    root.mkdir()
    (root / "cohort.json").write_text(json.dumps(COHORT))
    rows = [{"id": f"r01-s0{i}-t{i}", "harbor_task": f"t{i}", "instruction": f"task {i}", "agent_timeout_sec": 60,
             "ahe_session": {"round": 1, "slot": i, "persona": {"id": "sr-secret"}, "scenario": {"description": "x"}}}
            for i in range(2)]
    (root / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (root / "train.txt").write_text("r01-s00-t0\nr01-s01-t1\n")
    (root / "final-round.txt").write_text("r01-s00-t0\n")
    return root


def test_the_template_checks_and_refuses_a_conditional_stop_hook(tmp_path):
    shutil.copytree(TEMPLATE, tmp_path / "adapter")
    candidate = tmp_path / "adapter"
    assert check_spec(candidate, TEMPLATE).passed
    (candidate / ".codex/hooks.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"matcher": "anything", "hooks": [{"type": "command", "command": "true"}]}]}}))
    assert not check_spec(candidate, TEMPLATE).passed


def test_rows_hold_no_persona_and_one_step_is_one_round(tmp_path):
    root = cohort(tmp_path)
    prepare(cohort=root, ahe_root=tmp_path, run_id="unit")
    rows = [json.loads(line) for line in (root / "rows.jsonl").read_text().splitlines()]
    assert all(set(r) == {"id", "harbor_task", "instruction"} for r in rows)       # what the learner reads
    assert "sr-secret" not in (root / "rows.jsonl").read_text()
    assert json.loads((root / "assignments.json").read_text())["r01-s01-t1"]["slot"] == 1
    cfg = Config.load(root / "run.yaml")
    data, agent = cfg.data["data"], cfg.data["agent"]
    assert data["step_size"] == 2 and data["shuffle"] is False and data["test"] == "final-round.txt"
    assert agent["name"].endswith("utils/user.py:UserCodexAgent") and agent["params"]["base"] == "codex"
    user = agent["params"]["user"]
    assert user["simulate"] == "ahe_user:simulate" and user["assignments"] == "assignments.json"
    assert user["params"]["model"] == {"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt", "effort": "low",
                                       "unapplied": ["temperature"]}
    assert cfg.data["judge"]["name"] == "user" and cfg.data["evolver"]["name"] == "codex"
    assert cfg.data["environment"]["harbor"]["environment"]["mounts"][0]["target"] == "/opt/hermit-codex"


def test_a_delivery_repository_adds_the_delivery_evolver_config(tmp_path):
    root = cohort(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "ahe"], check=True)
    prepare(cohort=root, ahe_root=tmp_path, run_id="unit", delivery_repo=tmp_path / "delivery")
    request = json.loads((root / "delivery.json").read_text())
    assert request["sessions_per_round"] == 2 and request["assignments"] == str(root / "assignments.json")
    assert request["tracker"]["token_env"] == "GITEA_TOKEN" and "token" not in request["tracker"]
    cfg = Config.load(root / "run-delivery.yaml")
    assert cfg.data["evolver"]["name"] == str(EXAMPLE / "delivery")
    assert load_class(find(cfg.data["evolver"]["name"])).__name__ == "AHEBetaEvolver"
