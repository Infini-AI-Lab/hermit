"""Prepare training-only OfficeQA fixtures for the CoEvoSkills loop.

No replacement dataset, split, prompt builder or evaluator. Run utils/prepare.py
first, or supply --csv and --docs to invoke it here. Only the container contract
is new: answer.txt lets a surrogate inspect the produced artifact independently.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
# Upstream bounds each oracle check by the package's agent timeout; the oracle here is a whole acting session (the
# configs' codex.timeout_s: 3,600 s) plus its container's start and the adapter's copy.
ORACLE_TIMEOUT_S = 4500
UTILS = HERE.parent / "utils"


def prepare(dest: Path, *, build_image=True, oracle_timeout_s: int = ORACLE_TIMEOUT_S):
    from hermit.task import load_callable
    from skillopt.envs.officeqa import evaluator
    from skillopt.envs.officeqa.tool_runtime import resolve_docs_roots
    task_text = load_callable(str(UTILS / "task.py") + ":task_text", HERE)
    data = json.loads((UTILS / "data.json").read_text())
    rows = {row["id"]: row for row in map(json.loads, (UTILS / "tasks.jsonl").read_text().splitlines())}
    training_ids = (UTILS / "splits/train.txt").read_text().split()
    if len(set(training_ids)) != len(training_ids) or not set(training_ids) <= rows.keys():
        raise ValueError("training split contains duplicate or unknown OfficeQA IDs")
    rows = [rows[tid] for tid in training_ids]
    answers = {r["id"]: r["answer"] for r in map(json.loads, Path(data["answers"]).read_text().splitlines())}
    roots = resolve_docs_roots([data["docs"]])
    # Training fixtures share one corpus image; evaluation uses the original actor.
    context = dest / "image"
    context.mkdir(parents=True)
    for index, root in enumerate(roots, 1):
        shutil.copytree(root, context / "docs" / f"root_{index}")
    corpus_hash = hashlib.sha256()
    for path in sorted((context / "docs").rglob("*")):
        if path.is_file():
            corpus_hash.update(str(path.relative_to(context)).encode())
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1048576), b""):
                    corpus_hash.update(chunk)
    image = "officeqa-coevoskills:" + corpus_hash.hexdigest()[:16]
    (context / "Dockerfile").write_text(
        "FROM python:3.12-slim\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends bash tmux curl ca-certificates "
        "&& rm -rf /var/lib/apt/lists/*\n"
        "RUN pip install --no-cache-dir pytest pyyaml\n"
        "WORKDIR /root\nCOPY docs /root/docs\n")
    if build_image:
        subprocess.run(["docker", "build", "-t", image, str(context)], check=True)
    for row in rows:
        tid = row["id"]
        from hermit.agent.evolver.methods.coevoskills_adapter.upstream import safe_name
        safe_name(tid)
        package = dest / "packages" / tid.lower()
        environment = package / "environment"
        tests = package / "tests"
        (environment / "doc").mkdir(parents=True)
        (environment / "skills").mkdir()
        tests.mkdir()
        text, _ = task_text(row, data["docs"])
        # As upstream's tasks: the instruction says where the result goes and in what form, and the task's background
        # document (upstream: artifacts/background_docs/<task>/, given to the Generator and the Verifier, removed for
        # the oracle) is the task's own description, ../utils/task-info/README.md, with nothing added.
        (package / "instruction.md").write_text(
            text + "\n\nSave the final answer to `/root/answer.txt` as `<answer>...</answer>`.\n")
        (environment / "doc/officeqa.md").write_text((UTILS / "task-info/README.md").read_text())
        (environment / "Dockerfile").write_text(
            f"FROM {image}\nCOPY doc /app/environment/doc\n")
        (package / "task.toml").write_text(
            'version = "1.0"\n[metadata]\nbenchmark = "officeqa"\n'
            f'[agent]\ntimeout_sec = {int(oracle_timeout_s)}\n[verifier]\ntimeout_sec = 60\n'
            '[environment]\nbuild_timeout_sec = 600\ncpus = 1\nmemory_mb = 4096\nstorage_mb = 10240\n')
        # Reuse the installed, pinned SkillOpt evaluator verbatim. Ground truth exists
        # only under tests/: Harbor injects it after agent execution, never in the image.
        (tests / "evaluator.py").write_text(inspect.getsource(evaluator))
        (tests / "answer.json").write_text(json.dumps(answers[tid]))
        (tests / "test_outputs.py").write_text(
            'import json, re\nfrom pathlib import Path\nfrom evaluator import evaluate\n'
            'def test_answer():\n'
            '    text = Path("/root/answer.txt").read_text()\n'
            '    matches = re.findall(r"<answer>(.*?)</answer>", text, re.I | re.S)\n'
            '    answer = matches[-1].strip() if matches else ""\n'
            '    Path("/logs/verifier/answer.json").write_text(json.dumps({"answer": answer}))\n'
            '    gold = json.loads(Path("/tests/answer.json").read_text())\n'
            '    assert answer and evaluate(answer, gold)["em"] == 1.0\n')
        (tests / "test.sh").write_text(
            '#!/bin/bash\nmkdir -p /logs/verifier\n'
            'if python -m pytest -q /tests/test_outputs.py --junitxml=/logs/verifier/results.xml; then\n'
            '  echo 1 > /logs/verifier/reward.txt\nelse\n  echo 0 > /logs/verifier/reward.txt\nfi\n')
    dataset_manifest = Path(data["csv"]).parent / "hermit-revision.json" if data.get("csv") else None
    dataset_revision = json.loads(dataset_manifest.read_text()) if dataset_manifest and dataset_manifest.is_file() else None
    (dest / "revision.json").write_text(json.dumps({
        "dataset": dataset_revision,
        "source": data, "corpus_sha256": corpus_hash.hexdigest(), "image": image,
        "training_ids": training_ids,
        "protocol": "frozen OfficeQA adapter; existing train/val/test split",
        "evaluator": "skillopt.envs.officeqa.evaluator (verbatim)",
        "prompt_builder": "../utils/task.py:task_text"}, indent=2))
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--docs", type=Path)
    parser.add_argument("--split-dir", type=Path)
    parser.add_argument("--output", type=Path, default=HERE / "data")
    parser.add_argument("--no-build", action="store_true", help="stage files; build corpus image before real runs")
    parser.add_argument("--oracle-timeout-s", type=int, default=ORACLE_TIMEOUT_S,
                        help="the oracle's clock per check (upstream: the package's agent timeout); longer than the "
                             "acting agent's session (codex.timeout_s) and its start")
    args = parser.parse_args()
    if args.csv or args.docs:
        if not (args.csv and args.docs):
            parser.error("--csv and --docs must be supplied together")
        command = [sys.executable, str(UTILS / "prepare.py"), "--csv", str(args.csv), "--docs", str(args.docs)]
        if args.split_dir:
            command += ["--split-dir", str(args.split_dir)]
        subprocess.run(command, check=True)
    if not (UTILS / "data.json").is_file():
        raise SystemExit("Existing OfficeQA data is not prepared: run ../utils/prepare.py, or supply --csv and --docs.")
    if args.output.exists():
        raise SystemExit("output already exists; choose a fresh --output")
    args.output.mkdir(parents=True)
    count = prepare(args.output.resolve(), build_image=not args.no_build, oracle_timeout_s=args.oracle_timeout_s)
    print(f"Prepared {count} OfficeQA training fixtures")


if __name__ == "__main__":
    main()
