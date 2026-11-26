#!/usr/bin/env python3
"""Write MATH-500's task directories, and the MATH training problems a method learns on, from Hugging Face at pinned
revisions.

    python examples/math500/utils/benchmark/prepare.py

Writes here (utils/benchmark/): tasks/ (one task directory per problem in Harbor's format: instruction.md, MATH-500's
own prompt and the problem; task.toml, its subject and level, no network but the model's, half an hour; no tests/: the
judge runs on the host), and the references to .private/answers.jsonl, which no sandbox is granted. The splits under
splits/ are committed: test.txt, MATH-500's 500 problems (HuggingFaceH4/MATH-500, from MATH's test split); train.txt
(100), val.txt (50) and smoke.txt (2), drawn with seed 42 from MATH's training split (EleutherAI/hendrycks_math), whose
reference is the last \\boxed{} of its solution, as MATH-500's answers are.

Needs pyarrow (`python -m pip install -r examples/math_grading/requirements.txt`).
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import random
import shutil
import sys
import urllib.request

from hermit.task.directory import write_task

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "math_grading"))
from grade import last_boxed  # noqa: E402

MATH500 = ("HuggingFaceH4/MATH-500", "6e4ed1a2a79af7d8630a6b768ec859cb5af4d3be")
MATH = ("EleutherAI/hendrycks_math", "21a5633873b6a120296cce3e2df9d5550074f4a3")
SUBJECTS = ("algebra", "counting_and_probability", "geometry", "intermediate_algebra", "number_theory", "prealgebra",
            "precalculus")
# MATH-500's own prompt (its eval.yaml on Hugging Face), the problem in place of {prompt}
PROMPT = ("Solve the following math problem efficiently and clearly.  The last line of your response should be of the "
          "following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without "
          "quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step "
          "before answering.\n\n")
SIZES = {"train": 100, "val": 50, "smoke": 2}
TIMEOUT_SEC = 1800.0


def fetch(repo: str, revision: str, path: str) -> bytes:
    url = f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{path}"
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def test_problems() -> list[dict]:
    rows = [json.loads(line) for line in fetch(*MATH500, "test.jsonl").decode().splitlines() if line.strip()]
    return [{"id": r["unique_id"].removesuffix(".json").replace("/", "-"),         # test-precalculus-807
             "problem": r["problem"].strip(), "answer": r["answer"].strip(), "subject": r["subject"],
             "level": int(r["level"]), "source": f"{MATH500[0]}@{MATH500[1]}"} for r in rows]


def train_problems() -> list[dict]:
    import pyarrow.parquet as pq
    out = []
    for subject in SUBJECTS:
        table = pq.read_table(io.BytesIO(fetch(*MATH, f"{subject}/train-00000-of-00001.parquet"))).to_pylist()
        for i, r in enumerate(table):
            answer = last_boxed(r["solution"])
            if answer and r["level"].startswith("Level ") and r["level"][6:].isdigit():
                out.append({"id": f"train-{subject}-{i}", "problem": r["problem"].strip(), "answer": answer,
                            "subject": r["type"], "level": int(r["level"][6:]), "source": f"{MATH[0]}@{MATH[1]}"})
    return out


def main() -> None:
    test, pool = test_problems(), train_problems()
    assert len(test) == 500, len(test)
    assert not {p["problem"] for p in test} & {p["problem"] for p in pool}, "a training problem is a test problem"
    random.Random(42).shuffle(pool)
    drawn, at = {}, 0
    for name, n in SIZES.items():
        drawn[name], at = pool[at:at + n], at + n
    kept = test + [p for part in drawn.values() for p in part]
    if (HERE / "tasks").exists():                              # this script's own output: rewritten whole
        shutil.rmtree(HERE / "tasks")
    for p in kept:
        write_task(HERE / "tasks" / p["id"], PROMPT + p["problem"],
                   metadata={"subject": p["subject"], "level": p["level"], "source": p["source"]},
                   environment={"network_mode": "no-network"},  # the agent reaches its model only
                   agent={"timeout_sec": TIMEOUT_SEC})
    private = HERE / ".private"
    private.mkdir(mode=0o700, exist_ok=True)
    (private / "answers.jsonl").write_text("".join(json.dumps({"id": p["id"], "answer": p["answer"]}) + "\n"
                                                    for p in kept))
    (private / "answers.jsonl").chmod(0o600)
    splits = {"test": [p["id"] for p in test], **{name: sorted(p["id"] for p in part) for name, part in drawn.items()}}
    (HERE / "splits").mkdir(exist_ok=True)
    for name, ids in splits.items():
        (HERE / "splits" / f"{name}.txt").write_text("\n".join(ids) + "\n")
    print({name: len(ids) for name, ids in splits.items()}, "tasks:", len(kept))


if __name__ == "__main__":
    main()
