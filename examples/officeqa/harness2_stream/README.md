# Harness² under the paper's protocol (OfficeQA)

[Harness²](https://arxiv.org/abs/2610.14224) as its paper runs it, by its own code
([google-research/harness2](https://github.com/google-research/harness2), `requirements-harness2.txt`) through the
[`harness2_upstream`](../../../hermit/algorithms/evolvers/harness2_upstream/) Evolver, with Codex as the acting
agent. The 172 test questions are one stream, in upstream's seeded order, one question per step. Every question
starts from SkillOpt's initial skill as `AGENTS.md` (H0; `make_seed_codex.py` writes it as a `harness2-codex`
adapter, so Harness² starts from the text GEPA and SkillOpt start from):

1. a reference execution of H0 (the Trainer's rollout of the step);
2. an INV and a DOM editor session, each candidate executed on the same question;
3. a composer session, whose composed harness is executed on the same question: that execution is the deliverable;
4. a reflection that stores the adopted harness and its lesson in the library, the only thing the next question
   inherits.

The adapter never changes. The harness is Codex's own tree (the `harness2-codex` template: `AGENTS.md`,
`.agents/skills/`, `scripts/`, `sub_agents/`, `.codex/hooks.json`), so the editors see upstream's Codex surface and
skills on demand, scripts, subagents and hooks are all levers. Every execution is one `codex exec` session in a
Docker container of its own (`../utils/agent/codex_agent.py`, `codex_session.py`): the workspace holds `task.md` (the
question with its parsed pages and source files, as SkillOpt's Codex harness writes it) and the whole corpus under
`docs/`; the container reaches only the model provider through the egress proxy, and Codex's workspace-write sandbox
inside it gives the session's commands no network at all. A session has `agent.params.timeout_s` seconds (3,600,
the paper's cap; its 200-turn cap is not a setting Codex has, and upstream enforces none on Codex) and answers inside
`<answer>...</answer>`; one that ends without an answer scores 0.

Executor and editors are both GLM 5.3 Flash at medium effort: the executor as GEPA's and SkillOpt's, the editors as
Claude Code sessions (upstream's `claude_cli` channel, `claude -p`), each in its own sandbox that shows only its
evidence and reaches only the model (`EDITOR_MODEL` changes the editors' model). The editors see the question, its
parsed pages and source bulletins, and the trajectories; no judge result reaches them. `slots_codex.yaml` states
the answer's form as the task describes it (the value alone, in the requested unit and rounding), as upstream's
benchmarks state their deliverables.

## Run

Prepare `../utils/` first (`../README.md`), build the Codex harness's container once (`../utils/agent/docker/`:
Codex 0.162.0 for Linux, the image `officeqa-codex:0.162.0`, the internal network `oq-sandbox` and the proxy
`oq-egress`, as the [OfficeQA recipe](../../../docs/recipes/officeqa.md#run-in-the-codex-harness) describes for its
own Codex harness), then:

```bash
pip install -e . -r requirements-skillopt.txt -r requirements-harness2.txt
FIREWORKS_API_KEY=... SEED=0 WM_PORT=3591 examples/officeqa/harness2_stream/launch.sh
MAX_TASKS=2 TAG=h2-codex-smoke FIREWORKS_API_KEY=... WM_PORT=3592 examples/officeqa/harness2_stream/launch.sh   # smoke
```

`launch.sh` writes upstream's stream order for `SEED`, regenerates H0, starts a wire meter in front of the provider
for the editor sessions, and trains with the editor model bound to the meter. Each rollout's log holds the
`codex exec --json` event stream (`agent-codex.jsonl`), `task.md`, `prompt.txt`, `last_message.txt` and what the
adapter deployed (`harness.json`).

### Continuing a stream

A finished stream can be continued on other questions, e.g. the questions after the first N of the same stream, or
the 74 training and validation questions after the 172 test questions: the new run starts from that stream's library
(copied once, before its first question) and keeps everything else, so it is the same stream with those questions at
its end. A continuation names its TAG, SPLITS, STREAM and CLAUDE_BIN (the continued stream's pinned binary); the
Evolver refuses editor settings, slots, sources or a binary that differ from that stream's, and a question it took.
Paths are relative to this directory:

```bash
LIBRARY_FROM=runs/h2-codex-s0-first10 TAG=h2-codex-s0-rest STREAM=stream_rest_s0.txt SKIP_TASKS=10 \
  SPLITS=../utils/splits/test.txt CLAUDE_BIN=runs/.claude-pin/<version>/claude \
  FIREWORKS_API_KEY=... SEED=0 WM_PORT=3591 examples/officeqa/harness2_stream/launch.sh
```

GEPA and SkillOpt were trained on the training and validation questions: there, compare with H0 (`runs.base1`)
only. An interrupted run is resumed in place instead: run the same `launch.sh` command again (the Trainer's
`resume_mode: auto`).

## Reading the result

The score of question *t* is the judge's score of its deliverable, `runs.agg1.score` in
`runs/<tag>/step_NNNN/output/harness2.json` (each execution's score is also in `output/scores.jsonl` as soon as it is
judged, which survives a stopped step); a question without one counts 0. Upstream's own number, which leaves out a
question whose composed harness gave no answer, is `judged` there. The same file has the reference execution
(`runs.base1`, H0 on the same question) and the two probes. Each step's output holds upstream's records of the
question: the evidence each session saw, the candidate harnesses, the notes and the lesson.

## Result

One stream, `SEED=0`, over the 172 test questions (run `h2-codex-s0`, 2026-10-23/24: the first 10 questions, then a
continuation on the remaining 162). Executor and editors GLM 5.3 Flash at medium effort, Codex 0.162.0, one container
per session, 3,600 s per session; exact match; one pass, so each accuracy carries a standard error of about 3.7
points.

| Execution | accuracy |
|---|---|
| reference: H0, the initial skill (`runs.base1`) | 0.576 |
| INV probe (`runs.inv1`) | 0.651 |
| DOM probe (`runs.dom1`) | 0.645 |
| **deliverable: the composed harness (`runs.agg1`)** | **0.651** |

Deliverable − reference, paired per question with a 95% bootstrap interval: **+7.6** [+1.7, +13.4]. Per question,
the deliverable fixed 19 the reference got wrong and lost 6 it got right; on 12 more a probe or the reference was
right and the deliverable wrong. Majority of the three single executions: 0.663; any of the four right: 0.721
(upper bounds on what delivery could reach, not results).

The 6 losses are answer-form drift, not retrieval: the judge keeps `%` but strips commas, `million` and `$`, and the
reference answers carry `%` on some questions and not on others; the deliverable copies the form the editors chose.
The library ends with 160 adopted harnesses (148 skills, 92 scripts, no sub-agents, 27 with stored constants); the
editors cite it in about four of five sessions; the cost per question stays flat along the stream.

Spending and time: editor sessions USD 25.86, executions USD 1.62, 20.5 h in all, about 7 minutes and 0.16 USD per
question. On the same questions, under the same executor, Codex version and one pass, no adapter scores 0.541 and
the initial skill 0.576; GEPA and SkillOpt trained with GLM 5.3 Flash as the learner score 0.580 and 0.600 (means
of four trainings each): the [recipe's Flash row](../../../docs/recipes/officeqa.md#the-flash-row). Those two are
evaluated frozen after training on the 74 training and validation questions; this stream answers each test
question once, with no training split, and learns from its own executions without the judge's verdict.

The paper reports the mean of three streams with different seeds (`SEED`); this is one.
