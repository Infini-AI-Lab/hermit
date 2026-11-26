# LiveMathBench

LiveMathBench in Hermit's own environment: each question is a task directory, the acting agent (Codex or Claude Code)
solves it in a bubblewrap sandbox of its own with no network but its model's, and a judge on the host scores it as
OpenCompass's LiveMathBench evaluator does: the last `\boxed{}` of the agent's final message is its answer, and a judge
model reads the question, the reference and the answer in OpenCompass's judge prompt and says yes or no. The questions
are the official dataset's (`opencompass/LiveMathBench`, gated, at a pinned revision) with OpenCompass's English
prompt: the 202505 release's English questions are tested, and a method learns on the 202412 release's.

| Folder | What it holds |
|---|---|
| `utils/benchmark/prepare.py` | writes the task directories and the references with their questions (`.private/answers.jsonl`, which no sandbox is granted), and the splits |
| `utils/benchmark/splits/` | `test.txt` (100): the 202505 English questions; `train.txt` (40), `val.txt` (20), `smoke.txt` (2): 202412 English questions drawn with seed 42 |
| `utils/benchmark/judge.py` | the judge: OpenCompass's judge prompt (commit `4f3812d`, compared byte for byte) and verdict, on the model `LIVEMATHBENCH_JUDGE` names |
| `utils/gpassk.py` | G-Pass@k and mG-Pass@k, LiveMathBench's own metrics, from an eval's attempts |
| `utils/benchmark/task-info/` | what a learner is told about the task |
| `baseline/` | `codex.yaml`, `claude.yaml`: `hermit eval --adapter none` on the 202505 English questions |

Each config comes twice, Codex with GPT-5.6-Luna and Claude Code with Claude Haiku 5.5, as in [AIME](../aime/README.md).
A run needs Linux with bubblewrap (no Docker), and the judge model.

## Prepare

The dataset is gated: accept its terms at `huggingface.co/datasets/opencompass/LiveMathBench` with your account, and
sign in on the machine (`hf auth login`, or `HF_TOKEN`). Then:

```bash
python -m pip install -e .
python -m pip install -r examples/math_grading/requirements.txt
python examples/livemathbench/utils/benchmark/prepare.py
```

It writes 162 task directories: the 202505 release's 100 English questions (`all_en`) and 62 questions drawn with seed
42 from the 202412 release's English sets (AMC, CCEE, CNMO, WLPMC; none is a 202505 question). The 202412 questions are
problems to solve or blanks to fill (none has options); the 202505 ones give a subject instead (`type`), which the
task's `[metadata]` keeps. Each task's `task.toml` gives the agent an hour and no network but its model's, and asks for
no memory beyond the sandbox's default; the configs give the CLI the same hour (`agent.<cli>.timeout_s: 3600`) and grant
the sandbox `../../math_grading`, where the hook that reads the answer runs.

## Scoring

OpenCompass's evaluator calls a judge model at temperature 0 with up to 8,192 output tokens; the paper's is
Qwen2.5-72B-Instruct, and OpenCompass also offers the smaller LiveMath-Judge (`jnanliu/LiveMath-Judge`). The judge here
makes the same call on the model `LIVEMATHBENCH_JUDGE` names, a `<backend>/<model>` reference
([Model backends](../../docs/guides/model-backends.md)), by default `vllm/Qwen/Qwen2.5-72B-Instruct` on a local vLLM
server:

```bash
vllm serve Qwen/Qwen2.5-72B-Instruct      # on GPUs that hold it, port 8000 of the run's machine
export LIVEMATHBENCH_JUDGE=vllm/Qwen/Qwen2.5-72B-Instruct
```

A judge call that fails is a failed judgement (`hermit eval` counts it among its `errors`), never a 0 the agent earned.
Each verdict and its tokens are in the judgement's record (`judge_verdict`, `judge_tokens`). Each test question is run
four times; the summary's `mean_score` is the accuracy over the attempts, and `utils/gpassk.py` gives G-Pass@4 and
mG-Pass@4 from the same attempts (the paper draws 48 a question for k up to 16: `eval.repeats=48`, then
`gpassk.py --k 16`). A learner reads a training question's feedback, which names the reference; never a validation or
test question's.

## Run

```bash
hermit eval --config examples/livemathbench/baseline/codex.yaml --adapter none
python examples/livemathbench/utils/gpassk.py examples/livemathbench/baseline/runs/codex-baseline/eval/none
```

A method's configs train on `splits/train.txt`, select on `splits/val.txt`, and test on `splits/test.txt`.
