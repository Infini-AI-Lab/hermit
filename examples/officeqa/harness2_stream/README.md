# Harness² under the paper's protocol (OfficeQA)

[Harness²](https://arxiv.org/abs/2610.14224) as its paper runs it, by its own code
([google-research/harness2](https://github.com/google-research/harness2), the `harness2` extra) through the
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
pip install -e '.[skillopt,harness2]'
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

The paper reports the mean of three streams with different seeds (`SEED`). With GLM 5.3 Flash a question costs
four executions (about 0.01 USD) and four editor sessions (about 0.1 USD) and takes about 7 minutes, so a stream
costs about 20 USD and a day.
