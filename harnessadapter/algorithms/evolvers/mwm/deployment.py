"""The venue texts the MWM learner and host need, and what the method itself fixes.

A task describes itself to MWM in `task-info/mwm/` (the Evolver's `deployment` parameter can point elsewhere):

    contract/domain_what.md, env_evidence.md, env_verification.md, env_consumption.md, env_executable.md
                       the contract's venue slots: facts about the environment, information only
    host.json          {"transport_note": ...}: how the host is told it reaches wm
    delegation         managed | native | none: the sub-agent sentence of the contract
    environment/       what the learner's /environment holds (README.md, bin/, docs/)

What is the method's, not the venue's, is fixed here and applies when a venue says nothing: which directories the
learner writes, which mounts it reads, the wm call sentence, the libraries a `wm run` program may import, and the
read paging of the host. The texts are the ones every venue carried before (2026-10-16), so prompts are unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

METHOD_SLOTS = {
    "domain_write_dirs": 'memory/, examples/, consumption/, meta/,\n       tools/, agents/, bin/',
    "domain_read_exception": ', EXCEPT the read-only\n       mounts the harness gives you: /patterns (the pattern\n       library), /skills (your skill library), /evidence (the raw\n       record store, when present) and /environment (what you can\n       reach of the environment — read its README.md first)',
}
HOST_DEFAULTS = {
    "wm_calls_sentence": "Calls are free: none of them touches the environment or counts toward this\nepisode's score; at most 100 calls per episode. wm reaches the world\nmodel only, never the environment.",
    "run_libraries": 'collections, heapq, itertools, functools, json,\n    re, math, random, statistics, numpy.',
    "delegation": "none",
    "read_paging": False,
}
SLOT_NAMES = ("domain_what", "domain_read_exception", "domain_write_dirs", "env_consumption", "env_executable",
              "env_verification", "env_evidence")


def find(task_info: Path | None, explicit: Path | None = None) -> Path | None:
    """The venue texts: `explicit` when given, else task-info/mwm, else the older task-info/deployment."""
    if explicit is not None:
        return explicit
    if task_info is None:
        return None
    for name in ("mwm", "deployment"):
        if (Path(task_info) / name).is_dir():
            return Path(task_info) / name
    return None


def host_settings(root: Path | None) -> dict:
    """The `deployment` resource a host binds: the method's defaults under the venue's host.json."""
    settings = dict(HOST_DEFAULTS)
    if root is not None and (Path(root) / "host.json").is_file():
        try:
            settings.update(json.loads((Path(root) / "host.json").read_text()))
        except ValueError:
            pass
    return settings


def load(root: Path | None) -> dict:
    """{slots, delegation, host} for the contract renderer: the venue's files over the method's defaults."""
    out = {"slots": dict(METHOD_SLOTS), "delegation": "native", "host": host_settings(root)}
    if root is None or not Path(root).is_dir():
        return out
    root = Path(root)
    for name in SLOT_NAMES:
        f = root / "contract" / f"{name}.md"
        if f.is_file():
            out["slots"][name] = f.read_text()
    d = root / "delegation"
    if d.is_file():
        mode = d.read_text().strip()
        if mode in ("managed", "native", "none"):
            out["delegation"] = mode
    return out
