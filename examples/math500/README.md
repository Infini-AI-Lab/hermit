# MATH-500

MATH-500 in Hermit's own environment: each problem is a task directory, the acting agent (Codex or Claude Code) solves
it in a bubblewrap sandbox of its own with no network but its model's, and a judge on the host scores the last
`\boxed{}` of its final message against the reference with Math-Verify, 1 or 0. The test problems are
`HuggingFaceH4/MATH-500` (OpenAI's 500 of MATH's test split) with that dataset's own prompt; a method learns on problems
of MATH's training split (`EleutherAI/hendrycks_math`), both at pinned revisions.

| Folder | What it holds |
|---|---|
| `utils/benchmark/prepare.py` | writes the task directories and the references (`.private/answers.jsonl`, which no sandbox is granted) |
| `utils/benchmark/splits/` | `test.txt`: MATH-500's 500; `train.txt` (100), `val.txt` (50), `smoke.txt` (2): MATH training problems drawn with seed 42 |
| `utils/benchmark/judge.py` | the judge, on [`../math_grading/`](../math_grading/README.md), as AIME's |
| `utils/benchmark/task-info/` | what a learner is told about the task |
| `baseline/` | `codex.yaml`, `claude.yaml`: `hermit eval --adapter none` on the 500 |

Each config comes twice, Codex with GPT-5.6-Luna and Claude Code with Claude Haiku 5.5, as in [AIME](../aime/README.md).
A run needs Linux with bubblewrap; no Docker.

## Prepare

```bash
python -m pip install -e .
python -m pip install -r examples/math_grading/requirements.txt
python examples/math500/utils/benchmark/prepare.py
```

It downloads MATH-500 and MATH's training split and writes 652 task directories. A training problem's reference is the
last `\boxed{}` of its solution, as MATH-500's answers were taken; no training problem is a MATH-500 problem (prepare
checks). Each task's `task.toml` gives the agent half an hour and no network but its model's, and asks for no memory
beyond the sandbox's default; the configs give the CLI the same half hour (`agent.<cli>.timeout_s: 1800`) and grant the
sandbox `../../math_grading/utils`, where the hook that reads the answer runs.

## Scoring

As [AIME's](../aime/README.md#scoring): the last `\boxed{}` of the final message, equal to the reference by Math-Verify
(`0.5` and `\frac{1}{2}` are the same answer), each test problem four times, as MATH-500's own evaluation runs it
(`epochs: 4`, pass@1). That evaluation (its `eval.yaml`) grades with a model (`model_graded_fact`); Math-Verify is a rule
with no model of its own. It misses some right answers written in another form than the reference: a tuple for a column
vector, a `pmatrix` with spacing between its rows, an inequality for an interval, `-\mathbf I` for the matrix it names.

## Run

```bash
hermit eval --config examples/math500/baseline/codex.yaml --adapter none
```

The results are under `examples/math500/baseline/runs/codex-baseline/eval/none/` (`summary.json`, `results.jsonl`). A
method's configs train on `splits/train.txt`, select on `splits/val.txt`, and test on `splits/test.txt`.
