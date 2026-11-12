"""LAB-Bench's patch (utils/patches.py:drop_seed), which `python -m hermit.harbor.dataset prepare` runs on every
download: the setup.py each task's container runs at its start saves the shuffle's seed beside the letters, and the
right choice is the first one before the shuffle, so the seed names the answer. The task directories here are built
under tmp_path in the published layout (`<task>/environment/workspace/setup.py`), with a setup.py that saves its
mapping as the published one does."""
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


def task(root: Path, name: str, text: str) -> Path:
    setup = root / name / "environment" / "workspace" / "setup.py"
    setup.parent.mkdir(parents=True)
    setup.write_text(text, encoding="utf-8")
    return setup


def run(setup: Path, tmp_path: Path) -> dict:
    """The setup script as a container runs it; the mapping it saved for the verifier, where the agent can read it."""
    out = tmp_path / f"{setup.parents[2].name}-mapping.json"
    subprocess.run([sys.executable, "-I", str(setup), str(out)], check=True)
    return json.loads(out.read_text())


def answer_from_seed(saved: dict) -> str:
    """What an agent can do with a saved seed: replay the shuffle on the choices' positions; the right choice was at 0."""
    order = list(range(len(saved["mapping"])))
    random.Random(saved["seed"]).shuffle(order)
    return saved["mapping"][chr(ord("A") + order.index(0))]


def test_the_published_mapping_gives_the_answer_away(tmp_path):
    setup = task(tmp_path / "tasks", "figqa-0001", SETUP)
    assert answer_from_seed(run(setup, tmp_path)) == "the right choice"


def test_the_patched_setup_saves_the_letters_only(tmp_path):
    tasks = tmp_path / "tasks"
    setup = task(tasks, "figqa-0001", SETUP)
    assert load().drop_seed(tasks) == "1 setup.py patched, 0 already patched"
    saved = run(setup, tmp_path)
    assert set(saved) == {"mapping"}                       # what the verifier reads, and no seed
    assert sorted(saved["mapping"].values()) == sorted(["the right choice", "wrong 1", "wrong 2", "wrong 3", "wrong 4"])


def test_a_second_prepare_finds_the_tasks_patched(tmp_path):
    tasks = tmp_path / "tasks"
    first, second = task(tasks, "figqa-0001", SETUP), task(tasks, "figqa-0002", SETUP)
    patches = load()
    assert patches.drop_seed(tasks) == "2 setup.py patched, 0 already patched"
    patched = first.read_text()
    assert patches.drop_seed(tasks) == "0 setup.py patched, 2 already patched"
    assert first.read_text() == second.read_text() == patched


def test_a_setup_the_patch_does_not_know_is_refused_with_no_task_changed(tmp_path):
    tasks = tmp_path / "tasks"
    known = task(tasks, "figqa-0001", SETUP)                      # patched first, were the patch to write as it reads
    task(tasks, "figqa-0002", SETUP.replace('"seed": seed', '"seed": seed, "order": all_choices'))
    with pytest.raises(ValueError, match="figqa-0002"):
        load().drop_seed(tasks)
    assert known.read_text() == SETUP


def test_a_task_set_with_no_setup_is_refused(tmp_path):
    (tmp_path / "tasks" / "other-task" / "environment").mkdir(parents=True)
    with pytest.raises(ValueError, match="not LAB-Bench's tasks"):
        load().drop_seed(tmp_path / "tasks")
