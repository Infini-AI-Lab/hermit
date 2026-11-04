#!/usr/bin/env python3
"""Write seed_adapter/ beside this file: the coevoskills-adapter template with SkillOpt's initial OfficeQA skill
(../utils/seed_adapter/ADAPTER.md, written by ../utils/prepare.py) as its ADAPTER.md and no Skill package yet, so the
run starts from the text the other methods start from.

    python examples/officeqa/coevoskills_adapter/make_seed.py
"""
from pathlib import Path
import shutil

import hermit
from hermit.spec import check_spec

HERE = Path(__file__).resolve().parent
TEMPLATE = Path(hermit.__file__).resolve().parent / "adapter" / "templates" / "coevoskills_adapter"
out = HERE / "seed_adapter"
if out.exists():
    raise SystemExit(f"{out} exists: remove it to write it again")
shutil.copytree(TEMPLATE, out, ignore=shutil.ignore_patterns("__pycache__"))
(out / "ADAPTER.md").write_bytes((HERE.parent / "utils" / "seed_adapter" / "ADAPTER.md").read_bytes())
result = check_spec(out, TEMPLATE, deploy=True)
if not result.passed:
    raise SystemExit(f"the seed fails the template check: {[i.__dict__ for i in result.issues]}")
print(f"wrote {out}")
