"""A benchmark's task set (Harbor's format) as Hermit data: the rows of `tasks.jsonl`, a lock of the tasks' contents, splits, a download.

A Harbor task is a directory: `task.toml`, `instruction.md`, `environment/` (a Dockerfile or a prebuilt
`docker_image`), `tests/test.sh` writing `/logs/verifier/reward.txt`. A multi-step task adds `[[steps]]` to task.toml
and `steps/<name>/{instruction.md, tests/}`: one environment that lives through every step (a stream).

    python -m hermit.harbor.dataset prepare utils/benchmark.yaml [--force]   # its `dataset:` block, below
    python -m hermit.harbor.dataset download terminal-bench@2.0 --output ~/.cache/hermit/harbor/datasets
    python -m hermit.harbor.dataset index <tasks dir> --out utils/tasks.jsonl [--rollouts 2]
    python -m hermit.harbor.dataset lock <tasks dir> --out utils/dataset.lock [--source terminal-bench@2.0]
    python -m hermit.harbor.dataset check <tasks dir> --lock utils/dataset.lock
    python -m hermit.harbor.dataset splits <tasks dir> --out-dir utils/splits --train 50 --test 39 --seed 0

The rows: one per task, or one per (task, rollout) when `--rollouts k` asks for k independent attempts
(`<task>#<k>`), or one per (task, step) for a multi-step task (`<task>:<step>`); every row carries the task's
directory name as `harbor_task`, its instruction, its timeouts and metadata, and a step row its `step`,
`step_index` (from 1) and `steps` (how many), and the task's resources (`cpus`, `memory_mb`, `storage_mb`, `gpus`)
and network modes (`network`: the agent phase's and the verifier's, as Harbor resolves them: `network_modes`). The
lock records the sha256 of every task directory's contents, so a run can tell that the tasks it scores are the ones
it was prepared with.

`prepare` makes an example's data from the `dataset:` block of its benchmark file (the runner's shared params ignore
the block), everything written beside that file:

    dataset:
      ref: labbench@1.0            # required, name@version: the Harbor registry's (a bare name would be @head)
      data: null                   # where the tasks go (relative to this file; not `tasks`, the link below, nor in
                                   # it); default <root>/<name>@<version>, the root $HERMIT_HARBOR_TASKS (a relative
                                   # one under the working directory) or ~/.cache/hermit/harbor-tasks (outside the
                                   # checkout)
      repo: null                   # a git registry's dataset (registry_url, registry_path: Harbor's options too)
      rollouts: 1                  # k > 1: rows <task>#1..k, a task's rows together
      split: {train: 50, val: 31, test: rest, seed: 0}   # task counts (test: rest = what train and val leave), or
                                   # in_sample: train and test are every row
      smoke: [figqa-0001]          # rows <task>#0 (splits/smoke.txt, in tasks.jsonl too): a smoke trial is never an
                                   # eval trial's row, so the trial cache never mixes them
      patch: patches.py:drop_seed  # fn(tasks_dir) run on the download (and on a reuse: it must be idempotent), its
                                   # file's digest and what it returns recorded in the lock
      image_sizes: false           # true: image_sizes.json, the registry's compressed size of each task's prebuilt
                                   # image (`docker manifest inspect`), for the runner's disk gate

It writes `tasks` (a link to the task directories), `tasks.jsonl`, `dataset.lock`, `splits/{all,train,val,test,
smoke}.txt` and, with `image_sizes: true`, `image_sizes.json`. Tasks already in the data directory are reused; a
download is made in a folder of its own, `.<name>.partial` in the data directory when that is there (else
`<data>.partial` beside it), and moved into place once it is whole, so one that stopped part of the way is never taken
for the set (the next prepare removes it), and a second prepare downloading there at the same time is refused (on a
filesystem that keeps file locks). The tasks, reused or downloaded, must match the lock beside the benchmark file when
there is one (`check_lock`, and the lock's ref and patch): a mismatch is an error unless `--force`. The lock names no
path of this machine, and a prepare that finds the tasks and their source as locked leaves it as it is.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import shutil
import stat
import subprocess
import sys
import tomllib

TASK_FILE = "task.toml"
SKIP_DIRS = {".git", "__pycache__"}
LOCK_SCHEMA = 1
TASKS_ROOT_ENV = "HERMIT_HARBOR_TASKS"                  # the root of every prepared task set (prepare's data default)
TASKS_ROOT = "~/.cache/hermit/harbor-tasks"
HARBOR = "~/.cache/hermit/harbor-venv/bin/harbor"
SPLITS = ("all", "train", "val", "test", "smoke")
DATASET_KEYS = {"ref", "data", "repo", "registry_url", "registry_path", "rollouts", "split", "smoke", "patch",
                "image_sizes"}


def is_task(path: Path) -> bool:
    return path.is_dir() and (path / TASK_FILE).is_file()


def task_dirs(tasks_dir: Path) -> list[Path]:
    """The task directories under `tasks_dir`, by name."""
    tasks_dir = Path(tasks_dir)
    if is_task(tasks_dir):
        return [tasks_dir]
    return sorted(p for p in tasks_dir.iterdir() if is_task(p)) if tasks_dir.is_dir() else []


def task_config(task_dir: Path) -> dict:
    return tomllib.loads((Path(task_dir) / TASK_FILE).read_text())


def step_names(config: dict) -> list[str]:
    return [str(s["name"]) for s in (config.get("steps") or [])]


def _baseline(env) -> str:
    """An environment's network mode (Harbor's baseline): its network_mode, else the deprecated allow_internet (true:
    public, false: no-network), else public."""
    env = env if isinstance(env, dict) else {}
    if env.get("network_mode"):
        return str(env["network_mode"])
    if env.get("allow_internet") is not None and env.get("allowed_hosts") is None:
        return "public" if env["allow_internet"] else "no-network"
    return "public"


def network_modes(config: dict, step: str | None = None) -> dict[str, str]:
    """{"agent": mode, "verifier": mode} of a task (or of its step `step`), as Harbor resolves them
    (harbor/trial/network_policy.py): a phase's own `network_mode` ([agent], [verifier], a step's first), else the
    environment's baseline (the verifier's own environment's when it has one); modes public, no-network, allowlist."""
    step_cfg = next((s for s in config.get("steps") or [] if str(s.get("name")) == str(step)), None) if step else None
    step_cfg = step_cfg or {}

    def explicit(role: str):
        return ((step_cfg.get(role) or {}).get("network_mode")) or ((config.get(role) or {}).get("network_mode"))
    base = _baseline(config.get("environment"))
    verifier_env = ((step_cfg.get("verifier") or {}).get("environment")
                    or (config.get("verifier") or {}).get("environment"))
    return {"agent": str(explicit("agent") or base),
            "verifier": str(explicit("verifier") or (_baseline(verifier_env) if verifier_env else base))}


def _size_mb(value) -> int | None:
    """A size as Harbor reads the deprecated `memory`/`storage` fields ("2G", "512M"), in MB; None when unreadable."""
    text = str(value).strip().upper()
    try:
        if text.endswith("G"):
            return int(float(text[:-1]) * 1024)
        if text.endswith("M"):
            return int(float(text[:-1]))
        if text.endswith("K"):
            return int(float(text[:-1]) / 1024)
    except ValueError:
        return None
    return None


def resources(config: dict) -> dict:
    """The task's resources ([environment]): cpus, memory_mb, storage_mb, gpus (None where it sets none)."""
    env = config.get("environment") or {}
    memory = env.get("memory_mb") if env.get("memory_mb") is not None else \
        (_size_mb(env["memory"]) if env.get("memory") is not None else None)
    storage = env.get("storage_mb") if env.get("storage_mb") is not None else \
        (_size_mb(env["storage"]) if env.get("storage") is not None else None)
    return {"cpus": env.get("cpus"), "memory_mb": memory, "storage_mb": storage, "gpus": env.get("gpus")}


def _instruction(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def rows_of(task_dir: Path, *, rollouts: int = 1) -> list[dict]:
    """The Hermit rows of one task: by rollout, and by step for a multi-step task."""
    task_dir = Path(task_dir)
    config = task_config(task_dir)
    env, agent, verifier = (config.get("environment") or {}), (config.get("agent") or {}), (config.get("verifier") or {})
    base = {"harbor_task": task_dir.name, "agent_timeout_sec": agent.get("timeout_sec"),
            "verifier_timeout_sec": verifier.get("timeout_sec"), "build_timeout_sec": env.get("build_timeout_sec"),
            "docker_image": env.get("docker_image"), "metadata": config.get("metadata") or {}, **resources(config),
            "network": network_modes(config)}
    steps = step_names(config)
    out = []
    for k in range(1, rollouts + 1):
        suffix = f"#{k}" if rollouts > 1 else ""
        if not steps:
            out.append({"id": f"{task_dir.name}{suffix}", **base, "rollout": k,
                        "instruction": _instruction(task_dir / "instruction.md")})
            continue
        for i, step in enumerate(steps, start=1):
            step_cfg = next(s for s in config["steps"] if str(s["name"]) == step)
            out.append({"id": f"{task_dir.name}:{step}{suffix}", **base, "rollout": k, "step": step, "step_index": i,
                        "steps": len(steps), "instruction": _instruction(task_dir / "steps" / step / "instruction.md"),
                        "network": network_modes(config, step),
                        "agent_timeout_sec": (step_cfg.get("agent") or {}).get("timeout_sec") or base["agent_timeout_sec"],
                        "verifier_timeout_sec": ((step_cfg.get("verifier") or {}).get("timeout_sec")
                                                 or base["verifier_timeout_sec"])})
    return out


def index(tasks_dir: Path, *, rollouts: int = 1, include: list[str] | None = None) -> list[dict]:
    """The rows of every task under `tasks_dir` (or of those named in `include`)."""
    wanted = set(include) if include else None
    out = []
    for task_dir in task_dirs(tasks_dir):
        if wanted is not None and task_dir.name not in wanted:
            continue
        out.extend(rows_of(task_dir, rollouts=rollouts))
    if wanted is not None:
        missing = wanted - {r["harbor_task"] for r in out}
        if missing:
            raise ValueError(f"not under {tasks_dir}: {sorted(missing)[:5]}")
    return out


def write_jsonl(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


# ---- the lock ----------------------------------------------------------------------------------------------------

def digest(task_dir: Path) -> str:
    """sha256 of the task directory's contents: every file's relative path, mode and bytes, in path order (what
    RSIHub's dataset manifests hash too); of the mode only whether the file is executable, as git keeps it (755 or
    644): the group's and others' bits are the umask's at download, not the task's."""
    task_dir = Path(task_dir)
    h = hashlib.sha256()
    for top, dirs, names in os.walk(task_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            path = Path(top) / name
            if name.endswith(".pyc") or path.is_symlink() and not path.exists():
                continue
            rel = path.relative_to(task_dir).as_posix()
            mode = 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644
            h.update(f"{rel}\0{mode:o}\0".encode())
            h.update(path.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def lock_of(tasks_dir: Path, *, source: dict | None = None) -> dict:
    """The lock of the tasks under `tasks_dir`: each one's digest, their source and when they were locked; no path of
    this machine (a lock is committed beside its benchmark file)."""
    tasks = {t.name: digest(t) for t in task_dirs(tasks_dir)}
    return {"schema": LOCK_SCHEMA, "source": source or {},
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tasks": tasks}


def _locked_as(lock) -> dict | None:
    """What a lock says of its tasks and their source: all of it but when it was made and what the patch said on its
    run (an idempotent patch run again on a reuse says it changed nothing)."""
    if not isinstance(lock, dict):
        return None
    out = {k: v for k, v in lock.items() if k != "created"}
    source = out.get("source")
    if isinstance(source, dict) and isinstance(source.get("patch"), dict):
        out["source"] = {**source, "patch": {k: v for k, v in source["patch"].items() if k != "said"}}
    return out


def check_lock(tasks_dir: Path, lock_path: Path) -> list[str]:
    """What differs between the tasks on disk and the lock: missing, added and changed tasks; or that the lock is
    unreadable (a file that cannot be read, JSON nested past the parser's depth, JSON too that is no object with the
    tasks' digests)."""
    try:
        lock = json.loads(Path(lock_path).read_text())
        if not isinstance(lock, dict) or not isinstance(lock.get("tasks") or {}, dict):
            raise ValueError("not an object with the tasks' digests")
    except (OSError, ValueError, RecursionError) as exc:
        return [f"lock unreadable: {exc}"]
    recorded = lock.get("tasks") or {}
    present = {t.name: t for t in task_dirs(tasks_dir)}
    problems = [f"task missing on disk: {n}" for n in sorted(set(recorded) - set(present))]
    problems += [f"task not in the lock: {n}" for n in sorted(set(present) - set(recorded))]
    for name in sorted(set(recorded) & set(present)):
        if digest(present[name]) != recorded[name]:
            problems.append(f"task changed since the lock: {name}")
    return problems


# ---- splits ------------------------------------------------------------------------------------------------------

def split(tasks_dir: Path, *, train: int, test, seed: int = 0, val: int = 0) -> dict[str, list[str]]:
    """A deterministic split of the task names: shuffled with `seed`, the first `train` for training, then `val`,
    then `test` (a number, or "rest": every task train and val leave). Rows of one task (its rollouts, its steps) go
    together; `index` expands names to rows."""
    names = [t.name for t in task_dirs(tasks_dir)]
    train, val = int(train), int(val or 0)
    test = len(names) - train - val if str(test) == "rest" else int(test)
    if test < 0 or train < 0 or val < 0 or train + val + test > len(names):
        raise ValueError(f"{train}+{val}+{test} tasks asked for, {len(names)} present")
    random.Random(seed).shuffle(names)
    return {"train": sorted(names[:train]), "val": sorted(names[train:train + val]),
            "test": sorted(names[train + val:train + val + test])}


def ids_for(rows: list[dict], names: list[str]) -> list[str]:
    """The row ids of the named tasks, in the rows' order (a stream's steps stay in order)."""
    wanted = set(names)
    return [r["id"] for r in rows if r["harbor_task"] in wanted]


# ---- download ----------------------------------------------------------------------------------------------------

def download(ref: str, output: Path, *, harbor: str, repo: str | None = None, registry_url: str | None = None,
             registry_path: str | None = None, overwrite: bool = False) -> Path:
    """`harbor datasets download <ref>` into `output` (export mode: `<output>/<dataset name>/<task>/`); the dataset
    directory. `ref` is `name@version` of the legacy registry, or with `repo` a git registry's dataset."""
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    cmd = [harbor, "datasets", "download", ref, "-o", str(output), "--export"]
    if repo:
        cmd += ["--repo", repo]
    if registry_url:
        cmd += ["--registry-url", registry_url]
    if registry_path:
        cmd += ["--registry-path", registry_path]
    if overwrite:
        cmd.append("--overwrite")
    before = {p.name for p in output.iterdir() if p.is_dir()}
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({run.returncode}): {(run.stderr or run.stdout).strip()[-2000:]}")
    name = ref.partition("@")[0].rsplit("/", 1)[-1]
    candidates = [output / name] + [p for p in output.iterdir() if p.is_dir() and p.name not in before]
    for candidate in candidates:
        if candidate.is_dir() and task_dirs(candidate):
            return candidate
    raise RuntimeError(f"harbor downloaded nothing recognisable into {output}: {run.stdout.strip()[-800:]}")


# ---- prepare: an example's data from its benchmark file -------------------------------------------------------------

def smoke_rows(tasks_dir: Path, names: list[str]) -> list[dict]:
    """The smoke rows of the named tasks: each task's rows (a multi-step task's steps) as rollout 0, `<id>#0`. An
    unknown name is an error (a smoke run of nothing would pass)."""
    present = {t.name: t for t in task_dirs(tasks_dir)}
    unknown = [n for n in names if n not in present]
    if unknown:
        raise ValueError(f"smoke names tasks not under {tasks_dir}: {unknown}")
    out = []
    for name in names:
        for row in rows_of(present[name]):
            out.append({**row, "id": f"{row['id']}#0", "rollout": 0})
    return out


def manifest_size(text: str, *, os_name: str = "linux", arch: str = "amd64") -> int | None:
    """An image's compressed size (its layers' bytes) from `docker manifest inspect -v`: one manifest, or a list of
    one per platform (this machine's, linux/amd64, taken)."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    entries = data if isinstance(data, list) else [data]

    def platform(entry):
        p = (entry.get("Descriptor") or {}).get("platform") or {}
        return p.get("os"), p.get("architecture")
    chosen = next((e for e in entries if isinstance(e, dict) and platform(e) == (os_name, arch)), None)
    if chosen is None and len(entries) == 1 and isinstance(entries[0], dict):
        chosen = entries[0]
    if chosen is None:
        return None
    manifest = chosen.get("SchemaV2Manifest") or chosen.get("OCIManifest") or chosen
    layers = manifest.get("layers") if isinstance(manifest, dict) else None
    if not isinstance(layers, list) or not layers:
        return None
    return sum(int(layer.get("size") or 0) for layer in layers if isinstance(layer, dict))


def docker_manifest(image: str) -> str:
    """`docker manifest inspect -v <image>`'s output (the registry's manifest; nothing is pulled)."""
    run = subprocess.run(["docker", "manifest", "inspect", "-v", image], capture_output=True, text=True, timeout=120)
    if run.returncode != 0:
        raise RuntimeError((run.stderr or run.stdout).strip()[-300:])
    return run.stdout


def image_sizes(tasks_dir: Path, *, inspect=docker_manifest, log=None) -> dict:
    """{image: compressed bytes} of every task's prebuilt image (task.toml's docker_image), from the registry's
    manifest (`inspect`: image -> `docker manifest inspect -v`'s text); an image whose manifest cannot be read is
    left out, and said."""
    say = log or (lambda m: print(m, file=sys.stderr))
    out: dict = {"_comment": "compressed bytes of each task's prebuilt image (docker manifest inspect -v, "
                             "linux/amd64): the runner's disk gate keeps 2.5x this free for an image not pulled yet"}
    images = sorted({str(img) for img in ((task_config(t).get("environment") or {}).get("docker_image")
                                           for t in task_dirs(tasks_dir)) if img})
    for image in images:
        try:
            size = manifest_size(inspect(image))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            say(f"image_sizes: {image}: {exc}")
            continue
        if size is None:
            say(f"image_sizes: {image}: no linux/amd64 layers in its manifest")
            continue
        out[image] = size
    return out


def load_patch(spec: str, base: Path):
    """The function `file.py:fn` names (the file relative to `base`, the benchmark file's directory), and the file's
    sha256."""
    file, sep, name = str(spec).rpartition(":")
    if not sep or not file.endswith(".py") or not name.isidentifier():
        raise ValueError(f"dataset.patch is file.py:function, not {spec!r}")
    path = (Path(base) / file).resolve()
    module_spec = importlib.util.spec_from_file_location(f"hermit_dataset_patch_{path.stem}", path)
    if module_spec is None or not path.is_file():
        raise ValueError(f"dataset.patch: no file {path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    fn = getattr(module, name, None)
    if not callable(fn):
        raise ValueError(f"dataset.patch: {path} has no function {name}")
    return fn, hashlib.sha256(path.read_bytes()).hexdigest()


def dataset_block(benchmark: Path) -> dict:
    """The `dataset:` block of a benchmark file, checked: its keys known, its ref name@version."""
    import yaml
    data = yaml.safe_load(Path(benchmark).read_text()) or {}
    block = data.get("dataset") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        raise ValueError(f"{benchmark} has no dataset: block")
    unknown = set(block) - DATASET_KEYS
    if unknown:
        raise ValueError(f"dataset: unknown keys {sorted(unknown)}; known: {sorted(DATASET_KEYS)}")
    ref = str(block.get("ref") or "")
    name, _, version = ref.partition("@")
    if not name or not version or version in ("head", "latest"):
        raise ValueError(f"dataset.ref is name@version, a version the registry keeps, not {ref!r} (a bare name is "
                         "the registry's @head, which moves)")
    split_spec = block.get("split", "in_sample")
    if split_spec != "in_sample" and not (isinstance(split_spec, dict) and set(split_spec) <= {"train", "val", "test",
                                                                                                   "seed"}
                                          and "train" in split_spec and "test" in split_spec):
        raise ValueError("dataset.split is in_sample or {train, val, test (a number or rest), seed}")
    return block


def data_dir(ref: str, data=None, base: Path | None = None, *, follow: bool = True) -> Path:
    """Where a task set is kept: `data` (a relative one under `base`, the benchmark file's folder for the block's own,
    else under the working directory), else <root>/<name>@<version> under $HERMIT_HARBOR_TASKS (or TASKS_ROOT); its
    links followed, or with `follow` false as it is spelled (made absolute)."""
    if data:
        path = Path(os.path.expandvars(str(data))).expanduser()
        path = Path(base) / path if base is not None else path
    else:
        path = Path(os.environ.get(TASKS_ROOT_ENV) or TASKS_ROOT).expanduser() / re.sub(r"[^A-Za-z0-9_.@-]", "_", ref)
    return path.resolve() if follow else path.absolute()


def _in_link(data: Path, link: Path) -> bool:
    """Whether the data directory `data` (absolute, as spelled) is `link` or in it: as spelled, each part with the
    folders above it resolved but not itself (a path read through the link would follow wherever the last prepare
    pointed it), or once resolved."""
    spelled = (p.parent.resolve() / p.name for p in (data, *data.parents) if p.name not in ("", ".."))
    return link in spelled or data.resolve().is_relative_to(link)


def _tasks_in(data: Path, ref: str) -> Path | None:
    name = ref.partition("@")[0].rsplit("/", 1)[-1]
    for candidate in (data / name, data):
        if candidate.is_dir() and task_dirs(candidate):
            return candidate
    return None


def _refuse_in_the_way(there: Path, ref: str) -> None:
    """A ValueError naming what is at `there` (a data directory, or an entry in one), when a download of `ref` may not
    replace it: only an empty directory can be replaced."""
    if os.path.lexists(there) and (there.is_symlink() or not there.is_dir() or any(there.iterdir())):
        raise ValueError(f"{there} holds none of {ref}'s tasks and is in the way of its download: move it away")


@contextmanager
def _downloading(partial: Path, ref: str):
    """Held while `ref` is downloaded into `partial`: an flock of `<partial>.lock`, so a second prepare downloading
    there at the same time is refused (a ValueError) instead of removing what the first one has downloaded so far; the
    file is removed when the download is done with (one that a prepare which was killed left is taken over). On a
    filesystem that keeps no file locks the download goes ahead unguarded, as it did before."""
    lock = partial.with_name(partial.name + ".lock")
    while True:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ValueError(f"another prepare is downloading {ref} into {partial}: wait for it to end, then prepare "
                             "again") from None
        except OSError as exc:                           # NFS without its lock daemon, Lustre without flock
            if exc.errno not in (errno.ENOLCK, errno.ENOSYS, errno.EOPNOTSUPP):
                os.close(fd)
                raise
            break
        try:
            if os.path.samestat(os.fstat(fd), os.stat(lock)):
                break
        except FileNotFoundError:
            pass
        os.close(fd)                                     # removed by the prepare that just ended: the new file's lock
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)
        os.close(fd)


def _download_whole(get, ref: str, data: Path) -> Path:
    """`get(ref, output)` (the tasks' directory under output) into a partial download's folder, an earlier one (a
    download that stopped) removed first, then moved to `data` once it returned: Harbor copies a dataset into its
    output a group of tasks at a time, and a data directory that holds tasks is reused as the whole set. The folder is
    `<data>/.<name>.partial` when `data` is there: on its filesystem and writable wherever `data` is (beside a data
    directory that is a mount point a download is on another filesystem, whose entries cannot be renamed into it, and
    beside one in a folder of others' none can be made), and never taken for a task by a listing of them (it holds the
    set's directory, no task.toml); else `<data>.partial` beside it, renamed to `data`. One prepare at a time
    downloads into it (_downloading). What is in the way, `data` itself when it is no directory (a file), or in `data`
    an entry of the download's name that is no empty directory (a directory holding none of the set's tasks, a file, a
    link), is never replaced: a ValueError names it, to be moved away, before the download when it is `data` itself or
    at `<data>/<name>`, the set's own directory (the name _tasks_in looks at and all Harbor's export writes), else once
    the download returned. The tasks' directory."""
    name = ref.partition("@")[0].rsplit("/", 1)[-1]
    if not data.is_dir():                                # data itself no directory (a file): in the way too
        _refuse_in_the_way(data, ref)
    _refuse_in_the_way(data / name, ref)                 # before anything is downloaded
    if data.is_dir():
        partial = data / f".{name}.partial"
    else:
        partial = data.with_name(data.name + ".partial")
        data.parent.mkdir(parents=True, exist_ok=True)
    with _downloading(partial, ref):
        if partial.is_dir() and not partial.is_symlink():
            shutil.rmtree(partial)
        elif os.path.lexists(partial):
            partial.unlink()
        tasks = Path(get(ref, partial)).resolve().relative_to(partial.resolve())
        if data.is_dir():                                # there already, with no tasks of the set: moved into it
            entries = sorted(partial.iterdir())
            for entry in entries:
                _refuse_in_the_way(data / entry.name, ref)
            for entry in entries:
                os.replace(entry, data / entry.name)
            partial.rmdir()
        else:
            os.replace(partial, data)
    return data / tasks


def _write_ids(path: Path, ids: list[str]) -> None:
    path.write_text("".join(i + "\n" for i in ids))


def prepare(benchmark: Path, *, force: bool = False, harbor: str = HARBOR, data=None, rollouts: int | None = None,
            download_fn=None, inspect=docker_manifest, log=None) -> dict:
    """An example's data from its benchmark file's `dataset:` block (the module's docstring); what was written.
    `download_fn(ref, output) -> tasks dir` replaces Harbor's download (a test's)."""
    say = log or print
    benchmark = Path(benchmark).resolve()
    here = benchmark.parent
    block = dataset_block(benchmark)
    ref = str(block["ref"])
    spelled = data_dir(ref, data, follow=False) if data else data_dir(ref, block.get("data"), base=here, follow=False)
    link = here / "tasks"                                # the link to the tasks prepare writes (below)
    if _in_link(spelled, link):                          # where the link goes: refused before a download or a reuse
        raise ValueError(f"the data directory {spelled} is the link prepare writes to the tasks ({link}) or in it: "
                         "name another")
    if link.exists() and not link.is_symlink():          # what is there instead of the link: refused alike
        what = "a directory" if link.is_dir() else "a file"
        raise ValueError(f"{link} is {what} of its own, not the link prepare writes: move it away")
    data = spelled.resolve()
    rollouts = int(rollouts or block.get("rollouts") or 1)
    lock_path = here / "dataset.lock"
    tasks = _tasks_in(data, ref)
    reused = tasks is not None
    if not reused:
        get = download_fn or (lambda r, out: download(r, out, harbor=str(Path(os.path.expanduser(harbor))),
                                                      repo=block.get("repo"), registry_url=block.get("registry_url"),
                                                      registry_path=block.get("registry_path")))
        try:
            tasks = _download_whole(get, ref, data)
        except OSError as exc:                           # a folder this user cannot write in, a lock another user owns
            raise ValueError(f"{ref} could not be downloaded into {data}: {exc}") from exc
    source: dict = {"ref": ref, **({"repo": block["repo"]} if block.get("repo") else {})}
    if block.get("patch"):
        fn, sha = load_patch(block["patch"], here)
        said = fn(tasks)
        source["patch"] = {"hook": str(block["patch"]), "sha256": sha,
                           **({"said": str(said)} if said not in (None, "") else {})}
    previous = None                                      # the lock as it is, when there is one
    if lock_path.is_file():                              # the tasks a run was prepared with: still these?
        problems = check_lock(tasks, lock_path)
        try:
            previous = json.loads(lock_path.read_text())
        except (OSError, ValueError, RecursionError):
            pass                                         # unreadable: check_lock said so
        locked = (previous.get("source") if isinstance(previous, dict) else None) or {}
        if not isinstance(locked, dict):
            problems.append("lock unreadable: its source is not an object")
            locked = {}
        if locked.get("ref") not in (None, ref):
            problems.insert(0, f"the lock is of {locked.get('ref')}, the benchmark names {ref}")
        def hook(src):                                   # the patch's identity (what it said on this run aside)
            patch = src.get("patch")
            return {k: patch.get(k) for k in ("hook", "sha256")} if isinstance(patch, dict) else patch
        if hook(locked) != hook(source):
            problems.append("the lock's patch is not the benchmark's")
        if problems and not force:
            raise ValueError(f"the tasks at {tasks} are not the ones {lock_path} locked (--force locks them anew): "
                             + "; ".join(problems[:10]))
    if link.is_symlink():
        link.unlink()
    link.symlink_to(tasks, target_is_directory=True)
    rows = index(tasks, rollouts=rollouts)
    smoke = smoke_rows(tasks, [str(n) for n in block.get("smoke") or []])
    write_jsonl(rows + smoke, here / "tasks.jsonl")
    lock = lock_of(tasks, source=source)
    if _locked_as(previous) != _locked_as(lock):         # the same tasks from the same source: the lock left as it is
        lock_path.write_text(json.dumps(lock, indent=1) + "\n")
    splits = here / "splits"
    splits.mkdir(exist_ok=True)
    ids = [r["id"] for r in rows]
    parts = {"all": ids, "smoke": [r["id"] for r in smoke]}
    split_spec = block.get("split", "in_sample")
    if split_spec == "in_sample":
        parts.update(train=ids, val=[], test=ids)
    else:
        names = split(tasks, train=split_spec["train"], val=split_spec.get("val", 0), test=split_spec["test"],
                      seed=int(split_spec.get("seed", 0)))
        parts.update({k: ids_for(rows, v) for k, v in names.items()})
    for name in SPLITS:
        _write_ids(splits / f"{name}.txt", parts[name])
    written = {"tasks": str(tasks), "rows": len(rows), "smoke": len(smoke), "reused": reused,
               "splits": {k: len(v) for k, v in parts.items()}}
    if block.get("image_sizes"):
        sizes = image_sizes(tasks, inspect=inspect, log=say)
        (here / "image_sizes.json").write_text(json.dumps(sizes, indent=1) + "\n")
        written["image_sizes"] = len(sizes) - 1
    say(f"{len(rows)} rows of {len(lock['tasks'])} tasks from {ref} at {tasks}"
        f"{' (reused)' if reused else ''}; splits: {written['splits']}")
    return written


# ---- command line ------------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="A benchmark's task set (Harbor's format) as Hermit data.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="an example's data from its benchmark file's dataset: block")
    p.add_argument("benchmark", type=Path)
    p.add_argument("--force", action="store_true", help="re-lock tasks that differ from the lock")
    p.add_argument("--harbor", default=HARBOR)
    p.add_argument("--data", help="where the tasks go (default: the block's data, else $HERMIT_HARBOR_TASKS/<ref>)")
    p.add_argument("--rollouts", type=int, help="rows per task (default: the block's rollouts)")
    p = sub.add_parser("download", help="harbor datasets download <ref> into --output")
    p.add_argument("ref")
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--harbor", default=HARBOR)
    p.add_argument("--repo")
    p.add_argument("--registry-url")
    p.add_argument("--registry-path")
    p.add_argument("--overwrite", action="store_true")
    p = sub.add_parser("index", help="write tasks.jsonl rows for the tasks")
    p.add_argument("tasks_dir", type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--rollouts", type=int, default=1)
    p.add_argument("--include-file", type=Path, help="task names to include, one per line")
    p = sub.add_parser("lock", help="write a lock of the tasks' contents")
    p.add_argument("tasks_dir", type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--source", help="where the tasks came from (a dataset ref, a URL)")
    p = sub.add_parser("check", help="compare the tasks with a lock")
    p.add_argument("tasks_dir", type=Path)
    p.add_argument("--lock", required=True, type=Path)
    p = sub.add_parser("splits", help="write train/val/test id lists")
    p.add_argument("tasks_dir", type=Path)
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--train", type=int, required=True)
    p.add_argument("--test", type=int, required=True)
    p.add_argument("--val", type=int, default=0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--rollouts", type=int, default=1)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        try:
            prepare(args.benchmark, force=args.force, harbor=args.harbor, data=args.data, rollouts=args.rollouts)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    elif args.command == "download":
        where = download(args.ref, args.output, harbor=str(Path(args.harbor).expanduser()), repo=args.repo,
                         registry_url=args.registry_url, registry_path=args.registry_path, overwrite=args.overwrite)
        print(where)
    elif args.command == "index":
        include = [l.strip() for l in args.include_file.read_text().splitlines() if l.strip()] if args.include_file else None
        rows = index(args.tasks_dir, rollouts=args.rollouts, include=include)
        write_jsonl(rows, args.out)
        print(f"{len(rows)} rows of {len({r['harbor_task'] for r in rows})} tasks -> {args.out}")
    elif args.command == "lock":
        lock = lock_of(args.tasks_dir, source={"ref": args.source} if args.source else {})
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(lock, indent=1) + "\n")
        print(f"{len(lock['tasks'])} tasks locked -> {args.out}")
    elif args.command == "check":
        problems = check_lock(args.tasks_dir, args.lock)
        for problem in problems:
            print(problem)
        print("ok" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    elif args.command == "splits":
        parts = split(args.tasks_dir, train=args.train, test=args.test, val=args.val, seed=args.seed)
        rows = index(args.tasks_dir, rollouts=args.rollouts)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for name, names in parts.items():
            if names:
                (args.out_dir / f"{name}.txt").write_text("".join(i + "\n" for i in ids_for(rows, names)))
        print({k: len(v) for k, v in parts.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
