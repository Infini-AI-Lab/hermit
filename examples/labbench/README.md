# LAB-Bench FigQA

LAB-Bench's FigQA through Harbor: 181 multiple-choice questions about figures of biology papers, each in its own
container with the figure as an image file, scored 1 for the right letter in `/app/answer.txt`, else 0. The task set
is the Harbor registry's `labbench@1.0`, patched (below) and locked in `utils/dataset.lock`. The method is SkillOpt in
the SkillOpt paper's protocol; the baseline is the same acting agent with no adapter, on the same test questions.

| Folder | What it runs |
|---|---|
| `utils/benchmark.yaml` | the task set (SkillOpt's split, the patch) and the settings every config shares: each question's own 300 s, no web search |
| `utils/patches.py` | the patch: the shuffle's seed is not saved where the agent can read it, and the verifier no longer runs `/app/setup.py`, a script the agent can rewrite |
| `utils/rows/` | `test_new_figures.txt`: the 102 test questions whose figure no training question has (`utils/figures.py` writes it) |
| `baseline/` | `codex.yaml`, `claude.yaml`: `hermit eval --adapter none` on the 127 test questions (all 181 with `data.test=../utils/splits/all.txt` and a run name of its own: [Baseline](#baseline)) |
| `skillopt/` | `run_codex.yaml`, `run_claude.yaml`: 4 SkillOpt epochs (`hermit train`), then the final skill's test (`hermit eval --adapter final`); `smoke_*.yaml` first |

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
the files that name the right choice. As published, that mapping also holds the shuffle's seed, and the right choice is
the first before the shuffle, so the seed gives the answer away to an agent that replays the shuffle: in all 181 tasks.
And the verifier (`tests/test.sh`, uploaded with the right choice once the agent has stopped) runs `/app/setup.py` again
when the mapping is missing, a file the agent can rewrite: a script left there would run beside the right choice and
could write a mapping that matches any letter. `utils/patches.py` drops the seed from what setup.py saves (the verifier
reads only the letters) and removes that fallback (the mapping is always written before the agent starts, so a missing
one scores 0); prepare runs it on every download and records it in the lock. Harbor's oracle agent still scores 1 on the
patched tasks. What the patch leaves open: the verifier runs in the agent's container, with the container's `python3`
and the mapping in `/app`, which the agent, root in `python:3.11-slim`, can change; nothing here checks for that, and
SkillOpt's learners read the verifier's output.

**Shared figures.** FigQA asks up to three questions about one figure (the 181 questions show 107 figures), and
SkillOpt's split, like its paper's, is by question: 25 of the 127 test questions share a figure with a training
question, whose verifier output (the right choice's text) the learner reads, so a skill can carry a fact about a
figure into the test. `utils/rows/test_new_figures.txt` lists the other 102 (15 of them share their figure with a
validation question, and stay new: SkillOpt reads a validation trial's score alone, for its gate, never its output);
report every arm on them as well, from the same trials, which `utils/figures.py` reads from each eval's
`results.jsonl` (the figures `python -m hermit.harbor.report` gives, on those rows: the accuracy is `pass_at_1`):

```bash
python examples/labbench/utils/figures.py examples/labbench/baseline/runs/codex-baseline/eval/none examples/labbench/skillopt/runs/codex-skillopt/eval/root examples/labbench/skillopt/runs/codex-skillopt/eval/final
```

(Not `hermit eval` on those rows, `data.test=../utils/rows/test_new_figures.txt`: its results go into the run's
`eval/<adapter>/`, `eval/final/` for the final skill, where they would replace the 127 questions' results the table
reads.)

## Baseline

```bash
hermit eval --config examples/labbench/baseline/codex.yaml --adapter none
hermit eval --config examples/labbench/baseline/codex.yaml --adapter none \
    data.test=../utils/splits/all.txt trainer.run_name=codex-baseline-all
```

127 trials (the test questions), or 181 (the whole benchmark): `data.test` is relative to the config's folder, and the
run name keeps the two evals apart. The accuracy is the `pass_at_1` of `python -m hermit.harbor.report` on the eval's
folder (`examples/labbench/baseline/runs/codex-baseline/eval/none/`), over the trials that are the agent's result; the
summary's `mean_score` counts an infra trial as 0.

## SkillOpt

Why this method: none of this framework's methods reports LAB-Bench. SkillOpt's paper (Yang et al., 2026,
[arXiv:2605.23904v2](https://arxiv.org/abs/2605.23904v2)) has the nearest tasks, with a coding CLI as the acting agent:
multiple choice (LiveMathematicianBench, where GPT-5.5 in the Codex harness goes from 35.2 with no skill to 78.4) and
questions about document images (DocVQA, +5.0 in the Codex harness, 78.8 to 91.2 in direct chat). A skill can carry how
to read a figure (its panels, axes, legends and error bars, cropping or enlarging a region, reading every choice against
the figure) from one question to the next. GEPA (its paper's splits on a few hundred questions: 150 / 300 / 300 for
HotpotQA and HoVer, thirds for LiveBench-Math) and ACE (an itemised playbook over the training questions) are the other
candidates.

| | The SkillOpt paper | Here |
|---|---|---|
| Split | train / selection / test 2:1:7, seed 42 | the same: 36 / 18 / 127 questions |
| Epochs, batch | 4 epochs, rollout batch 40, reflection minibatch 8 | the same (one step an epoch: 36 questions) |
| Edits | learning rate 4, cosine to 2 | the same |
| Gate | strict: a candidate kept only above the current selection score (ties rejected) | the same, the score the share of right letters |
| Epoch end | from the second epoch: the slow update on 20 questions, gated, and the meta skill (the learner's own guidance, never deployed) | the same |
| Final | the best skill | the same |
| Models | GPT-5.5 (medium) as student and teacher | one model for both: GPT-5.6-Luna or Claude Haiku 5.5, medium |
| Task text | the task's question (in SkillOpt's code, a rollout's `task_description`) | the row's text: the task's settings and the published placeholder (`Task instructions are in /app/instruction.md`); the question reaches the learners where the agent read that file in its trajectory, a tool result of which they get the first 800 characters (the instruction files are 504 to 949 characters, 10 of the 181 longer than 800) |
| Trace summary | SkillOpt's Codex harness gives the analysts a summary of the Codex session | none (`codex_trace_to_optimizer: false`): the summary reads SkillOpt's own harness (`task.md`, `SKILL.md`, `<answer>`), where this task's answer is `[ANSWER]X[/ANSWER]` in `/app/answer.txt` |
| Starting skill | the benchmark's own | none: the prompt-only template's `ADAPTER.md`, a placeholder with no instructions |

```bash
hermit train --config examples/labbench/skillopt/smoke_codex.yaml   # 2 questions, 2 epochs
hermit eval  --config examples/labbench/skillopt/smoke_codex.yaml --adapter final
hermit train --config examples/labbench/skillopt/run_codex.yaml     # again to resume
hermit eval  --config examples/labbench/skillopt/run_codex.yaml --adapter final
hermit eval  --config examples/labbench/skillopt/run_codex.yaml --adapter root
```

`--adapter final` tests the final skill on the 127 test questions (`runs/codex-skillopt/eval/final/`) and
`--adapter root` the empty skill, both after `hermit train` (adapters of its tree); report them beside the baseline, on
the 102 new-figure questions as well. The run is at most 408 training and validation trials of a few minutes each (18
for the starting skill's validation; each epoch 36 training rollouts and 18 for its gate; from the second epoch, 40 for
the slow update's comparison and 18 for its gate), fewer where a skill was validated before (SkillOpt keeps its scores).
A resumed run runs the step it stopped in again, its rollouts and its validations, so a run stopped mid-step takes more.
A learner's call waits for the plan's window however long it holds (`llm.plan_max_wait_s: null`);
`trainer.call_timeout_min` (a week) still bounds a step.

SkillOpt's validations (the starting skill's, each gate's and the slow update's comparison) score a trial that is not
the agent's result (infra) 0, as a failed one: one in the starting skill's validation lowers the bar for the whole run
(SkillOpt keeps that score), one in a candidate's can get it rejected. `hermit eval` counts such a trial as 0 in
`mean_score` too, and `python -m hermit.harbor.report` (and `utils/figures.py`) leaves it out of its `pass_at_1` and
counts it (`infra`): report that accuracy, and give that count beside each number.

## Results

Not run yet. Accuracy on the 127 test questions, and on the 102 of them whose figure no training question has.

| Acting agent and learner (each cell: 127 / 102 questions) | No adapter | Empty skill (`root`) | SkillOpt's final skill |
|---|---|---|---|
| Codex, GPT-5.6-Luna | | | |
| Claude Code, Claude Haiku 5.5 | | | |
