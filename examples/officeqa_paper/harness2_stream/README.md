# Harness² under the paper's protocol (OfficeQA)

[Harness²](https://arxiv.org/abs/2610.14224) as its paper runs it, by its own code
([google-research/harness2](https://github.com/google-research/harness2), the `harness2` extra) through the
[`harness2_upstream`](../../../harnessadapter/algorithms/evolvers/harness2_upstream/) Evolver. The test questions are
one stream, in upstream's seeded order, one question per step. Every question starts from SkillOpt's initial skill
(H0, as for GEPA and SkillOpt):

1. a reference execution of H0 (the Trainer's rollout of the step);
2. an INV and a DOM editor session, each candidate executed on the same question;
3. a composer session, whose composed harness is executed on the same question: that execution is the deliverable;
4. a reflection that stores the adopted harness and its lesson in the library, the only thing the next question
   inherits.

The adapter never changes. Executor: GEPA's and SkillOpt's (GLM 5.3 Flash, medium effort, 24 turns, 16,384 tokens).
`slots.yaml` states the answer's form as the task describes it (the value alone, in the requested unit and rounding),
as upstream's benchmarks state their deliverables; without it, H0 and the compositions wrap right values in sentences
that exact match refuses (a smoke run before the change: UID0226).
Editors: Claude Code (upstream's `claude_cli` channel) on GLM 5.3 at medium effort, the model GEPA and SkillOpt
optimise with, each session in its own sandbox that shows only its evidence and reaches only the model. The editors
see the question, its parsed pages and source bulletins, and the trajectories; no judge result reaches them.

## Run

Prepare `../utils/` first (`../README.md`), then:

```bash
pip install -e '.[skillopt,harness2]'
FIREWORKS_API_KEY=... SEED=0 WM_PORT=3591 examples/officeqa_paper/harness2_stream/launch.sh
MAX_TASKS=2 TAG=h2-smoke FIREWORKS_API_KEY=... WM_PORT=3592 examples/officeqa_paper/harness2_stream/launch.sh   # smoke
```

## Reading the result

The score of question *t* is the judge's score of its deliverable, `extra.harness2.runs.agg1` in
`runs/<tag>/step_NNNN/step_result.json`; a question without one counts 0. Upstream's own number, which leaves out a
question whose composed harness gave no answer, is `extra.harness2.judged`. The same file has the reference execution
(`runs.base1`, H0 on the same question) and the two probes. Each step's output holds upstream's records of the
question: the evidence each session saw, the candidate harnesses, the notes and the lesson.

The paper reports the mean of three streams with different seeds (`SEED`). A question costs four executions (about
0.01 USD) and four editor sessions (about 0.65 USD) and takes about 9 minutes, so a stream costs about 110-140 USD and
a day. A run stopped by its budget cannot be resumed: keep `trainer.budget.usd` well above the projection.
