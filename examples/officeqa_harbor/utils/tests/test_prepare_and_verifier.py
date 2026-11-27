"""OfficeQA through Harbor's own code: the baseline config, prepare.py's rewrite of Harbor's tasks (the agent's
network, the image, the paper's agent prompt, no answer source in the metadata, no oracle), how it replaces the last
successful setup (stand-ins for its clone and its image build), and the verifier the tasks run
(utils/verifier/judge.py), with a stand-in for the OfficeQA reward.py the tasks carry. No network, no Harbor."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[4]
UTILS = ROOT / "examples" / "officeqa_harbor" / "utils"


@pytest.mark.parametrize("profile, agent_name, model", [("codex", "codex", "gpt-5.6-luna"),
                                                       ("claude", "claude-code", "claude-haiku-5-5")])
def test_a_baseline_config_runs_its_cli_in_harbors_environments_with_the_tasks_verifier(profile, agent_name, model):
    from hermit.config import Config
    config = Config.load(ROOT / "examples" / "officeqa_harbor" / "baseline" / f"{profile}.yaml")
    agent = config["agent"]                       # loaded: the CLI's agent, its model and its settings as params
    assert agent["name"] == agent_name and agent["params"]["model"] == model
    assert agent["params"]["web_search"] is False  # a hosted search runs past the network allowlist
    assert config["environment"]["harbor"]["benchmark"] == "../utils/benchmark.yaml"
    assert (config["judge"]["path"], config["judge"]["name"]) == ("hermit.task.verify", "environment")


def test_the_splits_are_skillopts_50_24_172_of_the_locked_tasks_and_the_smoke_is_validation():
    lock = set(json.loads((UTILS / "dataset.lock").read_text())["tasks"])
    split = {name: (UTILS / "splits" / f"{name}.txt").read_text().split() for name in ("train", "val", "test", "smoke")}
    assert [len(split[n]) for n in ("train", "val", "test")] == [50, 24, 172]
    assert set(split["train"]) | set(split["val"]) | set(split["test"]) == lock            # every task, once
    assert len(set(split["train"]) | set(split["val"]) | set(split["test"])) == 246
    assert split["smoke"] and set(split["smoke"]) <= set(split["val"])                     # no run's test row


def load_prepare():
    spec = importlib.util.spec_from_file_location("officeqa_harbor_prepare", UTILS / "prepare.py")
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    return prepare


def test_prepare_limits_the_agents_network_pins_the_image_and_drops_the_answers_source():
    """Harbor's OfficeQA tasks declare `allow_internet = true`: the run's isolation comes from this rewrite."""
    prepare = load_prepare()
    toml = tomllib.loads(prepare.localize(
        '[metadata]\nsource_id = "UID1"\nsource_files = ["treasury_bulletin_1999_03.txt"]\nsource_docs = ["https://x"]\n\n'
        '[verifier]\ntimeout_sec = 300.0\n\n[agent]\ntimeout_sec = 1800.0\n\n'
        '[environment]\ndocker_image = "docker.io/x/officeqa-corpus:latest"\nallow_internet = true\n'))
    assert toml["agent"]["network_mode"] == "allowlist"
    assert toml["agent"]["allowed_hosts"] == ["chatgpt.com", "auth.openai.com"]
    assert toml["verifier"]["network_mode"] == "no-network"
    assert toml["environment"]["docker_image"] == prepare.LOCAL_IMAGE
    assert toml["metadata"] == {"source_id": "UID1"}                        # no file or page that holds the answer
    with pytest.raises(ValueError):
        prepare.localize("[agent]\n")


def test_the_image_keeps_no_codex_home_from_its_build():
    """The build's `codex --version` leaves a temp dir in ~/.codex; in a trial Codex then warns on stderr that it
    cannot move it (overlayfs), Harbor reads that warning as the installed version, and it installs Codex again over
    the network in every trial, past the setup timeout."""
    run = (UTILS / "Dockerfile").read_text().split("\nRUN ", 1)[1]
    assert run.index("codex --version") < run.index("/root/.codex")
    assert "rm -rf" in run[run.index("codex --version"):]


SOURCE_TOML = """version = "1.0"

[metadata]
source_id = "UID000{n}"
source_files = "treasury_bulletin_1944_01.txt"
difficulty = "hard"

[verifier]
timeout_sec = 300.0

[agent]
timeout_sec = 1800.0

[environment]
docker_image = "benediktstroebl/officeqa-corpus:latest"
"""


@pytest.fixture
def prepare_main(tmp_path, monkeypatch):
    """prepare.py's main() writing to tmp_path/utils, with two stand-ins: its clone of harbor-datasets (two tasks in
    Harbor's form, the first one's expected answer `answer`) and `docker build` (failing when `build_fails`). The
    framework's own `hermit.harbor.dataset` index, check and lock run as subprocesses."""
    prepare = load_prepare()
    here = tmp_path / "utils"
    shutil.copytree(UTILS / "verifier", here / "verifier")
    reward = b"def score_answer(gold, predicted, tolerance=0.0):\n    return 1.0\n"
    monkeypatch.setattr(prepare, "HERE", here)
    monkeypatch.setattr(prepare, "REWARD_BLOB", prepare.blob(reward))
    real_run = subprocess.run

    def main(*argv, answer="1", build_fails=False):
        def run(cmd, *args, **kwargs):
            if cmd[0] == "git":
                if cmd[1] == "clone":
                    for n in (1, 2):
                        task = Path(cmd[-1]) / prepare.DATASET / f"officeqa-uid000{n}"
                        (task / "tests").mkdir(parents=True)
                        (task / "environment" / "resources").mkdir(parents=True)
                        (task / "environment" / "resources" / "manifest.json").write_text("{}")
                        (task / "task.toml").write_text(SOURCE_TOML.format(n=n))
                        (task / "instruction.md").write_text("Write the answer to /app/answer.txt.")
                        (task / "tests" / "config.json").write_text(json.dumps(
                            {"uid": f"UID000{n}", "question": f"Question {n}?",
                             "expected_answer": answer if n == 1 else "2"}))
                        (task / "tests" / "reward.py").write_bytes(reward)
                return subprocess.CompletedProcess(cmd, 0)
            if cmd[0] == "docker":
                if build_fails:
                    raise subprocess.CalledProcessError(1, cmd)
                return subprocess.CompletedProcess(cmd, 0)
            return real_run(cmd, *args, **kwargs)
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(sys, "argv", ["prepare.py", *argv])
        try:
            prepare.main()
        finally:
            monkeypatch.setattr(subprocess, "run", real_run)
    return here, main


def setup_files(here: Path) -> dict:
    """What a run reads of a prepare: the tasks, their index and the lock, path by path."""
    files = [here / "tasks.jsonl", here / "dataset.lock", *sorted((here / "tasks").rglob("*"))]
    return {p.relative_to(here).as_posix(): p.read_bytes() for p in files if p.is_file()}


def assert_usable(here: Path):
    """The setup as a run takes it: the index names every task directory and the tasks match the lock."""
    rows = [json.loads(line) for line in (here / "tasks.jsonl").read_text().splitlines()]
    assert sorted(r["harbor_task"] for r in rows) == sorted(p.name for p in (here / "tasks").iterdir())
    check = [sys.executable, "-m", "hermit.harbor.dataset", "check", str(here / "tasks"), "--lock", str(here / "dataset.lock")]
    assert subprocess.run(check, capture_output=True).returncode == 0
    assert not (here / ".staging").exists()


def test_a_failed_image_build_keeps_the_last_successful_setup(prepare_main):
    """A rebuild writes the new tasks aside and replaces the previous ones only after the image is built, so a run
    started meanwhile, or after the build failed, still reads the last setup that succeeded."""
    here, main = prepare_main
    main()
    before = setup_files(here)
    with pytest.raises(subprocess.CalledProcessError):
        main("--relock", answer="9", build_fails=True)     # new answers and a new lock, then the build fails
    assert setup_files(here) == before
    assert_usable(here)


def test_tasks_that_differ_from_the_lock_keep_the_last_successful_setup(prepare_main):
    here, main = prepare_main
    main()
    before = setup_files(here)
    with pytest.raises(SystemExit, match="previous setup is kept"):
        main(answer="9")
    assert setup_files(here) == before
    assert_usable(here)


def test_a_successful_prepare_replaces_the_setup(prepare_main):
    here, main = prepare_main
    main()
    before = setup_files(here)
    main("--relock", answer="9")
    after = setup_files(here)
    assert after != before and json.loads(after["tasks/officeqa-uid0001/tests/config.json"])["expected_answer"] == "9"
    assert_usable(here)
    assert not (here / "tasks" / "officeqa-uid0001" / "environment" / "resources" / "manifest.json").exists()


def test_the_staged_setup_holds_answers_so_git_and_docker_ignore_it():
    assert ".staging/" in (UTILS / ".gitignore").read_text().split()
    assert ".staging/" in (UTILS / ".dockerignore").read_text().split()


def test_prepare_asks_in_the_papers_agent_prompt_without_web_search():
    prepare = load_prepare()
    text = prepare.instruction({"uid": "UID1", "question": "What were the receipts in 1944?"})
    assert text.startswith("You are an agent that is an expert in answering questions related to the U.S treasury")
    assert "located in the folder /app/corpus/" in text and "web search" not in text
    assert "<FINAL_ANSWER>\n[value]\n</FINAL_ANSWER>" in text and "FAILURE CONDITION" in text
    assert text.rstrip().endswith("Question: What were the receipts in 1944?")
    with pytest.raises(ValueError):
        prepare.instruction({"uid": "UID2", "question": " "})
    assert prepare.blob(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"       # git's empty blob


# the two functions judge.py imports from databricks/officeqa's reward.py at 2a2c3b0: `extract_final_answer` as that
# file has it (the first tag; ValueError on an empty text or tag), `score_answer` reduced to a single number, 0 for an
# answer with none (its text branch), ValueError on an empty answer; the real scorer is tested below when prepare.py
# has written the tasks
STUB_REWARD = """import re
def extract_final_answer(text):
    if not text:
        raise ValueError("Cannot extract from empty text")
    match = re.search(r"<FINAL_ANSWER>\\s*(.*?)\\s*</FINAL_ANSWER>", text, re.DOTALL | re.IGNORECASE)
    if match:
        content = match.group(1).strip()
        if not content:
            raise ValueError("FINAL_ANSWER tags are empty")
        return content
    return text
def score_answer(gold, predicted, tolerance=0.0):
    if not predicted:
        raise ValueError("Predicted answer cannot be empty")
    found = re.findall(r"-?[0-9][0-9,]*[.]?[0-9]*", predicted)
    if not found:
        return 0.0
    expected = float(gold.replace(",", ""))
    return 1.0 if abs(float(found[0].replace(",", "")) - expected) <= tolerance * abs(expected) else 0.0
"""


@pytest.fixture
def verifier(tmp_path):
    """The container's judge (utils/verifier/judge.py) beside a stand-in for the OfficeQA reward.py (its two functions'
    signatures; the real file is the tasks' own, not vendored), its paths moved under tmp_path.
    That the Codex agent's log holds last_message.txt, which the run hands the verifier as /logs/agent, is
    tests/core/test_agents.py's."""
    shutil.copy(UTILS / "verifier" / "judge.py", tmp_path / "judge.py")
    (tmp_path / "reward.py").write_text(STUB_REWARD)               # the judge loads it by path
    spec = importlib.util.spec_from_file_location("officeqa_harbor_verifier", tmp_path / "judge.py")
    judge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(judge)
    judge.LOGS, judge.VERIFIER, judge.CONFIG = tmp_path / "agent", tmp_path / "verifier", tmp_path / "config.json"
    judge.LOGS.mkdir()
    judge.CONFIG.write_text(json.dumps({"expected_answer": "44,463"}))
    return judge


def verdict(judge):
    judge.main()
    return (json.loads((judge.VERIFIER / "reward.json").read_text()),
            (judge.VERIFIER / "feedback.txt").read_text().strip())


def test_the_verifier_scores_the_final_message_at_the_papers_thresholds(verifier):
    (verifier.LOGS / "last_message.txt").write_text("<REASONING>r</REASONING>\n<FINAL_ANSWER>44393</FINAL_ANSWER>")
    rewards, feedback = verdict(verifier)
    assert rewards == {"reward": 0.0, "reward_0.1pct": 0.0, "reward_1pct": 1.0, "reward_5pct": 1.0}
    assert (verifier.VERIFIER / "reward.txt").read_text() == "0\n"
    assert feedback == "FAIL: final answer '44393'" and "44,463" not in feedback and "44463" not in feedback


def test_the_verifier_reads_codexs_stream_when_the_log_has_no_last_message(verifier):
    """The stream's shape is Codex 0.164.0's `codex exec --json` (its stderr line first, then its events), as the
    OfficeQA runs kept it (examples/officeqa/utils/tests/test_harbor_judge.py records one)."""
    events = ["Reading additional input from stdin...",
              json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Searching."}}),
              json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "44,463"}})]
    (verifier.LOGS / "agent-codex.jsonl").write_text("\n".join(events))
    rewards, feedback = verdict(verifier)
    assert rewards["reward"] == 1.0 and feedback == "PASS: final answer '44,463' (no <FINAL_ANSWER> tag)"


def test_a_scorer_that_fails_leaves_no_reward(verifier, monkeypatch):
    def broken(*_):
        raise RuntimeError("a bug in the scorer")
    monkeypatch.setattr(verifier, "score_answer", broken)
    (verifier.LOGS / "last_message.txt").write_text("<FINAL_ANSWER>44463</FINAL_ANSWER>")
    assert verifier.main() == 1                       # no reward: the run records verifier_failed
    assert not (verifier.VERIFIER / "reward.txt").exists() and not (verifier.VERIFIER / "reward.json").exists()
    assert (verifier.VERIFIER / "feedback.txt").read_text() == "verifier error: RuntimeError\n"


def test_an_empty_final_answer_is_a_wrong_answer_not_a_verifier_failure(verifier):
    (verifier.LOGS / "last_message.txt").write_text("<FINAL_ANSWER> </FINAL_ANSWER>")   # reward.py raises ValueError
    rewards, feedback = verdict(verifier)
    assert set(rewards.values()) == {0.0} and feedback == "FAIL: final answer ''"


def test_a_task_without_an_expected_answer_is_a_verifier_failure(verifier):
    verifier.CONFIG.write_text(json.dumps({"expected_answer": " "}))
    (verifier.LOGS / "last_message.txt").write_text("<FINAL_ANSWER>44463</FINAL_ANSWER>")
    assert verifier.main() == 1 and not (verifier.VERIFIER / "reward.json").exists()


def test_the_verifier_scores_the_first_final_answer_as_the_papers_scorer_takes_it(verifier):
    final = "<FINAL_ANSWER>44393</FINAL_ANSWER> or <FINAL_ANSWER>44463</FINAL_ANSWER>"
    (verifier.LOGS / "last_message.txt").write_text(final)
    rewards, feedback = verdict(verifier)
    assert rewards["reward"] == 0.0 and feedback == "FAIL: final answer '44393'"


def test_a_reward_package_left_in_tests_does_not_replace_the_scorer(verifier):
    """In a fresh `python3 -I -S`, as test.sh runs it: a `reward` package an agent (root in the container) left in
    /tests, which Harbor copies the tests over, would win an import by name over the uploaded reward.py."""
    tests = verifier.LOGS.parent
    (tests / "reward").mkdir()
    (tests / "reward" / "__init__.py").write_text("def extract_final_answer(t): return t\ndef score_answer(*a): return 1.0\n")
    (verifier.LOGS / "last_message.txt").write_text("<FINAL_ANSWER>1</FINAL_ANSWER>")
    run = (f"import importlib.util; s = importlib.util.spec_from_file_location('judge', {str(tests / 'judge.py')!r}); "
           "j = importlib.util.module_from_spec(s); s.loader.exec_module(j); from pathlib import Path; "
           f"j.LOGS, j.VERIFIER, j.CONFIG = Path({str(verifier.LOGS)!r}), Path({str(verifier.VERIFIER)!r}), "
           f"Path({str(verifier.CONFIG)!r}); j.main()")
    subprocess.run([sys.executable, "-I", "-S", "-c", run], cwd=tests, check=True, capture_output=True)
    assert set(json.loads((verifier.VERIFIER / "reward.json").read_text()).values()) == {0.0}


def test_the_verifier_scores_no_final_message_zero(verifier):
    rewards, feedback = verdict(verifier)
    assert set(rewards.values()) == {0.0} and feedback == "FAIL: no final message"


REAL_REWARD = UTILS / "tasks" / "officeqa-uid0003" / "tests" / "reward.py"


@pytest.mark.skipif(not REAL_REWARD.is_file(), reason="needs the tasks prepare.py writes (their reward.py)")
@pytest.mark.parametrize("final, rewards", [
    ("<REASONING>summed</REASONING>\n<FINAL_ANSWER>\n44,463 million\n</FINAL_ANSWER>", (1.0, 1.0, 1.0, 1.0)),
    ("<FINAL_ANSWER>44393</FINAL_ANSWER>", (0.0, 0.0, 1.0, 1.0)),       # 0.16% off: right only from 1%
    ("The total is 44,463 million dollars.", (1.0, 1.0, 1.0, 1.0)),    # no tag: the whole message, its number found
    ("<FINAL_ANSWER>n/a</FINAL_ANSWER>", (0.0, 0.0, 0.0, 0.0)),        # no number: its text branch finds no match
])
def test_the_verifier_with_the_officeqa_scorer_itself(verifier, final, rewards):
    shutil.copy(REAL_REWARD, verifier.LOGS.parent / "reward.py")
    spec = importlib.util.spec_from_file_location("officeqa_harbor_verifier_real", verifier.LOGS.parent / "judge.py")
    judge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(judge)
    judge.LOGS, judge.VERIFIER, judge.CONFIG = verifier.LOGS, verifier.VERIFIER, verifier.CONFIG
    (judge.LOGS / "last_message.txt").write_text(final)
    got, _ = verdict(judge)
    assert tuple(got[k] for k in ("reward", "reward_0.1pct", "reward_1pct", "reward_5pct")) == rewards
