#!/usr/bin/env python3
"""Write seed_adapter_codex/ beside this file: SkillOpt's initial OfficeQA skill (../utils/seed_adapter/ADAPTER.md,
written by ../utils/prepare.py) as a `harness2-codex` adapter: the skill text is AGENTS.md (and ADAPTER.md, its
copy), and the on-demand directories hold only the template's placeholders. Harness² under Codex then starts from
exactly the text the GEPA and SkillOpt runs start from, as Codex reads it.

    python examples/officeqa/harness2_stream/make_seed_codex.py
"""
from pathlib import Path
import shutil

import hermit
from hermit.spec import check_spec

HERE = Path(__file__).resolve().parent
TEMPLATE = Path(hermit.__file__).resolve().parent / "algorithms" / "templates" / "harness2_codex"
seed = (HERE.parent / "utils" / "seed_adapter" / "ADAPTER.md").read_text()
out = HERE / "seed_adapter_codex"
if out.exists():
    shutil.rmtree(out)
shutil.copytree(TEMPLATE, out, ignore=shutil.ignore_patterns("__pycache__"))
for name in ("AGENTS.md", "ADAPTER.md"):
    (out / name).write_text(seed, encoding="utf-8")
result = check_spec(out, TEMPLATE, deploy=True)
assert result.passed, [i.__dict__ for i in result.issues]
print(f"wrote {out} ({len(seed)} chars of AGENTS.md; identical to the seed skill: {(out / 'AGENTS.md').read_text() == seed})")
