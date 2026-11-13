"""The patch `python -m hermit.harbor.dataset prepare` runs on LAB-Bench FigQA's tasks (benchmark.yaml:
dataset.patch), on a download and again on a reuse. It closes two ways the published tasks let an agent score
without reading the figure.

The seed. Each task's container runs `environment/workspace/setup.py` when it starts: it shuffles the answer choices,
writes `/app/instruction.md` and `/app/.letter_mapping.json`, and deletes the files that name the right choice. As
published, the mapping it saves also holds the shuffle's seed, and the right choice is the first one before the
shuffle: the seed and the choices give the answer away (replaying the shuffle on the positions finds it). The verifier
reads only the mapping's letters, so the patch drops the seed from what setup.py saves.

The fallback. The verifier (`tests/test.sh`, uploaded with the right choice in `/tests/choices.json` once the agent
has stopped) runs `/app/setup.py` again when the mapping is missing. The mapping is written before the agent starts,
so a missing one is the agent's doing, and `/app/setup.py` is the agent's to rewrite: a script left there would run
beside the right choice and could write a mapping that matches any letter. The patch removes the fallback; a missing
mapping then scores 0 at the verifier's next check, which the published script already has.
"""
from __future__ import annotations

from pathlib import Path

# (a task's file, its text as published, the text the patch puts in its place, what a patched file no longer holds)
SEED = ("environment/workspace/setup.py",
        '        "mapping": letter_mapping,\n        "seed": seed\n',
        '        "mapping": letter_mapping,\n',
        '"seed": seed')
FALLBACK = ("tests/test.sh",
            '# Ensure setup.py has run (generates /app/.letter_mapping.json)\n'
            'if [[ ! -f "$mapping_file" ]]; then\n'
            '    echo "Running setup.py to generate letter mapping..."\n'
            '    if [[ -f /app/setup.py ]]; then\n'
            '        python3 /app/setup.py || {\n'
            '            echo "setup.py failed"\n'
            '            echo 0 > "${LOG_DIR}/reward.txt"\n'
            '            exit 1\n'
            '        }\n'
            '    else\n'
            '        echo "setup.py not found and letter_mapping.json missing"\n'
            '        echo 0 > "${LOG_DIR}/reward.txt"\n'
            '        exit 1\n'
            '    fi\n'
            'fi\n',
            "# Hermit's patch (utils/patches.py): the mapping is written before the agent starts, so a missing one\n"
            "# is the agent's doing, and /app/setup.py the agent's to rewrite: it is not run again here.\n",
            "python3 /app/setup.py")
PATCHES = (SEED, FALLBACK)


def patch_tasks(tasks: Path) -> str:
    """Each task's setup.py without the seed in the mapping it saves, and its verifier without the fallback; what was
    done. Idempotent: a file already patched is left as it is. A file in neither form, or a task without both files,
    is not what this patch knows: refused, with no task changed, before any task runs, so that someone reads it
    first."""
    tasks = Path(tasks)
    edits, said, names = [], [], None
    for path, published, patched, gone in PATCHES:
        files = sorted(tasks.glob(f"*/{path}"))
        if not files:
            raise ValueError(f"no */{path} under {tasks}: not LAB-Bench's tasks")
        found = {file.relative_to(tasks).parts[0] for file in files}
        if names is not None and found != names:
            raise ValueError(f"{tasks}: the tasks with a {path} are not those with a {PATCHES[0][0]}; read them before "
                             "running the tasks")
        names, done, already = found, 0, 0
        for file in files:
            text = file.read_text(encoding="utf-8")
            if published in text:
                edits.append((file, text.replace(published, patched)))
                done += 1
            elif patched in text and gone not in text:
                already += 1
            else:
                raise ValueError(f"{file}: not the {Path(path).name} this patch knows (neither form); read it before "
                                 "running the tasks")
        said.append(f"{Path(path).name}: {done} patched, {already} already patched")
    for file, text in edits:
        file.write_text(text, encoding="utf-8")
    return "; ".join(said)
