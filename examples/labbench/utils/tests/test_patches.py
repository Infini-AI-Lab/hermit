"""LAB-Bench's patch (utils/patches.py:patch_tasks), which `python -m hermit.harbor.dataset prepare` runs on every
download. The setup.py each task's container runs at its start saves the shuffle's seed beside the letters, and the
right choice is the first one before the shuffle, so the seed names the answer; the verifier runs /app/setup.py, the
agent's to rewrite, when the mapping is missing. The task directories here are built under tmp_path in the published
layout (`<task>/environment/workspace/setup.py`, `<task>/tests/test.sh`), with a setup.py that saves its mapping as
the published one does and a verifier holding the published fallback."""
from __future__ import annotations

import importlib.util
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parents[1] / "patches.py"

# The published setup.py's shuffle and the mapping it saves (its lines as the patch reads them), reduced to the part
# that matters: the choices with the right one first, a seed of its own, the letters' texts written for the verifier.
SETUP = '''import json, random, sys
ideal, distractors = "the right choice", ["wrong 1", "wrong 2", "wrong 3", "wrong 4"]
all_choices = [ideal] + distractors
seed = random.randint(0, 2**31 - 1)
rng = random.Random(seed)
rng.shuffle(all_choices)
letters = [chr(ord("A") + i) for i in range(len(all_choices))]
letter_mapping = {letters[i]: all_choices[i] for i in range(len(all_choices))}
mapping_data = {
        "mapping": letter_mapping,
        "seed": seed
        # NO "correct_letter" field - this is anti-cheating measure
    }
with open(sys.argv[1], "w") as f:
    json.dump(mapping_data, f, indent=2)
'''


def load():
    spec = importlib.util.spec_from_file_location("labbench_patches", PATCHES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The published verifier, reduced to its mapping checks: the fallback block as published, then the check after it;
# its log directory and mapping file are its arguments here.
VERIFIER = ('#!/bin/bash\nset -euo pipefail\nLOG_DIR="$1"\nmapping_file="$2"\nmkdir -p "$LOG_DIR"\n\n'
            + load().FALLBACK[1] + '\nif [[ ! -f "$mapping_file" ]]; then\n'
            '    echo "letter_mapping.json still missing after setup"\n    echo 0 > "${LOG_DIR}/reward.txt"\n'
            '    exit 1\nfi\necho 1 > "${LOG_DIR}/reward.txt"\n')


def task(root: Path, name: str, setup: str = SETUP, verifier: str | None = VERIFIER) -> Path:
    """A task directory of the published layout; its setup.py."""
    path = root / name / "environment" / "workspace" / "setup.py"
    path.parent.mkdir(parents=True)
    path.write_text(setup, encoding="utf-8")
    if verifier is not None:
        (root / name / "tests").mkdir()
        (root / name / "tests" / "test.sh").write_text(verifier, encoding="utf-8")
    return path


def run(setup: Path, tmp_path: Path) -> dict:
    """The setup script as a container runs it; the mapping it saved for the verifier, where the agent can read it."""
    out = tmp_path / f"{setup.parents[2].name}-mapping.json"
    subprocess.run([sys.executable, "-I", str(setup), str(out)], check=True)
    return json.loads(out.read_text())


def verify(test_sh: Path, mapping: Path, logs: Path) -> tuple[str, str]:
    """The verifier's reward and output for a mapping file that may be missing."""
    proc = subprocess.run(["bash", str(test_sh), str(logs), str(mapping)], capture_output=True, text=True)
    return (logs / "reward.txt").read_text().strip(), proc.stdout


def answer_from_seed(saved: dict) -> str:
    """What an agent can do with a saved seed: replay the shuffle on the positions; the right choice was at 0."""
    order = list(range(len(saved["mapping"])))
    random.Random(saved["seed"]).shuffle(order)
    return saved["mapping"][chr(ord("A") + order.index(0))]


def test_the_published_mapping_gives_the_answer_away(tmp_path):
    setup = task(tmp_path / "tasks", "figqa-0001")
    assert answer_from_seed(run(setup, tmp_path)) == "the right choice"


def test_the_patched_setup_saves_the_letters_only(tmp_path):
    tasks = tmp_path / "tasks"
    setup = task(tasks, "figqa-0001")
    assert load().patch_tasks(tasks) == "setup.py: 1 patched, 0 already patched; test.sh: 1 patched, 0 already patched"
    saved = run(setup, tmp_path)
    assert set(saved) == {"mapping"}                       # what the verifier reads, and no seed
    assert sorted(saved["mapping"].values()) == sorted(["the right choice", "wrong 1", "wrong 2", "wrong 3", "wrong 4"])


def test_the_patched_verifier_runs_no_setup_script_and_scores_a_missing_mapping_0(tmp_path):
    tasks = tmp_path / "tasks"
    task(tasks, "figqa-0001")
    load().patch_tasks(tasks)
    test_sh = tasks / "figqa-0001" / "tests" / "test.sh"
    assert "/app/setup.py" not in test_sh.read_text().replace("and /app/setup.py the agent's", "")
    reward, said = verify(test_sh, tmp_path / "missing.json", tmp_path / "logs-missing")
    assert reward == "0" and "Running setup.py" not in said and "still missing" in said
    (tmp_path / "mapping.json").write_text("{}")
    assert verify(test_sh, tmp_path / "mapping.json", tmp_path / "logs-present")[0] == "1"   # the checks after it run


def test_a_second_prepare_finds_the_tasks_patched(tmp_path):
    tasks = tmp_path / "tasks"
    first, second = task(tasks, "figqa-0001"), task(tasks, "figqa-0002")
    patches = load()
    assert patches.patch_tasks(tasks) == "setup.py: 2 patched, 0 already patched; test.sh: 2 patched, 0 already patched"
    patched = first.read_text(), (tasks / "figqa-0001" / "tests" / "test.sh").read_text()
    assert patches.patch_tasks(tasks) == "setup.py: 0 patched, 2 already patched; test.sh: 0 patched, 2 already patched"
    assert first.read_text() == second.read_text() == patched[0]
    assert (tasks / "figqa-0002" / "tests" / "test.sh").read_text() == patched[1]


def test_tasks_patched_by_the_seed_alone_get_the_verifier_patched(tmp_path):
    tasks = tmp_path / "tasks"
    patches = load()
    task(tasks, "figqa-0001", setup=SETUP.replace(patches.SEED[1], patches.SEED[2]))   # an earlier prepare's patch
    assert patches.patch_tasks(tasks) == "setup.py: 0 patched, 1 already patched; test.sh: 1 patched, 0 already patched"


@pytest.mark.parametrize("file", ["setup.py", "test.sh"])
def test_a_file_the_patch_does_not_know_is_refused_with_no_task_changed(tmp_path, file):
    tasks = tmp_path / "tasks"
    known = task(tasks, "figqa-0001")                      # patched first, were the patch to write as it reads
    if file == "setup.py":
        task(tasks, "figqa-0002", setup=SETUP.replace('"seed": seed', '"seed": seed, "order": all_choices'))
    else:
        task(tasks, "figqa-0002", verifier=VERIFIER.replace("python3 /app/setup.py ||", "python3 /app/setup.py -v ||"))
    with pytest.raises(ValueError, match=f"figqa-0002.*{file}"):
        load().patch_tasks(tasks)
    assert known.read_text() == SETUP
    assert (tasks / "figqa-0001" / "tests" / "test.sh").read_text() == VERIFIER


def test_a_task_without_its_verifier_is_refused(tmp_path):
    tasks = tmp_path / "tasks"
    task(tasks, "figqa-0001")
    task(tasks, "figqa-0002", verifier=None)
    with pytest.raises(ValueError, match="not those with"):
        load().patch_tasks(tasks)
    assert (tasks / "figqa-0001" / "environment" / "workspace" / "setup.py").read_text() == SETUP


def test_a_task_set_with_no_setup_is_refused(tmp_path):
    (tmp_path / "tasks" / "other-task" / "environment").mkdir(parents=True)
    with pytest.raises(ValueError, match="not LAB-Bench's tasks"):
        load().patch_tasks(tmp_path / "tasks")
