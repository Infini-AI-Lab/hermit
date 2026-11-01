#!/usr/bin/env python3
"""OfficeQA in the SkillOpt paper's setting as a Harbor task set: one task directory per question, run by
`agent.name: harbor` (the Codex harness in a container Harbor starts, instead of utils/agent/codex_sandbox.py).

    python examples/officeqa/utils/harbor/build_tasks.py                 # after utils/prepare.py
        [--out utils/.private/harbor_tasks] [--ids utils/splits/test.txt] [--limit N] [--base-image ubuntu:24.04]

Each task is `<out>/<UID>/`: `task.toml` (the question's metadata, a 3600 s agent budget as the Codex route's),
`instruction.md` (the task text SkillOpt's Codex harness writes to task.md: the question, its oracle parsed pages,
the corpus note, the candidate files, the source hints, with the answer instruction), `environment/Dockerfile`
(`--base-image` plus python3, ripgrep and CA certificates; the same for every task, so Harbor builds one image), and
`tests/` (`test.sh`, `judge.py`, `gold.json`: the exact-match judge of utils/judge.py, run inside the container after
the agent, so the answer is never where the agent can read it). The corpus is not in the image: the config mounts
it read-only at `/app/docs/root_1`, where the instruction's candidate paths point (`MOUNTS.json` beside the tasks
says which host directory). `<out>/tasks.jsonl` holds the rows for `data.tasks` (the same UIDs as utils/tasks.jsonl,
so utils/splits/ apply unchanged).

Needs the skillopt extra (the instruction is rendered with SkillOpt's own `_build_user`) and the answers
utils/prepare.py wrote to utils/.private/answers.jsonl. The output is private: it holds the gold answers.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import stat
import sys

HERE = Path(__file__).resolve().parent
UTILS = HERE.parent
sys.path.insert(0, str(UTILS / "agent"))

INSTRUCTION = ("Inspect or search the full OfficeQA corpus under `docs/` and answer the question below. Treat the "
               "candidate files as hints, not an access limit. Return the final answer inside <answer>...</answer> in "
               "your final message (writing the same answer to `/app/answer.txt` is also accepted).\n\n")
DOCKERFILE = """FROM {base}
RUN if command -v apt-get >/dev/null 2>&1; then apt-get update \\
    && apt-get install -y --no-install-recommends python3 ca-certificates curl ripgrep \\
    && rm -rf /var/lib/apt/lists/*; fi
RUN mkdir -p /app/docs/root_1
WORKDIR /app
"""
TEST_SH = """#!/bin/bash
# The OfficeQA judge, after the agent: /tests/judge.py reads the answer and writes /logs/verifier/reward.txt.
mkdir -p /logs/verifier
python3 /tests/judge.py
[ -f /logs/verifier/reward.txt ] || echo 0 > /logs/verifier/reward.txt
exit 0
"""


def _toml(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def task_toml(row: dict) -> str:
    return "\n".join([
        'schema_version = "1.4"', "",
        "[metadata]",
        'benchmark = "OfficeQA"', 'source = "skillopt-setting"', f"uid = {_toml(row['id'])}",
        f"category = {_toml(row.get('category') or 'officeqa')}",
        f"source_files = {_toml(list(row.get('source_files') or []))}",
        f"source_docs = {_toml(list(row.get('source_docs') or []))}", "",
        "[verifier]", "timeout_sec = 60.0", "",
        "[agent]", "timeout_sec = 3600.0", "",
        "[environment]", "build_timeout_sec = 900.0", "cpus = 2", "memory_mb = 4096", 'workdir = "/app"', ""])


def build(out: Path, rows: list[dict], answers: dict[str, str], docs: str, base_image: str) -> list[str]:
    from codex_agent import task_text                     # SkillOpt's task.md, and the corpus roots it names
    out.mkdir(parents=True, exist_ok=True)
    roots: list[str] = []
    written = []
    for row in rows:
        text, roots = task_text(row, docs)
        task = out / row["id"]
        if task.exists():
            shutil.rmtree(task)
        (task / "environment").mkdir(parents=True)
        (task / "tests").mkdir()
        (task / "task.toml").write_text(task_toml(row))
        (task / "instruction.md").write_text(INSTRUCTION + text.rstrip() + "\n")
        (task / "environment" / "Dockerfile").write_text(DOCKERFILE.format(base=base_image))
        test = task / "tests" / "test.sh"
        test.write_text(TEST_SH)
        test.chmod(test.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        shutil.copy2(HERE / "judge.py", task / "tests" / "judge.py")
        (task / "tests" / "gold.json").write_text(json.dumps({"id": row["id"], "answer": answers[row["id"]]},
                                                             ensure_ascii=False) + "\n")
        written.append(row["id"])
    mounts = [{"type": "bind", "source": str(Path(r).resolve()), "target": f"/app/docs/root_{i}", "read_only": True}
              for i, r in enumerate(roots, start=1)]
    (out / "MOUNTS.json").write_text(json.dumps(mounts, indent=1) + "\n")
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=UTILS / ".private" / "harbor_tasks")
    parser.add_argument("--ids", type=Path, help="only these task ids (one per line; default: every task)")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--base-image", default="ubuntu:24.04",
                        help="the image the tasks build on; one with Codex installed saves Harbor's install per trial")
    parser.add_argument("--docs", help="treasury_bulletins_parsed/ (default: utils/data.json's)")
    args = parser.parse_args(argv)
    data = json.loads((UTILS / "data.json").read_text())
    rows = [json.loads(l) for l in (UTILS / "tasks.jsonl").read_text().splitlines() if l.strip()]
    if args.ids:
        wanted = {l.strip() for l in args.ids.read_text().splitlines() if l.strip() and not l.startswith("#")}
        rows = [r for r in rows if r["id"] in wanted]
    if args.limit:
        rows = rows[:args.limit]
    answers = {r["id"]: r["answer"] for r in map(json.loads, Path(data["answers"]).read_text().splitlines()) if r}
    missing = [r["id"] for r in rows if r["id"] not in answers]
    if missing:
        raise SystemExit(f"no answer for {missing[:5]}: run utils/prepare.py first")
    out = args.out.resolve()
    written = build(out, rows, answers, args.docs or data["docs"], args.base_image)
    from hermit.harbor import dataset
    index = dataset.index(out, include=written)
    by_id = {r["id"]: r for r in rows}
    for item in index:
        item["question"] = by_id[item["id"]]["question"]
    dataset.write_jsonl(index, out / "tasks.jsonl")
    mounts = json.loads((out / "MOUNTS.json").read_text())
    print(f"{len(written)} tasks -> {out}  (tasks.jsonl beside them)")
    print("the corpus mount for the config's agent.params.environment.mounts:")
    print("  export OFFICEQA_DOCS_ROOT=" + mounts[0]["source"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
