"""The Docker daemon as the Harbor runner sees it: the data root's free space (the disk gate), a task's prebuilt
image and its removal, a trial's compose projects (its environment's, its separate verifiers') taken down after a
kill.

Nothing here touches an image or a container that is not the task's own: every removal names the exact image
reference a task.toml declares, runs under a per-image file lock, and is a quiet no-op when Docker refuses it
because another trial of the task still uses the image. There is no prune of any kind.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib

DOCKER = "docker"
GB = 1024 ** 3                                           # GB = 2^30 bytes, on disk
DISK_GATE_POLL_S = 60.0
CLEANUP_MODES = ("images_keep", "rmi_task_image", "rmi_over_gb")
ON_DISK = 2.5                                            # an image's size on disk over its compressed (pulled) size
_NO_LOCK = contextlib.nullcontext()


def compose_project(name: str) -> str:
    """Harbor's `_sanitize_docker_compose_project_name` (environments/docker/docker.py): lowercase, a `0` first
    when the name does not start with a letter or digit, every other character outside [a-z0-9_-] a `-`."""
    name = name.lower()
    if not re.match(r"^[a-z0-9]", name):
        name = "0" + name
    return re.sub(r"[^a-z0-9_-]", "-", name)


def verifier_prefix(trial_name: str) -> str:
    """The start of the compose project of each separate verifier environment of a trial: Harbor names its session
    `<trial>__verifier__<key>` (harbor/trial/trial.py `_separate_verifier_session_id`: characters outside [A-Za-z0-9-._]
    made `_`, at most 63 with a digest past that, which keeps the `<trial>__verifier` a trial name of at most 41
    characters gives), then sanitises it as every project name."""
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in f"{trial_name}__verifier")
    return compose_project(safe)


def compose_projects(prefix: str, *, timeout: float = 60) -> list[str]:
    """The compose projects on this machine, running or stopped, whose name starts with `prefix`."""
    try:
        out = subprocess.run([DOCKER, "compose", "ls", "-a", "-q"], capture_output=True, text=True, timeout=timeout,
                             check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [name for name in out.split() if name.startswith(prefix)]


def compose_down(trial_name: str, *, timeout: float = 180) -> None:
    """Take a trial's compose projects down (volumes and orphans included), best effort: Harbor leaves the containers
    running when its own client is killed. The projects are the trial's environment (`<trial>__env`, as Harbor
    sanitises the name) and each separate verifier environment it started (verifier_prefix), named alone (`-p`) and
    taken down from an empty directory of their own (`/` when none can be made: a full /tmp) with none of this
    process's COMPOSE_* variables (COMPOSE_FILE, COMPOSE_ENV_FILES, COMPOSE_PROFILES, ...): no compose file or .env
    that this process's working directory holds or its environment names is read, whose volumes `down --volumes`
    would remove too."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
    try:
        empty = tempfile.TemporaryDirectory(prefix="hermit-compose-down-", ignore_cleanup_errors=True)
    except OSError:
        empty = contextlib.nullcontext("/")
    with empty as where:
        for project in [compose_project(f"{trial_name}__env"), *compose_projects(verifier_prefix(trial_name))]:
            try:
                subprocess.run([DOCKER, "compose", "-p", project, "down", "--volumes", "--remove-orphans"],
                               capture_output=True, timeout=timeout, check=False, cwd=where, env=env)
            except (OSError, subprocess.SubprocessError):
                continue


def task_image(task_dir: Path) -> str | None:
    """`environment.docker_image` of the task's task.toml: a prebuilt image Harbor pulls; None for a Dockerfile."""
    try:
        config = tomllib.loads((Path(task_dir) / "task.toml").read_text())
    except (OSError, ValueError):
        return None
    return (config.get("environment") or {}).get("docker_image")


def data_root() -> Path:
    """Docker's data root (`docker info`), /var/lib/docker when the daemon does not answer."""
    try:
        out = subprocess.run([DOCKER, "info", "--format", "{{.DockerRootDir}}"], capture_output=True, text=True,
                             timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    return Path(out or "/var/lib/docker")


def free_gb(root: Path) -> float:
    try:
        return shutil.disk_usage(root).free / GB
    except OSError:
        return float("inf")                              # no data root to measure: the gate does not hold


class Pulls:
    """The images trials of this process were let in to pull and have not pulled yet, with the room each will take:
    the disk gate counts that room as taken until the image is there (then the data root's free space shows it)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending: dict[object, tuple[str, float]] = {}

    def taken_gb(self) -> float:
        out, present = 0.0, {}
        for image, gb in list(self.pending.values()):
            if image not in present:
                present[image] = image_size(image) is not None
            if not present[image]:                     # not pulled yet: its room is still to come
                out += gb
        return out

    def add(self, image: str, gb: float) -> object:
        key = object()
        self.pending[key] = (image, gb)
        return key

    def done(self, key: object) -> None:
        with self.lock:
            self.pending.pop(key, None)


def wait_for_disk(root: Path, gate_gb: float, wait_s: float, *, poll_s: float = DISK_GATE_POLL_S, log=None,
                  image: str | None = None, image_gb: float = 0.0, pulls: Pulls | None = None,
                  admitted: list | None = None) -> bool:
    """True once the data root has `gate_gb` free, and room for `image` (`image_gb` on disk) when it is not there yet,
    beside the room `pulls` holds for images other trials are still to pull (the image's room is then held there too:
    its key goes into `admitted`, for `pulls.done` after the trial); False after `wait_s` of waiting. `gate_gb <= 0`
    never waits."""
    if gate_gb <= 0:
        return True
    deadline = time.time() + wait_s
    waited = False
    while True:
        with pulls.lock if pulls is not None else _NO_LOCK:
            need = gate_gb + (image_gb if image and image_gb and image_size(image) is None else 0.0)
            free = free_gb(root) - (pulls.taken_gb() if pulls is not None else 0.0)
            if free >= need:
                if pulls is not None and image and need > gate_gb:
                    key = pulls.add(image, image_gb)
                    if admitted is not None:
                        admitted.append(key)
                return True
        if time.time() >= deadline:
            return False
        if not waited:
            (log or (lambda m: print(m, file=sys.stderr)))(f"disk gate: {free:.1f} GB free on {root} (less what other "
                                                            f"trials are still to pull), waiting for {need:g} GB")
            waited = True
        time.sleep(poll_s)


def image_size(image: str) -> int | None:
    """The on-disk size `docker image inspect` reports for the exact reference; None when it is not present (or
    the daemon does not answer), which `rmi_over_gb` treats as nothing to remove."""
    try:
        proc = subprocess.run([DOCKER, "image", "inspect", "-f", "{{.Size}}", image], capture_output=True, text=True,
                              timeout=120, check=False)
        return int(proc.stdout.strip()) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def remove_image(image: str, lock_dir: Path, *, over_bytes: int | None = None) -> bool:
    """`docker image rm` of the exact reference, under a file lock per image; with `over_bytes`, only when the
    image's on-disk size exceeds it. Returns whether a removal was attempted."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / (re.sub(r"[^A-Za-z0-9_.-]", "_", image) + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if over_bytes is not None:
                size = image_size(image)
                if size is None or size <= over_bytes:
                    return False
            subprocess.run([DOCKER, "image", "rm", image], capture_output=True, timeout=300, check=False)
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def cleanup_images(mode: str, task_dir: Path, lock_dir: Path, *, threshold_gb: float = 1.0) -> None:
    """After a trial, what `docker_cleanup` asks for: nothing (`images_keep`), the task's prebuilt image
    (`rmi_task_image`), or that image when it is larger than `threshold_gb` on disk (`rmi_over_gb`)."""
    if mode not in CLEANUP_MODES:
        raise ValueError(f"docker_cleanup must be one of {', '.join(CLEANUP_MODES)}, not {mode!r}")
    if mode == "images_keep":
        return
    image = task_image(task_dir)
    if not image:
        return
    remove_image(image, lock_dir, over_bytes=None if mode == "rmi_task_image" else int(threshold_gb * GB))
