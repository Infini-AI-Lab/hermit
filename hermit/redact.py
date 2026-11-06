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
import tempfile

KEY_NAME = re.compile(r"(_API_KEY|_KEY|_TOKEN|_SECRET)$")
MIN_LENGTH = 16
MAX_BYTES = 16 << 20                       # larger files are scrubbed in chunks (a long session's log)


def key_values() -> dict[str, bytes]:
    """{variable: its value} for the key-like variables of this process."""
    return {name: value.encode() for name, value in os.environ.items()
            if KEY_NAME.search(name) and len(value) >= MIN_LENGTH}


def redact_text(text: str, values: dict[str, bytes] | None = None) -> str:
    for name, value in (key_values() if values is None else values).items():
        text = text.replace(value.decode(), f"<{name}>")
    return text


def redact_value(value, values: dict[str, bytes] | None = None):
    """`value` (a tool's result, a step's report: strings, lists, dicts) with every string in it scrubbed."""
    values = key_values() if values is None else values
    if isinstance(value, str):
        return redact_text(value, values)
    if isinstance(value, os.PathLike):                 # written out with default=str: scrubbed as that text
        text = str(value)
        scrubbed = redact_text(text, values)
        return value if scrubbed == text else scrubbed
    if isinstance(value, dict):
        return {redact_value(k, values): redact_value(v, values) for k, v in value.items()}
    if isinstance(value, (bytes, bytearray)):         # a process's output, written with default=str
        for variable, secret in values.items():
            value = value.replace(secret, f"<{variable}>".encode())
        return value
    if isinstance(value, tuple) and hasattr(value, "_fields"):           # a namedtuple: its fields by position
        return type(value)(*(redact_value(v, values) for v in value))
    if isinstance(value, (list, tuple)):
        items = [redact_value(v, values) for v in value]
        return items if type(value) not in (list, tuple) else type(value)(items)
    if isinstance(value, (set, frozenset)):
        return [redact_value(v, values) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)                                  # anything else is written as its text (default=str)
    scrubbed = redact_text(text, values)
    return value if scrubbed == text else scrubbed


def _holds(path: Path, values: dict[str, bytes]) -> bool:
    """Whether a file too large to hold in memory holds a key value, read chunk by chunk."""
    keep, carry = max(len(v) for v in values.values()) - 1, b""
    with open(path, "rb") as src:
        while chunk := src.read(MAX_BYTES):
            buffer = carry + chunk
            if any(v in buffer for v in values.values()):
                return True
            carry = buffer[-keep:] if keep else b""
    return False


def _redact_large(path: Path, values: dict[str, bytes]) -> bool:
    """Scrub a file too large to hold in memory, chunk by chunk; a key across two chunks is kept back and seen whole."""
    keep = max(len(v) for v in values.values()) - 1
    handle, name = tempfile.mkstemp(prefix=".redact-", dir=path.parent)      # short: any valid name fits beside
    out, changed, carry = Path(name), False, b""
    with open(path, "rb") as src, os.fdopen(handle, "wb") as dst:
        while True:
            chunk = src.read(MAX_BYTES)
            buffer = carry + chunk
            for variable, value in values.items():
                scrubbed = buffer.replace(value, f"<{variable}>".encode())
                changed, buffer = changed or scrubbed != buffer, scrubbed
            if not chunk:
                dst.write(buffer)
                break
            cut = max(len(buffer) - keep, 0)
            dst.write(buffer[:cut])
            carry = buffer[cut:]
    if changed:
        os.chmod(out, stat.S_IMODE(os.lstat(path).st_mode))         # an executable script stays executable
        os.replace(out, path)
    else:
        out.unlink()
    return changed


def redact_file(path: Path, values: dict[str, bytes] | None = None) -> bool:
    """Scrub one regular file (not followed through a link) of the key values, replaced through a file beside it (a
    read-only file too; a directory left read-only is made the owner's to write). Whether it changed."""
    values = key_values() if values is None else values
    path = Path(path)
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not values or not stat.S_ISREG(info.st_mode):
        return False
    parent = path.parent
    mode = stat.S_IMODE(info.st_mode)
    if not os.access(path, os.R_OK):                   # left unreadable: read, its mode kept
        os.chmod(path, mode | stat.S_IRUSR)
    try:
        if info.st_size > MAX_BYTES:
            if not _holds(path, values):               # clean: nothing written (a read-only place too)
                return False
            if not os.access(parent, os.W_OK | os.X_OK):
                os.chmod(parent, stat.S_IMODE(os.lstat(parent).st_mode) | stat.S_IRWXU)
            return _redact_large(path, values)
        data = path.read_bytes()
    finally:
        if os.path.lexists(path) and stat.S_IMODE(os.lstat(path).st_mode) != mode:
            os.chmod(path, mode)
    scrubbed = data
    for variable, value in values.items():
        scrubbed = scrubbed.replace(value, f"<{variable}>".encode())
    if scrubbed == data:
        return False
    if not os.access(parent, os.W_OK | os.X_OK):       # a directory the writer left read-only: made the owner's
        os.chmod(parent, stat.S_IMODE(os.lstat(parent).st_mode) | stat.S_IRWXU)
    handle, name = tempfile.mkstemp(prefix=".redact-", dir=parent)
    with os.fdopen(handle, "wb") as out:
        out.write(scrubbed)
    os.chmod(name, mode)
    os.replace(name, path)
    return True


def redact_path(path: Path) -> list[str]:
    """redact_tree for a directory, redact_file for a file; what changed."""
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        return redact_tree(path)
    return [path.name] if redact_file(path) else []


def redact_tree(root: Path) -> list[str]:
    """Scrub every regular file under `root` (links not followed) of the key values; returns what changed, relative."""
    values, changed = key_values(), []
    if not values or not Path(root).is_dir() or Path(root).is_symlink():
        return changed
    if not os.access(root, os.R_OK | os.X_OK):         # the root itself left unreadable
        os.chmod(root, stat.S_IMODE(os.lstat(root).st_mode) | stat.S_IRWXU)
    for directory, dirs, files in os.walk(root):
        for name in dirs:                               # a directory left unreadable is walked too
            sub = Path(directory) / name
            if not os.path.islink(sub) and not os.access(sub, os.R_OK | os.X_OK):
                os.chmod(sub, stat.S_IMODE(os.lstat(sub).st_mode) | stat.S_IRWXU)
        for name in files:
            if redact_file(Path(directory) / name, values):
                changed.append(os.path.relpath(Path(directory) / name, root))
    # A name that holds a key value (a learner's skills/<value>/), files and directories alike, deepest first.
    for directory, dirs, files in os.walk(root, topdown=False):
        for name in [*files, *dirs]:
            new = name
            for variable, value in values.items():
                new = new.replace(value.decode(errors="replace"), f"<{variable}>")
            if new != name:
                target = Path(directory) / new
                while os.path.lexists(target):
                    target = target.with_name(target.name + "_")
                os.rename(Path(directory) / name, target)
                changed.append(os.path.relpath(target, root))
    return changed
