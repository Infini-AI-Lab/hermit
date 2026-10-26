#!/usr/bin/env python3
"""Write seed_adapter/ beside this file: SkillOpt's initial OfficeQA skill (../utils/seed_adapter/ADAPTER.md, written
by ../utils/prepare.py) as a `continual-harness` adapter: the skill text is AGENTS.md (the base policy p; ADAPTER.md
is its copy), and the sub-agents, skills and memory start empty, as the template leaves them. Continual Harness then
starts from exactly the text the GEPA, SkillOpt and Harness² runs start from.

    python examples/officeqa/continual_harness/make_seed.py
"""
from pathlib import Path
import shutil

import hermit
from hermit.spec import check_spec

HERE = Path(__file__).resolve().parent
TEMPLATE = Path(hermit.__file__).resolve().parent / "algorithms" / "templates" / "continual_harness"
seed = (HERE.parent / "utils" / "seed_adapter" / "ADAPTER.md").read_text()
out = HERE / "seed_adapter"
if out.exists():
    shutil.rmtree(out)
shutil.copytree(TEMPLATE, out, ignore=shutil.ignore_patterns("__pycache__"))
for name in ("AGENTS.md", "ADAPTER.md"):
    (out / name).write_text(seed, encoding="utf-8")
result = check_spec(out, TEMPLATE, deploy=True)
assert result.passed, [i.__dict__ for i in result.issues]
print(f"wrote {out} ({len(seed)} chars of AGENTS.md; identical to the seed skill: {(out / 'AGENTS.md').read_text() == seed})")
