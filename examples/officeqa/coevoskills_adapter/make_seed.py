#!/usr/bin/env python3
"""Write seed_adapter/ beside this file: the coevoskills-adapter template with SkillOpt's initial OfficeQA skill
(../utils/seed_adapter/ADAPTER.md, written by ../utils/prepare.py) as its ADAPTER.md and no Skill package yet, so the
run starts from the text the other methods start from.

    python examples/officeqa/coevoskills_adapter/make_seed.py
"""
from pathlib import Path
import shutil
import tempfile

import hermit
from hermit.spec import check_spec

HERE = Path(__file__).resolve().parent
TEMPLATE = Path(hermit.__file__).resolve().parent / "adapter" / "templates" / "coevoskills_adapter"
out = HERE / "seed_adapter"
if out.exists():
    raise SystemExit(f"{out} exists: remove it to write it again")
initial = (HERE.parent / "utils" / "seed_adapter" / "ADAPTER.md").read_bytes()
with tempfile.TemporaryDirectory(dir=HERE) as tmp:
    staged = Path(tmp) / "seed"
    shutil.copytree(TEMPLATE, staged, ignore=shutil.ignore_patterns("__pycache__"))
    (staged / "ADAPTER.md").write_bytes(initial)
    result = check_spec(staged, TEMPLATE, deploy=True)
    if not result.passed:
        raise SystemExit(f"the seed fails the template check: {[i.__dict__ for i in result.issues]}")
    staged.rename(out)
print(f"wrote {out}")
