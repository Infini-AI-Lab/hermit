"""Check a completed acting session against its frozen adapter deployment."""
from __future__ import annotations

import json
from pathlib import Path

from hermit.adapter.loader import load_manifest
from hermit.hosts.inputs import adapter_files


def verify_deployment(adapter: Path, log: Path) -> None:
    adapter, log = Path(adapter), Path(log)
    work = log.parent / 'work'
    previous = json.loads((log / 'deployment.json').read_text())
    # The shared check rejects changed contents, links and replaced directories.
    files = adapter_files(adapter, work, load_manifest(adapter), previous)
    if previous.get('files') != files:
        raise ValueError('deployment record differs from the frozen adapter')
    # Redeployment permits missing files; an integrity audit must reject them.
    for relative in files:
        if not (work / relative).is_file():
            raise ValueError(f'{relative} is missing from the frozen deployment')
