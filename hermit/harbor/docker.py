"""The Docker daemon as the Harbor runner sees it: the data root's free space (the disk gate), a task's prebuilt
image and its removal, a trial's compose project taken down after a kill.

Nothing here touches an image or a container that is not the task's own: every removal names the exact image
reference a task.toml declares, runs under a per-image file lock, and is a quiet no-op when Docker refuses it
because another trial of the task still uses the image. There is no prune of any kind.
"""
from __future__ import annotations

import fcntl
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tomllib

DOCKER = "docker"
GB = 1024 ** 3                                           # GB = 2^30 bytes, on disk
DISK_GATE_POLL_S = 60.0
CLEANUP_MODES = ("images_keep", "rmi_task_image", "rmi_over_gb")


def compose_project(name: str) -> str:
    """Harbor's `_sanitize_docker_compose_project_name` (environments/docker/docker.py): lowercase, a `0` first
    when the name does not start with a letter or digit, every other character outside [a-z0-9_-] a `-`."""
    name = name.lower()
    if not re.match(r"^[a-z0-9]", name):
        name = "0" + name
    return re.sub(r"[^a-z0-9_-]", "-", name)


def compose_down(trial_name: str, *, timeout: float = 180) -> None:
    """Take a trial's compose project down (volumes and orphans included), best effort: Harbor leaves the container
    running when its own client is killed. The project name is the trial's name as Harbor sanitises it."""
    subprocess.run([DOCKER, "compose", "-p", compose_project(f"{trial_name}__env"), "down", "--volumes",
                    "--remove-orphans"], capture_output=True, timeout=timeout, check=False)


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


def wait_for_disk(root: Path, gate_gb: float, wait_s: float, *, poll_s: float = DISK_GATE_POLL_S, log=None) -> bool:
    """True once the data root has `gate_gb` free; False after `wait_s` of waiting. `gate_gb <= 0` never waits."""
    if gate_gb <= 0:
        return True
    deadline = time.time() + wait_s
    waited = False
    while (free := free_gb(root)) < gate_gb:
        if time.time() >= deadline:
            return False
        if not waited:
            (log or (lambda m: print(m, file=sys.stderr)))(f"disk gate: {free:.1f} GB free on {root}, waiting for "
                                                            f"{gate_gb:g} GB")
            waited = True
        time.sleep(poll_s)
    return True


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
