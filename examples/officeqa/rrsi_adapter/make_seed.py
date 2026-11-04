#!/usr/bin/env python3
"""Write seed_adapter/ beside this file: SkillOpt's initial OfficeQA skill (../utils/seed_adapter/ADAPTER.md, written
by ../utils/prepare.py) as an `rrsi-adapter` adapter, its ADAPTER.md and nothing else: H_0 is exactly the text the
GEPA, SkillOpt, Harness², Continual Harness and Meta-Harness Adapter runs start from.

    python examples/officeqa/rrsi_adapter/make_seed.py
"""
from pathlib import Path
import shutil

import hermit
from hermit.spec import check_spec

HERE = Path(__file__).resolve().parent
TEMPLATE = Path(hermit.__file__).resolve().parent / "adapter" / "templates" / "rrsi_adapter"
seed = (HERE.parent / "utils" / "seed_adapter" / "ADAPTER.md").read_text()
out = HERE / "seed_adapter"
if out.exists():
    shutil.rmtree(out)
shutil.copytree(TEMPLATE, out, ignore=shutil.ignore_patterns("__pycache__"))
(out / "ADAPTER.md").write_text(seed, encoding="utf-8")
result = check_spec(out, TEMPLATE, deploy=True)
assert result.passed, [i.__dict__ for i in result.issues]
print(f"wrote {out} ({len(seed)} chars of ADAPTER.md)")
