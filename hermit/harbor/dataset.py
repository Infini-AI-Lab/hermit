"""A Harbor task set as Hermit data: the rows of `tasks.jsonl`, a lock of the tasks' contents, splits, a download.

A Harbor task is a directory: `task.toml`, `instruction.md`, `environment/` (a Dockerfile or a prebuilt
`docker_image`), `tests/test.sh` writing `/logs/verifier/reward.txt`. A multi-step task adds `[[steps]]` to task.toml
and `steps/<name>/{instruction.md, tests/}`: one environment that lives through every step (a stream).

    python -m hermit.harbor.dataset download terminal-bench@2.0 --output ~/.cache/hermit/harbor/datasets
    python -m hermit.harbor.dataset index <tasks dir> --out utils/tasks.jsonl [--rollouts 2]
    python -m hermit.harbor.dataset lock <tasks dir> --out utils/dataset.lock [--source terminal-bench@2.0]
    python -m hermit.harbor.dataset check <tasks dir> --lock utils/dataset.lock
    python -m hermit.harbor.dataset splits <tasks dir> --out-dir utils/splits --train 50 --test 39 --seed 0

The rows: one per task, or one per (task, rollout) when `--rollouts k` asks for k independent attempts
(`<task>#<k>`), or one per (task, step) for a multi-step task (`<task>:<step>`); every row carries the task's
directory name as `harbor_task`, its instruction, its timeouts and metadata, and a step row its `step`,
`step_index` (from 1) and `steps` (how many). The lock records the sha256 of every task directory's contents, so a
run can tell that the tasks it scores are the ones it was prepared with.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tomllib

TASK_FILE = "task.toml"
SKIP_DIRS = {".git", "__pycache__"}
LOCK_SCHEMA = 1


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
            "docker_image": env.get("docker_image"), "metadata": config.get("metadata") or {}}
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
    """sha256 of the task directory's contents: every file's relative path, mode bits and bytes, in path order
    (what RSIHub's dataset manifests hash too)."""
    task_dir = Path(task_dir)
    h = hashlib.sha256()
    for top, dirs, names in os.walk(task_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            path = Path(top) / name
            if name.endswith(".pyc") or path.is_symlink() and not path.exists():
                continue
            rel = path.relative_to(task_dir).as_posix()
            mode = path.stat().st_mode & 0o777
            h.update(f"{rel}\0{mode:o}\0".encode())
            h.update(path.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def lock_of(tasks_dir: Path, *, source: dict | None = None) -> dict:
    tasks = {t.name: digest(t) for t in task_dirs(tasks_dir)}
    return {"schema": LOCK_SCHEMA, "source": source or {}, "tasks_dir": str(Path(tasks_dir).resolve()),
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tasks": tasks}


def check_lock(tasks_dir: Path, lock_path: Path) -> list[str]:
    """What differs between the tasks on disk and the lock: missing, added and changed tasks."""
    try:
        lock = json.loads(Path(lock_path).read_text())
    except (OSError, ValueError) as exc:
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

def split(tasks_dir: Path, *, train: int, test: int, seed: int = 0, val: int = 0) -> dict[str, list[str]]:
    """A deterministic split of the task names: shuffled with `seed`, the first `train` for training, then `val`,
    then `test`. Rows of one task (its rollouts, its steps) go together; `index` expands names to rows."""
    names = [t.name for t in task_dirs(tasks_dir)]
    if train + val + test > len(names):
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


# ---- command line ------------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="A Harbor task set as Hermit data.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("download", help="harbor datasets download <ref> into --output")
    p.add_argument("ref")
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--harbor", default="~/.cache/hermit/harbor-venv/bin/harbor")
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
    if args.command == "download":
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
