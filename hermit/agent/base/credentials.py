"""A credential's lifecycle, one implementation for every place a sign-in is handed to a session: provisioned, used
by the session (the block), taken back (a refresh the session made becomes the shared copy) and removed, the last two
however the block ends. A take-back that fails never keeps the copies (the removal runs regardless); a removal that
fails is raised, unless the block itself failed, whose error then stands (the removal's is printed)."""
from __future__ import annotations

import contextlib
import sys


@contextlib.contextmanager
def held(provision, take_back=None, remove=None):
    """`provision()` -> the copies, handed to the block; then `take_back(copies)` and `remove(copies)`."""
    copies = provision()
    failed = False
    try:
        yield copies
    except BaseException:
        failed = True
        raise
    finally:
        try:
            if take_back is not None:
                take_back(copies)
        finally:
            if remove is not None:
                try:
                    remove(copies)
                except Exception as exc:                 # noqa: BLE001 - the block's own error stands
                    if not failed:
                        raise
                    print(f"WARNING: a credential copy could not be removed: {exc}", file=sys.stderr, flush=True)
