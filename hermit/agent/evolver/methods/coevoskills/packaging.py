"""Project upstream Skill paths into the unchanged OfficeQA actor's workspace.

Only deployment paths change. The original exported packages remain archived;
no solver logic, examples, answers, or upstream evolution decisions are edited.
The same projection is used by training oracles and frozen final deployment.
"""
from __future__ import annotations

import hashlib
import os
from hermit.hosts.inputs import adapter_folder
from pathlib import Path
import re
import shutil

from hermit.agent.evolver.methods.coevoskills.upstream import real_tree, safe_name

TEXT = {'.md', '.py', '.sh', '.txt', '.json', '.yaml', '.yml', '.toml'}


def project(source: Path, destination: Path, task_id: str) -> dict:
    real_tree(source)
    tid = safe_name(task_id)
    packages = sorted(p.parent for p in source.glob('evo-*/SKILL.md'))
    if not packages:
        raise ValueError('upstream exported no Skill to deploy')
    mapping = {
        '/app/environment/skills/' + p.name: f'skills/{tid}/{p.name}'
        for p in packages
    }
    workspace = Path(os.path.relpath('.', adapter_folder(Path('.'), {'id': 'coevoskills'})))
    mapping.update({'/root/docs': (workspace / 'docs').as_posix(),
                    '/root/answer.txt': (workspace / 'skill-answer.txt').as_posix()})
    # The boundary prevents changing similarly named paths or unrelated literals.
    rules = [(re.compile(re.escape(a) + r'''(?=$|[/\s'"`),;\]])'''), b)
             for a, b in sorted(mapping.items(), key=lambda x: -len(x[0]))]
    shutil.copytree(source, destination)
    files = []
    for path in sorted(destination.rglob('*')):
        if not path.is_file():
            continue
        path.chmod(path.stat().st_mode | 0o600)
        original = path.read_bytes()
        output = original
        replacements = 0
        if path.suffix.lower() in TEXT:
            text = original.decode('utf-8')
            for pattern, replacement in rules:
                text, n = pattern.subn(replacement, text)
                replacements += n
            output = text.encode('utf-8')
            if output != original:
                path.write_bytes(output)
        files.append({'path': str(path.relative_to(destination)),
                      'source_sha256': hashlib.sha256(original).hexdigest(),
                      'deployed_sha256': hashlib.sha256(output).hexdigest(),
                      'path_replacements': replacements})
    for path in [destination, *destination.rglob('*')]:
        if path.is_dir():
            path.chmod(path.stat().st_mode | 0o700)
    return {'version': 1, 'task_id': tid, 'mapping': mapping, 'files': files,
            'scope': 'deterministic harness deployment path projection only'}
