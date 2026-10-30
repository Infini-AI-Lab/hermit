#!/usr/bin/env python3
"""Write seed_adapter/ beside this file: the reharness template with SkillOpt's initial OfficeQA skill as ADAPTER.md.

The skill is skillopt/envs/officeqa/skills/initial.md of the installed skillopt package, the text ../utils/prepare.py
gives the prompt-only seed the GEPA and SkillOpt runs start from; below it, after a blank line, the template's own
ADAPTER.md (the framing of the retrieved reflections). Everything else is the template's: index.json, the reader,
reharness_core/, an empty bank. Rewrites the directory each time (launch.sh runs it before every command).

    python examples/officeqa/reharness/make_seed.py
"""
from pathlib import Path
import shutil

import hermit
from hermit.spec import check_spec
import skillopt

HERE = Path(__file__).resolve().parent
TEMPLATE = Path(hermit.__file__).resolve().parent / "algorithms" / "templates" / "reharness"
SKILL = Path(skillopt.__file__).resolve().parent / "envs" / "officeqa" / "skills" / "initial.md"
out = HERE / "seed_adapter"
if out.exists():
    shutil.rmtree(out)
shutil.copytree(TEMPLATE, out, ignore=shutil.ignore_patterns("__pycache__"))
skill = SKILL.read_text()
framing = (TEMPLATE / "ADAPTER.md").read_text()
(out / "ADAPTER.md").write_text(skill.rstrip("\n") + "\n\n" + framing, encoding="utf-8")
result = check_spec(out, TEMPLATE)
assert result.passed, [i.__dict__ for i in result.issues]
print(f"wrote {out} ({len(skill)} chars of skill, then the template's {len(framing)} chars of framing)")
