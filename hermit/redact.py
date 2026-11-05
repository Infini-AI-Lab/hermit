"""Model keys out of what a run keeps and shares. Sandboxes get their models' keys, as Harbor gives its agents theirs
(docs/developer/sandbox-design.md, "Models and keys"), so an agent or a learner could write one into its output. The
learning material, the step records the learner wrote and the adapter are scrubbed of every key value before they
are kept: each value of a key-like variable of this process (…_API_KEY, …_KEY, …_TOKEN, …_SECRET, at least 16
characters) becomes `<VARIABLE>`."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat

KEY_NAME = re.compile(r"(_API_KEY|_KEY|_TOKEN|_SECRET)$")
MIN_LENGTH = 16
MAX_BYTES = 16 << 20                       # larger files are left as they are (no trajectory or adapter file is)


def key_values() -> dict[str, bytes]:
    """{variable: its value} for the key-like variables of this process."""
    return {name: value.encode() for name, value in os.environ.items()
            if KEY_NAME.search(name) and len(value) >= MIN_LENGTH}


def redact_text(text: str, values: dict[str, bytes] | None = None) -> str:
    for name, value in (key_values() if values is None else values).items():
        text = text.replace(value.decode(), f"<{name}>")
    return text


def redact_tree(root: Path) -> list[str]:
    """Scrub every regular file under `root` (links not followed) of the key values; returns what changed, relative."""
    values, changed = key_values(), []
    if not values or not Path(root).is_dir():
        return changed
    for directory, _, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                continue
            data = path.read_bytes()
            scrubbed = data
            for variable, value in values.items():
                scrubbed = scrubbed.replace(value, f"<{variable}>".encode())
            if scrubbed != data:
                path.write_bytes(scrubbed)
                changed.append(os.path.relpath(path, root))
    return changed
