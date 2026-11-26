#!/usr/bin/env python3
"""Write AIME's task directories from MathArena's datasets on Hugging Face, at pinned revisions.

    python examples/aime/utils/benchmark/prepare.py

Writes here (utils/benchmark/): tasks/ (one task directory per problem in Harbor's format: instruction.md, MathArena's
AIME prompt and the problem; task.toml, its year and number, no network but the model's, an hour; no tests/: the judge
runs on the host), and the references to .private/answers.jsonl, which no sandbox is granted. The splits under splits/
are committed: AIME 2024's 30 problems for learning (train 20, validation 10, seed 42), AIME 2025's and AIME 2026's 30
each for testing, and two AIME 2023 problems for the smoke, which no run takes.

Needs pyarrow (`python -m pip install -r examples/math_grading/requirements.txt`).
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import random
import shutil
import urllib.request

from hermit.task.directory import write_task

HERE = Path(__file__).resolve().parent
# MathArena's datasets (huggingface.co/datasets/MathArena/<name>) and their revisions. aime_2025 and aime_2026 hold
# both papers, AIME I as problems 1-15 and AIME II as 16-30; 2024's and 2023's are a dataset a paper.
SOURCES = {
    "aime2023-I": ("aime_2023_I", "4e55d1bf38f277ab89ba7fc987f76604a6c3877c", 0),
    "aime2024-I": ("aime_2024_I", "ea5b061c3e8039dc9858defaafc407d04b995e9f", 0),
    "aime2024-II": ("aime_2024_II", "29d5d31e9b46e215fc24d9b2a3047506823dd101", 15),
    "aime2025": ("aime_2025", "c94da77eb22bbd6439e62a323bec18493a421302", 0),
    "aime2026": ("aime_2026", "d61f486805306e6e6b5913504c07763a99e1b112", 0),
}
# MathArena's AIME instruction (configs/competitions/aime/aime_2025.yaml at github.com/eth-sri/matharena commit d98156f),
# followed by the problem as its runner does (src/matharena/runner.py: the instruction, a blank line, the problem)
INSTRUCTION = "Put your final answer within \\boxed{}.\nThe answer is an integer between 0 and 999 inclusive."
SMOKE = ["aime2023-01", "aime2023-02"]
TIMEOUT_SEC = 3600.0


def rows(name: str, revision: str) -> list[dict]:
    import pyarrow.parquet as pq
    url = f"https://huggingface.co/datasets/MathArena/{name}/resolve/{revision}/data/train-00000-of-00001.parquet"
    with urllib.request.urlopen(url, timeout=120) as response:
        return pq.read_table(io.BytesIO(response.read())).to_pylist()


def problems() -> list[dict]:
    out = []
    for source, (name, revision, offset) in SOURCES.items():
        year = int(source[4:8])
        for row in rows(name, revision):
            number = int(row["problem_idx"]) + offset          # 1-30 over the year's two papers
            out.append({"id": f"aime{year}-{number:02d}", "year": year, "number": number,
                        "paper": "I" if number <= 15 else "II", "problem": str(row["problem"]).strip(),
                        "answer": str(row["answer"]).strip(), "source": f"MathArena/{name}@{revision}"})
    return out


def main() -> None:
    every = problems()
    years = {y: [p for p in every if p["year"] == y] for y in (2023, 2024, 2025, 2026)}
    assert [len(years[y]) for y in (2024, 2025, 2026)] == [30, 30, 30], {y: len(v) for y, v in years.items()}
    kept = [p for p in every if p["year"] != 2023 or p["id"] in SMOKE]
    if (HERE / "tasks").exists():                              # this script's own output: rewritten whole
        shutil.rmtree(HERE / "tasks")
    for p in kept:
        write_task(HERE / "tasks" / p["id"], f"{INSTRUCTION}\n\n{p['problem']}",
                   metadata={"year": p["year"], "paper": p["paper"], "number": p["number"], "source": p["source"]},
                   environment={"network_mode": "no-network"},  # the agent reaches its model only
                   agent={"timeout_sec": TIMEOUT_SEC})
    private = HERE / ".private"
    private.mkdir(mode=0o700, exist_ok=True)
    (private / "answers.jsonl").write_text("".join(json.dumps({"id": p["id"], "answer": p["answer"]}) + "\n"
                                                    for p in kept))
    (private / "answers.jsonl").chmod(0o600)

    learn = sorted(p["id"] for p in years[2024])
    random.Random(42).shuffle(learn)
    splits = {"train": sorted(learn[:20]), "val": sorted(learn[20:]), "smoke": SMOKE,
              "test_2025": [p["id"] for p in years[2025]], "test_2026": [p["id"] for p in years[2026]]}
    (HERE / "splits").mkdir(exist_ok=True)
    for name, ids in splits.items():
        (HERE / "splits" / f"{name}.txt").write_text("\n".join(ids) + "\n")
    print({name: len(ids) for name, ids in splits.items()}, "tasks:", len(kept))


if __name__ == "__main__":
    main()
