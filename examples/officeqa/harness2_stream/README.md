# Harness² under the paper's protocol (OfficeQA)

[Harness²](https://arxiv.org/abs/2610.14224) as its paper runs it, by its own code
([google-research/harness2](https://github.com/google-research/harness2), the `harness2` extra) through the
[`harness2_upstream`](../../../hermit/algorithms/evolvers/harness2_upstream/) Evolver. The test questions are
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

### Continuing a stream

A finished stream can be continued on other questions, e.g. the 74 training and validation questions after the 172
test questions: the new run starts from that stream's library (copied once, before its first question) and keeps
everything else, the pinned Claude Code included, so it is the same stream with those questions at its end. Paths
are relative to this directory:

```bash
LIBRARY_FROM=runs/h2-stream-s0 TAG=h2-stream-s0-rest STREAM=stream_rest_s0.txt \
  SPLITS="../utils/splits/train.txt ../utils/splits/val.txt" CLAUDE_BIN=runs/.claude-pin/<version>/claude \
  FIREWORKS_API_KEY=... SEED=0 WM_PORT=3592 examples/officeqa_paper/harness2_stream/launch.sh
```

GEPA and SkillOpt were trained on those questions: there, compare with H0 (`runs.base1`) only. The continued
questions all come after the test questions, so each sees a larger library than in one shuffled stream of all 246.

## Reading the result

The score of question *t* is the judge's score of its deliverable, `runs.agg1.score` in
`runs/<tag>/step_NNNN/output/harness2.json` (each execution's score is also in `output/scores.jsonl` as soon as it is
judged, which survives a stopped step); a question without one counts 0. Upstream's own number, which leaves out a
question whose composed harness gave no answer, is `judged` there. The same file has the reference execution
(`runs.base1`, H0 on the same question) and the two probes. Each step's output holds upstream's records of the
question: the evidence each session saw, the candidate harnesses, the notes and the lesson.

The paper reports the mean of three streams with different seeds (`SEED`). A question costs four executions (about
0.01 USD) and four editor sessions (about 0.65 USD) and takes about 9 minutes, so a stream costs about 110-140 USD and
a day. A run stopped by its budget cannot be resumed: keep `trainer.budget.usd` well above the projection.
