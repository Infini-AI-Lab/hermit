"""prepare.py on a cohort as AHE-Beta's exporter writes it (a stand-in: cohort.json and rows of tasks.jsonl, no task
directories) and the example's template under check_spec; AHE-Beta's own delivery Evolver's configuration is
test_delivery.py's."""

import json
from pathlib import Path
import shutil
import sys

import pytest
import yaml

from hermit.config import Config
from hermit.spec import check_spec

EXAMPLE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(EXAMPLE))
from prepare import model, prepare  # noqa: E402

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
    for hooks in ({"Stop": {}}, {"PostToolUse": [{"hooks": [{"type": "command", "command": "  "}]}]}):
        (candidate / ".codex/hooks.json").write_text(json.dumps({"hooks": hooks}))   # Hermit refuses to load them:
        assert not check_spec(candidate, TEMPLATE).passed                         # every session would fail


def test_the_operators_default_git_ignore_file_is_not_read_by_the_tests(tmp_path, monkeypatch):
    """Git reads ~/.config/git/ignore (or $XDG_CONFIG_HOME's) without any configuration file naming it."""
    (tmp_path / "xdg/git").mkdir(parents=True)
    (tmp_path / "xdg/git/ignore").write_text(".codex/\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    prepare(cohort=cohort(tmp_path), ahe_root=tmp_path, run_id="unit")      # the tests' own excludes file stands


def test_an_instruction_holding_a_unicode_line_separator_is_one_row(tmp_path):
    """The exporter writes JSON lines unescaped: str.splitlines cut a row at U+2028, and Hermit's task loader would
    cut the public row there too."""
    root = cohort(tmp_path)
    rows = [json.loads(line) for line in (root / "tasks.jsonl").read_text().splitlines()]
    rows[0]["instruction"] = "first part\u2028second part"
    (root / "tasks.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    prepare(cohort=root, ahe_root=tmp_path, run_id="unit")
    lines = (root / "rows.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["instruction"] == "first part\u2028second part"


def test_rows_hold_no_persona_and_one_step_is_one_round(tmp_path):
    root = cohort(tmp_path)
    prepare(cohort=root, ahe_root=tmp_path, run_id="unit")
    rows = [json.loads(line) for line in (root / "rows.jsonl").read_text().splitlines()]
    assert all(set(r) == {"id", "harbor_task", "instruction"} for r in rows)       # what the learner reads
    assert "sr-secret" not in (root / "rows.jsonl").read_text()
    assert json.loads((root / "assignments.json").read_text())["r01-s01-t1"]["slot"] == 1
    cfg = Config.load(root / "run.yaml")
    data, agent = cfg.data["data"], cfg.data["agent"]
    assert data["step_size"] == 2 and data["shuffle"] is False and data["test"] == "rows-final-round.txt"
    assert (root / "rows-train.txt").read_text().split() == ["r01-s00-t0", "r01-s01-t1"]
    assert agent["name"].endswith("utils/user.py:UserCodexAgent") and agent["params"]["base"] == "codex"
    user = agent["params"]["user"]
    assert user["simulate"] == "ahe_user:simulate" and user["assignments"] == "assignments.json"
    assert user["params"]["model"] == {"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt", "effort": "low",
                                       "unapplied": ["temperature"], "shared_plan": True}   # the actor holds the plan
    assert cfg.data["judge"]["name"] == "user" and cfg.data["evolver"]["name"] == "codex"
    assert cfg.data["environment"]["harbor"]["environment"]["mounts"][0]["target"] == "/opt/hermit-codex"


def companion_cohort(tmp_path: Path, per_round: int) -> Path:
    """Two rounds of `per_round` sessions, the last task of each round running a service in a companion container."""
    root = tmp_path / "cohort"
    root.mkdir()
    (root / "cohort.json").write_text(json.dumps({**COHORT, "sessions_per_round": per_round}))
    rows, splits = [], {1: [], 2: []}
    for round_no in (1, 2):
        for slot in range(per_round):
            placement = "companion" if slot == per_round - 1 else "task"
            row = {"id": f"r0{round_no}-s0{slot}-t{slot}", "harbor_task": f"t{slot}", "instruction": f"task {slot}",
                   "ahe_session": {"round": round_no, "slot": slot, "persona": {"id": "p"},
                                   "environment": {"services": [{"id": "svc", "placement": placement}]}}}
            rows.append(row)
            splits[round_no].append(row["id"])
    (root / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (root / "train.txt").write_text("\n".join(splits[1]) + "\n")
    (root / "final-round.txt").write_text("\n".join(splits[2]) + "\n")
    return root


def test_sessions_whose_task_needs_a_companion_container_are_left_out_and_a_step_is_what_remains(tmp_path):
    """Such a session would run the whole conversation, then fail at AHE-Beta's finalize (a service a Hermit
    environment does not reach): it is left out before the run, and one step stays one round."""
    root = companion_cohort(tmp_path, 3)
    prepare(cohort=root, ahe_root=tmp_path, run_id="unit")
    data = yaml.safe_load((root / "run.yaml").read_text())["data"]
    assert data["step_size"] == 2 and data["train"] == "rows-train.txt"
    assert (root / "rows-train.txt").read_text().split() == ["r01-s00-t0", "r01-s01-t1"]
    assert (root / "rows-final-round.txt").read_text().split() == ["r02-s00-t0", "r02-s01-t1"]
    assert json.loads((root / "left-out.json").read_text())["sessions"] == ["r01-s02-t2", "r02-s02-t2"]
    assert "t2" not in (root / "rows.jsonl").read_text() and "r01-s02-t2" not in (root / "assignments.json").read_text()


def test_rounds_that_would_keep_different_numbers_of_sessions_are_refused(tmp_path):
    root = companion_cohort(tmp_path, 3)
    rows = [json.loads(line) for line in (root / "tasks.jsonl").read_text().splitlines()]
    rows[-1]["ahe_session"]["environment"]["services"][0]["placement"] = "task"     # round 2 keeps all three
    (root / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(ValueError, match="one step cannot be one round"):
        prepare(cohort=root, ahe_root=tmp_path, run_id="unit")


def test_a_cohort_with_a_round_that_would_keep_no_session_is_refused_before_anything_is_written(tmp_path):
    """Counting only the rounds that keep a session would see one even round and accept it."""
    root = companion_cohort(tmp_path, 2)
    rows = [json.loads(line) for line in (root / "tasks.jsonl").read_text().splitlines()]
    for row in rows[2:]:                                 # round 2: both of its sessions need a companion container
        row["ahe_session"]["environment"]["services"][0]["placement"] = "companion"
    (root / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(ValueError, match="one step cannot be one round"):
        prepare(cohort=root, ahe_root=tmp_path, run_id="unit")
    assert not (root / "rows.jsonl").exists() and not (root / "run.yaml").exists()


def test_budgets_that_leave_codex_no_time_before_the_review_are_refused(tmp_path):
    root = cohort(tmp_path)
    rows = [json.loads(line) for line in (root / "tasks.jsonl").read_text().splitlines()]
    for row in rows:                                     # as the exporter writes `--agent-seconds 600`
        row["ahe_session"]["scenario"]["workspace"] = {"params": {"execution": {"task_budget_seconds": 600}}}
    (root / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for review in (600, 300):                            # Harbor's 60 s margin first: 600 - 60 - 300 is 240 s
        with pytest.raises(ValueError, match="--review-seconds"):
            prepare(cohort=root, ahe_root=tmp_path, run_id="unit", review_seconds=review)
    with pytest.raises(ValueError, match="number of seconds"):
        prepare(cohort=root, ahe_root=tmp_path, run_id="unit", review_seconds=float("nan"))
    prepare(cohort=root, ahe_root=tmp_path, run_id="unit", review_seconds=240)
    assert yaml.safe_load((root / "run.yaml").read_text())["agent"]["params"]["user"]["review_s"] == 240


def test_a_run_file_is_published_whole(tmp_path, monkeypatch):
    """run.yaml marks a finished preparation: one cut short by a failure must not mark it."""
    root = cohort(tmp_path)
    real = Path.write_text

    def cut_short(self, text, *args, **kwargs):          # half of it written, then the disk is full
        if self.name in ("run.yaml", ".run.yaml.tmp"):
            real(self, text[: len(text) // 2], *args, **kwargs)
            raise OSError("no space left on device")
        return real(self, text, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", cut_short)
    with pytest.raises(OSError):
        prepare(cohort=root, ahe_root=tmp_path, run_id="unit")
    assert not (root / "run.yaml").exists()
    monkeypatch.setattr(Path, "write_text", real)
    prepare(cohort=root, ahe_root=tmp_path, run_id="unit")       # done again, whole
    assert yaml.safe_load((root / "run.yaml").read_text())["data"]["step_size"] == 2


def test_an_interrupted_preparation_is_done_again_whole(tmp_path):
    """run.yaml is written last: without it, what an earlier run left is rewritten; with it, nothing is."""
    root = cohort(tmp_path)
    (root / "rows.jsonl").write_text("partial\n")
    prepare(cohort=root, ahe_root=tmp_path, run_id="unit")
    assert "partial" not in (root / "rows.jsonl").read_text() and (root / "run.yaml").is_file()
    with pytest.raises(FileExistsError):
        prepare(cohort=root, ahe_root=tmp_path, run_id="unit")


def test_the_users_model_shares_the_plan_only_with_an_actor_on_its_login(tmp_path):
    """A reference keeps its effort; an actor reached by a reference holds no login's plan for the user to share."""
    root = cohort(tmp_path)
    prepare(cohort=root, ahe_root=tmp_path, run_id="unit", actor_model="fireworks/accounts/fireworks/models/glm-5p3")
    user = yaml.safe_load((root / "run.yaml").read_text())["agent"]["params"]["user"]
    assert "shared_plan" not in user["params"]["model"]
    assert model("vllm/m", "low", cli=True) == {"ref": "vllm/m", "effort": "low"}
