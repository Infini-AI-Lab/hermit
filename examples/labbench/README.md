# LAB-Bench FigQA

LAB-Bench's FigQA through Harbor: 181 multiple-choice questions about figures of biology papers, each in its own
container with the figure as an image file, scored 1 for the right letter in `/app/answer.txt`, else 0. The task set
is the Harbor registry's `labbench@1.0`, patched (below) and locked in `utils/dataset.lock`. The method is SkillOpt in
the SkillOpt paper's protocol; the baseline is the same acting agent with no adapter, on the same test questions.

| Folder | What it runs |
|---|---|
| `utils/benchmark.yaml` | the task set (SkillOpt's split, the patch) and the settings every config shares: each question's own 300 s, no web search |
| `utils/patches.py` | the patch: the shuffle's seed is not saved where the agent can read it |
| `baseline/` | `codex.yaml`, `claude.yaml`: `hermit eval --adapter none` on the 127 test questions (all 181 with `--split`) |
| `skillopt/` | `run_codex.yaml`, `run_claude.yaml`: 4 SkillOpt epochs, then the final skill's test; `smoke_*.yaml` first |

Each config comes twice: Codex with GPT-5.6-Luna on a ChatGPT login, and Claude Code with Claude Haiku 5.5 on a Claude
subscription, both at medium effort, both acting and learning. Both CLIs open the figure themselves (Claude Code with
its Read tool). [Run the Harbor benchmarks](../README.md#harbor-benchmarks) says what a machine needs and how to
switch to another model or CLI.

## Prepare

```bash
python -m hermit.harbor.dataset prepare examples/labbench/utils/benchmark.yaml
```

It downloads the 181 tasks (244 MB with the figures), patches them, checks them against the lock, and writes
`utils/tasks.jsonl` (one row per question, and the smoke rows `figqa-0001#0`, `figqa-0002#0`) and `utils/splits/`:
SkillOpt's 2:1:7 with its seed 42, 36 train, 18 validation and 127 test questions.

**The patch.** Each container shuffles the choices when it starts (`environment/workspace/setup.py`), writes the
question with its letters to `/app/instruction.md` and the letters' texts to `/app/.letter_mapping.json`, and deletes
the files that name the right choice. As published, that mapping also holds the shuffle's seed, and the right choice
is the first before the shuffle, so the seed gives the answer away to an agent that replays the shuffle: in all 181
tasks. `utils/patches.py` drops the seed from what setup.py saves (the verifier reads only the letters); prepare runs
it on every download and records it in the lock. Harbor's oracle agent still scores 1 on the patched tasks.

## Baseline

```bash
hermit eval --config examples/labbench/baseline/codex.yaml --adapter none
hermit eval --config examples/labbench/baseline/codex.yaml --adapter none --split examples/labbench/utils/splits/all.txt
```

127 trials (the test questions), or 181 (the whole benchmark). The accuracy is the summary's `mean_score`.

## SkillOpt

Why this method: none of this framework's methods reports LAB-Bench. SkillOpt's paper (Microsoft, 2026,
[arXiv:2605.23904](https://arxiv.org/abs/2605.23904)) has the nearest tasks, with a coding CLI as the acting agent:
multiple choice (LiveMathematicianBench, where GPT-5.5 in the Codex harness goes from 35.2 with no skill to 78.4) and
questions about document images (DocVQA, +5.0 in the Codex harness, 78.8 to 91.2 in direct chat). A skill can carry
how to read a figure (its panels, axes, legends and error bars, cropping or enlarging a region, reading every choice
against the figure) from one question to the next. GEPA (its paper's protocol on a few hundred questions: train,
validation and test in thirds) and ACE (an itemised playbook over the training questions) are the other candidates.

| | The SkillOpt paper | Here |
|---|---|---|
| Split | train / selection / test 2:1:7, seed 42 | the same: 36 / 18 / 127 questions |
| Epochs, batch | 4 epochs, rollout batch 40, reflection minibatch 8 | the same (one step an epoch: 36 questions) |
| Edits | learning rate 4, cosine to 2 | the same |
| Gate | strict, hard success on the selection split | the same: the right letter |
| Epoch end | slow update on 20 questions and meta skill, gated | the same |
| Final | the best skill | the same |
| Models | GPT-5.5 (medium) as student and teacher | one model for both: GPT-5.6-Luna or Claude Haiku 5.5, medium |
| Starting skill | the benchmark's own | none: the prompt-only template's empty `ADAPTER.md` |

```bash
hermit train --config examples/labbench/skillopt/smoke_codex.yaml   # 2 questions, 2 epochs
hermit train --config examples/labbench/skillopt/run_codex.yaml     # again to resume
hermit eval  --config examples/labbench/skillopt/run_codex.yaml --adapter root
```

`hermit train` ends by testing the final skill on the 127 test questions (`runs/codex-skillopt/eval/final/`); report
it beside the baseline and the empty skill (`root`). The run is about 450 training and validation trials of a few
minutes each.

## Results

Not run yet. Accuracy on the 127 test questions.

| Acting agent and learner | No adapter | Empty skill (`root`) | SkillOpt's final skill |
|---|---|---|---|
| Codex, GPT-5.6-Luna | | | |
| Claude Code, Claude Haiku 5.5 | | | |
