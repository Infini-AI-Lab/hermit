"""The patch `python -m hermit.harbor.dataset prepare` runs on LAB-Bench FigQA's tasks (benchmark.yaml:
dataset.patch), on a download and again on a reuse.

Each task's container runs `environment/workspace/setup.py` when it starts: it shuffles the answer choices, writes
`/app/instruction.md` and `/app/.letter_mapping.json`, and deletes the files that name the right choice. As published,
the mapping it saves also holds the shuffle's seed, and the right choice is the first one before the shuffle: the seed
and the choices give the answer away (replaying the shuffle on the positions finds it). The verifier reads only the
mapping's letters, so the patch drops the seed from what setup.py saves, and nothing else.
"""
from __future__ import annotations

from pathlib import Path

SAVED = '        "mapping": letter_mapping,\n        "seed": seed\n'      # setup.py's mapping_data, as published
DROPPED = '        "mapping": letter_mapping,\n'


def drop_seed(tasks: Path) -> str:
    """Each task's setup.py without the seed in the mapping it saves; what was done. Idempotent: a setup.py already
    patched is left as it is. A setup.py in neither form is not the one this patch knows: refused, with no task
    changed, before any task runs, so that someone reads it first."""
    to_patch, already = [], 0
    for setup in sorted(Path(tasks).glob("*/environment/workspace/setup.py")):
        text = setup.read_text(encoding="utf-8")
        if SAVED in text:
            to_patch.append((setup, text.replace(SAVED, DROPPED)))
        elif DROPPED in text and '"seed": seed' not in text:
            already += 1
        else:
            raise ValueError(f"{setup}: not the setup.py this patch knows (its mapping_data is neither form); read "
                             "it before running the tasks")
    if not to_patch and not already:
        raise ValueError(f"no environment/workspace/setup.py under {tasks}: not LAB-Bench's tasks")
    for setup, text in to_patch:
        setup.write_text(text, encoding="utf-8")
    return f"{len(to_patch)} setup.py patched, {already} already patched"
