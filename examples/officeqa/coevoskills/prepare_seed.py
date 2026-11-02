"""Prepare only the fixed OfficeQA seed context over the CoEvoSkills template."""
from __future__ import annotations

from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
UTILS = HERE.parent / "utils"


def prepare():
    seed = HERE / "common_seed"
    shutil.copytree(ROOT / "hermit/adapter/templates/coevoskills", seed, dirs_exist_ok=True)
    (seed / "ADAPTER.md").write_bytes((UTILS / "seed_adapter/ADAPTER.md").read_bytes())
    print("Prepared the CoEvoSkills adapter seed from the original OfficeQA context.")


if __name__ == "__main__":
    prepare()
