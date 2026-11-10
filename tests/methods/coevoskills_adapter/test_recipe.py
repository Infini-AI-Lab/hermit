"""The OfficeQA fixture/seed commands publish only complete, reusable outputs."""

import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hermit.task import load_callable


RECIPE = Path(__file__).resolve().parents[3] / "examples/officeqa/coevoskills_adapter"


def test_fixture_command_can_retry_after_build_failure(tmp_path, monkeypatch):
    main = load_callable("prepare.py:main", RECIPE)
    utils = tmp_path / "utils"
    utils.mkdir()
    (utils / "data.json").write_text("{}")
    monkeypatch.setitem(main.__globals__, "UTILS", utils)
    dest = tmp_path / "data"
    monkeypatch.setattr(sys, "argv", ["prepare.py", "--output", str(dest)])

    def failed_build(staged, **kwargs):
        (staged / "image").mkdir(parents=True)
        raise subprocess.CalledProcessError(1, ["docker", "build"])

    monkeypatch.setitem(main.__globals__, "prepare", failed_build)
    with pytest.raises(subprocess.CalledProcessError):
        main()
    assert not dest.exists()

    def successful_build(staged, **kwargs):
        (staged / "packages").mkdir(parents=True)
        return 1

    monkeypatch.setitem(main.__globals__, "prepare", successful_build)
    main()
    assert (dest / "packages").is_dir()


def test_missing_initial_context_leaves_no_partial_seed(tmp_path):
    script = tmp_path / "coevoskills_adapter" / "make_seed.py"
    script.parent.mkdir()
    shutil.copy2(RECIPE / script.name, script)
    with pytest.raises(FileNotFoundError, match="ADAPTER.md"):
        runpy.run_path(str(script), run_name="__main__")
    assert not (script.parent / "seed_adapter").exists()
