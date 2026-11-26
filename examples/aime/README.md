# AIME

AIME 2025 and AIME 2026 in Hermit's own environment: each problem is a task directory, the acting agent (Codex or Claude
Code) solves it in a bubblewrap sandbox of its own with no network but its model's, and a judge on the host scores the
last `\boxed{}` of its final message against the reference, 1 or 0. The problems are MathArena's
(`huggingface.co/datasets/MathArena`, pinned revisions), with MathArena's AIME prompt.

| Folder | What it holds |
|---|---|
| `utils/benchmark/prepare.py` | writes the task directories and the references (`.private/answers.jsonl`, which no sandbox is granted) |
| `utils/benchmark/splits/` | `test_2025.txt`, `test_2026.txt`: 30 problems each; `train.txt` and `val.txt`: AIME 2024's 30 problems, 20 and 10 (seed 42), for a method that learns; `smoke.txt`: two AIME 2023 problems that no run takes |
| `utils/benchmark/judge.py` | the judge, on [`../math_grading/`](../math_grading/README.md): the last `\boxed{}` (MathArena's rule) and Math-Verify's equivalence |
| `utils/benchmark/task-info/` | what a learner is told about the task |
| `baseline/` | `codex.yaml`, `claude.yaml`: `hermit eval --adapter none` on AIME 2025 (AIME 2026 with an override, below) |

Each config comes twice: Codex with GPT-5.6-Luna on a ChatGPT login, and Claude Code with Claude Haiku 5.5 on a Claude
subscription, both at medium effort (the sign-ins: [Harbor benchmarks](../README.md#harbor-benchmarks)). A run needs
Linux with bubblewrap; no Docker.

## Prepare

```bash
python -m pip install -e .
python -m pip install -r examples/math_grading/requirements.txt
python examples/aime/utils/benchmark/prepare.py
```

It downloads MathArena's AIME 2023 I, 2024 I and II, 2025 and 2026 sets (under 100 KB) and writes 92 task directories:
the 90 problems of 2024 to 2026 and the two smoke problems. Each task's `instruction.md` is MathArena's AIME instruction
followed by the problem, as MathArena's runner builds the prompt; its `task.toml` gives the agent an hour and no network
but its model's, and asks for no memory beyond the sandbox's default. The configs give the CLI the same hour
(`agent.<cli>.timeout_s: 3600`), so a session ends on its own clock rather than at the sandbox's bound, and grant the
sandbox `../../math_grading/utils`, where the hook that reads the answer from the agent's reply runs.

## Scoring

The answer is the content of the last complete `\boxed{}` in the agent's final message; a reply without one scores 0. It
is right when Math-Verify finds it equal to the reference (`070` and `70` are the same answer), read by its LaTeX
extractor only, each comparison in a process of its own with a time limit, on the host. Each test problem is run four
times, as MathArena runs a problem (`--n 4`); the accuracy is the summary's `mean_score`, the mean of the attempts. One
difference from MathArena's runner (`src/matharena/runner.py` at commit `d98156f`): when a final message has no
`\boxed{}`, it asks the model once more for the answer; a CLI session here ends with its final message, which then
scores 0. A learner reads a training problem's feedback, which names the reference; never a validation or test
problem's.

## Run

```bash
hermit eval --config examples/aime/baseline/codex.yaml --adapter none
hermit eval --config examples/aime/baseline/codex.yaml --adapter none \
    data.test=../utils/benchmark/splits/test_2026.txt trainer.run_name=codex-baseline-2026
```

The results are under `examples/aime/baseline/runs/<run name>/eval/none/` (`summary.json`, `results.jsonl`). A method's
configs train on `splits/train.txt`, select on `splits/val.txt`, and test on the baseline's splits.
