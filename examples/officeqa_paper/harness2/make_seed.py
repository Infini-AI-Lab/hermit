#!/usr/bin/env python3
"""Write seed_adapter/ beside this file: SkillOpt's initial OfficeQA skill (../utils/seed_adapter/ADAPTER.md, written by
../utils/prepare.py) as a `harness2@1.0` adapter, in harness/systemprompt.md. Its rendered ADAPTER.md is the seed
skill itself, so Harness² starts from exactly the text the GEPA and SkillOpt runs start from.

    python examples/officeqa_paper/harness2/make_seed.py
"""
from pathlib import Path
import shutil

from harnessadapter.algorithms.evolvers.harness2.core import surface as S

HERE = Path(__file__).resolve().parent
TEMPLATE = Path(S.__file__).resolve().parents[3] / "templates" / "harness2"
seed = (HERE.parent / "utils" / "seed_adapter" / "ADAPTER.md").read_text()
out = HERE / "seed_adapter"
if out.exists():
    shutil.rmtree(out)
shutil.copytree(TEMPLATE, out, ignore=shutil.ignore_patterns("__pycache__"))
spec = S.load_surface(out)
S.write_tree(spec, {"harness/systemprompt.md": seed}, out, render=True)
rendered = (out / "ADAPTER.md").read_text()
assert rendered.strip() == seed.strip(), "the rendered seed differs from SkillOpt's seed skill"
print(f"wrote {out} ({len(rendered)} chars; identical to the seed skill: {rendered == seed})")
