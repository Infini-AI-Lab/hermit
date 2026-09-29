"""File barrier across the roots of one repeat group (DESIGN §4; engineering review #7).

Each v9 root is its own process (one arm, one repeat). The roots of a repeat group must advance one
block at a time, so every arm's block b runs under the same provider conditions and the same question
set. v8's in-process `threading.Barrier` cannot span processes and had no failure handling.

Layout of the shared directory (`.task-paths.json` barrier.dir, outside every root):

    <dir>/order.json              the group's question order (first root writes it; others must match)
    <dir>/block-00/<root>.done    "ready": the root finished preflight and is about to start block 1
    <dir>/block-NN/<root>.done    the root finished block NN (actors and, for learner arms, learning)
    <dir>/ABORT                   first writer wins; every root stops at its next check

A root starts block b+1 only after `wait(b)` saw every root's block-b file, so for every pair of roots
start(b+1) >= max end(b) (full_audit re-checks it from these files and the roots' state.json). A wait
longer than `timeout_s` writes ABORT and raises; so does any root that stops on an error. A root checks
ABORT between episodes, so the whole group stops within one episode of a failure instead of deadlocking
or burning paid calls. Files are written atomically (temp file + rename in the same directory).

Resume (block level): the operator stops every root, runs `clear_abort` once (ABORT is renamed to
ABORT-<utc>.json, never deleted), then restarts each root with `run.py resume`; each root moves its own
partial block aside and waits on the barrier of the last block it completed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Callable, Mapping, Sequence

ABORT = 'ABORT'


class BarrierAborted(RuntimeError):
    """The group was aborted (ABORT exists or this root timed out waiting)."""


class BarrierTimeout(BarrierAborted):
    """This root waited longer than timeout_s; it wrote ABORT."""


class BarrierStopped(RuntimeError):
    """The local stop event was set while waiting."""


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_atomic(path: Path, value: Mapping) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    os.replace(temporary, path)


def create_exclusive(path: Path, value: Mapping) -> bool:
    """Create `path` with `value` unless it exists (atomic: link of a complete temp file). True if created."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    try:
        os.link(temporary, path)
        return True
    except FileExistsError:
        return False
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None


class FileBarrier:
    def __init__(self, directory, roots: Sequence[str], me: str, *, timeout_s: float, poll_s: float = 5.0,
                 stop: threading.Event | None = None, clock: Callable[[], float] = time.monotonic):
        self.dir = Path(directory)
        self.roots = tuple(roots)
        if me not in self.roots or len(set(self.roots)) != len(self.roots):
            raise ValueError('the barrier roots must be distinct and include this root')
        self.me = me
        self.timeout_s, self.poll_s = float(timeout_s), float(poll_s)
        self.stop = stop
        self.clock = clock
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- files ---------------------------------------------------------------------------------
    def block_dir(self, block: int) -> Path:
        return self.dir / f'block-{int(block):02}'

    def done_path(self, block: int, root: str | None = None) -> Path:
        return self.block_dir(block) / f'{root or self.me}.done'

    def done(self, block: int) -> dict[str, dict]:
        out = {}
        for root in self.roots:
            value = read_json(self.done_path(block, root))
            if value is not None:
                out[root] = value
        return out

    def aborted(self) -> dict | None:
        return read_json(self.dir / ABORT)

    # -- protocol ------------------------------------------------------------------------------
    def check(self) -> None:
        """Raise if the group was aborted or this root was asked to stop."""
        info = self.aborted()
        if info is not None:
            raise BarrierAborted(f'barrier aborted by {info.get("root")}: {info.get("reason")}')
        if self.stop is not None and self.stop.is_set():
            raise BarrierStopped('stop requested')

    def arrive(self, block: int, info: Mapping | None = None) -> dict:
        """Record that this root finished `block` (0 = ready to start block 1). Idempotent: a done file
        that already exists is kept (a resumed root re-arrives at the last block it had completed)."""
        path = self.done_path(block)
        existing = read_json(path)
        if existing is not None:
            return existing
        record = {'root': self.me, 'block': int(block), 'end_utc': utc(), 'end_unix': time.time(),
                  'host': socket.gethostname(), 'pid': os.getpid(), 'info': dict(info or {})}
        write_atomic(path, record)
        return record

    def wait(self, block: int) -> dict[str, dict]:
        """Block until every root arrived at `block`; ABORT (+ raise) after timeout_s."""
        begin = self.clock()
        while True:
            self.check()
            done = self.done(block)
            if len(done) == len(self.roots):
                return done
            waited = self.clock() - begin
            if waited >= self.timeout_s:
                missing = [r for r in self.roots if r not in done]
                reason = f'timeout after {round(waited)} s waiting for block {block:02}: missing {missing}'
                self.abort(reason, block=block)
                raise BarrierTimeout(reason)
            delay = min(self.poll_s, max(0.0, self.timeout_s - waited))
            if self.stop is not None:
                if self.stop.wait(delay):
                    raise BarrierStopped('stop requested')
            else:
                time.sleep(delay)

    def pass_block(self, block: int, info: Mapping | None = None) -> dict[str, dict]:
        self.arrive(block, info)
        return self.wait(block)

    def abort(self, reason: str, *, block: int | None = None) -> dict:
        """Write ABORT unless it exists (the first reason is kept); return the ABORT record."""
        record = {'root': self.me, 'block': block, 'reason': str(reason)[:2000], 'utc': utc(),
                  'unix': time.time(), 'pid': os.getpid()}
        create_exclusive(self.dir / ABORT, record)
        return self.aborted() or record

    def register_order(self, repeat: int, order_sha256: str, count: int) -> dict:
        """Every root of the group must run the same question order (same repeat, same seed)."""
        record = {'repeat': int(repeat), 'order_sha256': order_sha256, 'count': int(count),
                  'first_root': self.me, 'utc': utc()}
        create_exclusive(self.dir / 'order.json', record)
        stored = read_json(self.dir / 'order.json') or {}
        if (stored.get('repeat'), stored.get('order_sha256'), stored.get('count')) != (int(repeat), order_sha256, int(count)):
            raise ValueError(f'question order differs from the repeat group\'s order.json '
                             f'(first written by {stored.get("first_root")})')
        return stored


def clear_abort(directory) -> Path | None:
    """Operator step before a block-level resume: rename ABORT to ABORT-<utc>.json (kept as history)."""
    path = Path(directory) / ABORT
    if not path.exists():
        return None
    target = path.with_name(f'{ABORT}-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")}.json')
    os.replace(path, target)
    return target


def from_spec(spec, *, stop: threading.Event | None = None) -> FileBarrier | None:
    """The barrier of an arms.CampaignSpec (None for a root without a repeat group)."""
    if spec.barrier is None:
        return None
    return FileBarrier(spec.barrier.dir, spec.barrier.roots, spec.root, timeout_s=spec.barrier.timeout_s,
                       poll_s=spec.barrier.poll_s, stop=stop)
