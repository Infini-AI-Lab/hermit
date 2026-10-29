#!/usr/bin/env python3
"""Write seed_adapter/ beside this file: SkillOpt's initial OfficeQA skill (../utils/seed_adapter/ADAPTER.md, written
by ../utils/prepare.py) as an `ace` adapter: the skill text first, unchanged, then ACE's generator instructions and
its empty playbook. ACE then starts from exactly the text the GEPA and SkillOpt runs start from, plus an empty
playbook.

    python examples/officeqa/ace/make_seed.py
"""
from pathlib import Path
import shutil

import hermit
from hermit.algorithms.evolvers.ace import playbook as pb
from hermit.spec import check_spec

HERE = Path(__file__).resolve().parent
TEMPLATE = Path(hermit.__file__).resolve().parent / "algorithms" / "templates" / "ace"
skill = (HERE.parent / "utils" / "seed_adapter" / "ADAPTER.md").read_text()
out = HERE / "seed_adapter"
if out.exists():
    shutil.rmtree(out)
shutil.copytree(TEMPLATE, out, ignore=shutil.ignore_patterns("__pycache__"))
(out / "ADAPTER.md").write_text(pb.with_playbook(skill), encoding="utf-8")
result = check_spec(out, TEMPLATE, deploy=True)
if not result.passed:
    raise SystemExit(f"the seed fails the template check: {[i.__dict__ for i in result.issues]}")
text = (out / "ADAPTER.md").read_text()
print(f"wrote {out}: the skill ({len(skill)} chars, unchanged: {text.startswith(skill.rstrip())}) and an empty playbook")
