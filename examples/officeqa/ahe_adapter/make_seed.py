#!/usr/bin/env python3
"""Write seed_adapter/ beside this file: the `ahe` seed harness (the paper's seed system prompt, an empty
long-term memory, the seed middleware) with SkillOpt's initial OfficeQA skill (../utils/seed_adapter/ADAPTER.md,
written by ../utils/prepare.py) appended to its system prompt, so H_0 holds the same text the GEPA, SkillOpt,
Harness², Continual Harness and Meta-Harness Adapter runs start from. AGENTS.md and its copy ADAPTER.md carry it.

    python examples/officeqa/ahe_adapter/make_seed.py
"""
from pathlib import Path
import shutil

import hermit
from hermit.spec import check_spec

HERE = Path(__file__).resolve().parent
TEMPLATE = Path(hermit.__file__).resolve().parent / "adapter" / "templates" / "ahe"
skill = (HERE.parent / "utils" / "seed_adapter" / "ADAPTER.md").read_text(encoding="utf-8").strip()
out = HERE / "seed_adapter"
if out.exists():
    shutil.rmtree(out)
shutil.copytree(TEMPLATE, out, ignore=shutil.ignore_patterns("__pycache__"))
prompt = (TEMPLATE / "AGENTS.md").read_text(encoding="utf-8").rstrip() + "\n\n" + skill + "\n"
for name in ("AGENTS.md", "ADAPTER.md"):
    (out / name).write_text(prompt, encoding="utf-8")
result = check_spec(out, TEMPLATE, deploy=True)
assert result.passed, [i.__dict__ for i in result.issues]
print(f"wrote {out} ({len(prompt)} chars of AGENTS.md)")
