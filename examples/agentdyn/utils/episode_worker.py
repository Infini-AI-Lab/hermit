"""Trusted episode subprocess; learned Python executes only in its hook container."""
from dataclasses import asdict
import contextlib
import json
from pathlib import Path
import sys
from bridge import _run_episode

if __name__ == '__main__':
    job = json.loads(sys.stdin.read())
    with contextlib.redirect_stdout(sys.stderr):
        result = _run_episode(job['task'], Path(job['adapter']) if job['adapter'] else None,
                              params=job['params'], base=Path(job['base']))
    Path(job['result']).write_text(json.dumps(asdict(result), default=str))
