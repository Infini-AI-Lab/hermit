#!/usr/bin/env python3
"""Write LiveMathBench's task directories from its official dataset on Hugging Face, at a pinned revision.

    python examples/livemathbench/utils/benchmark/prepare.py

The dataset (huggingface.co/datasets/opencompass/LiveMathBench) is gated: accept its terms on Hugging Face with your
account, then sign in on this machine (`hf auth login`, or HF_TOKEN). Writes here (utils/benchmark/): tasks/ (one task
directory per question in Harbor's format: instruction.md, OpenCompass's English prompt and the question; task.toml,
its release, set, question type (202412) and subject (202505), no network but the model's, an hour; no tests/: the judge runs on the host), and
the references with each question and its type to .private/answers.jsonl, which no sandbox is granted (the judge
model reads all three). The splits under splits/ are prepare's (not committed: the
dataset's terms come first): test.txt, the latest release's English questions
(202505 `all_en`); train.txt (40), val.txt (20) and smoke.txt (2), drawn with seed 42 from the 202412 release's English
sets (AMC, CCEE, CNMO, WLPMC), leaving out any question the test set has.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import random
import shutil
import urllib.error
import urllib.request

from hermit.task.directory import write_task

HERE = Path(__file__).resolve().parent
REPO, REVISION = "opencompass/LiveMathBench", "ea05b95ab431f3ef3900ce86f864451cf5bcd699"
TEST = ("202505", "all")
LEARN = [("202412", name) for name in ("AMC", "CCEE", "CNMO", "WLPMC")]
# OpenCompass's PROMPT_EN (opencompass/datasets/livemathbench/prompts.py, commit 4f3812d), the question in its place
PROMPT = "Here is a math question, please reasoning step by step, and put your answer in \\boxed{}.\n"
SIZES = {"train": 40, "val": 20, "smoke": 2}
TIMEOUT_SEC = 3600.0


def token() -> str | None:
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"]
    path = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "token"
    return path.read_text().strip() if path.is_file() else None


def rows(release: str, name: str) -> list[dict]:
    url = f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/{release}/{name}_en.jsonl"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {t}"} if (t := token()) else {})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            text = response.read().decode()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise SystemExit(f"{REPO} is gated ({exc.code}): accept its terms on huggingface.co with your account, then "
                             "sign in here (`hf auth login`, or HF_TOKEN)") from None
        raise
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def questions(release: str, name: str) -> list[dict]:
    return [{"id": f"v{release}-{name.lower()}-{i:03d}", "question": str(r["question"]).strip(),
             "answer": str(r["answer"]).strip(), "question_type": str(r.get("question_type") or ""),
             "subject": str(r.get("type") or ""),            # 202505 gives a subject (`type`), 202412 a question type
             "release": release, "set": name} for i, r in enumerate(rows(release, name))]


def main() -> None:
    test = questions(*TEST)
    seen = {q["question"] for q in test}
    learn = [q for release, name in LEARN for q in questions(release, name)]
    pool = [q for q in learn if q["question"] not in seen]
    need = sum(SIZES.values())
    if len(pool) < need:
        raise SystemExit(f"the 202412 English sets hold {len(pool)} questions the test set does not, fewer than {need}")
    random.Random(42).shuffle(pool)
    drawn, at = {}, 0
    for name, n in SIZES.items():
        drawn[name], at = pool[at:at + n], at + n
    kept = test + [q for part in drawn.values() for q in part]
    if (HERE / "tasks").exists():                              # this script's own output: rewritten whole
        shutil.rmtree(HERE / "tasks")
    for q in kept:
        write_task(HERE / "tasks" / q["id"], PROMPT + q["question"],
                   metadata={"release": q["release"], "set": q["set"], "question_type": q["question_type"], "subject": q["subject"],
                             "source": f"{REPO}@{REVISION}"},
                   environment={"network_mode": "no-network"},  # the agent reaches its model only
                   agent={"timeout_sec": TIMEOUT_SEC})
    private = HERE / ".private"
    private.mkdir(mode=0o700, exist_ok=True)
    (private / "answers.jsonl").write_text("".join(
        json.dumps({k: q[k] for k in ("id", "answer", "question", "question_type")}, ensure_ascii=False) + "\n"
        for q in kept))
    (private / "answers.jsonl").chmod(0o600)
    splits = {"test": [q["id"] for q in test], **{name: sorted(q["id"] for q in part) for name, part in drawn.items()}}
    (HERE / "splits").mkdir(exist_ok=True)
    for name, ids in splits.items():
        (HERE / "splits" / f"{name}.txt").write_text("\n".join(ids) + "\n")
    print({name: len(ids) for name, ids in splits.items()}, "tasks:", len(kept),
          "202412 questions left out as test questions:", len(learn) - len(pool))


if __name__ == "__main__":
    main()
