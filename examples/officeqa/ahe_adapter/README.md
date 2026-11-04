# OfficeQA with the AHE Adapter

[Agentic Harness Engineering](https://arxiv.org/abs/2604.25850) (Lin et al. 2026;
[code](https://github.com/china-qijizhifeng/agentic-harness-engineering)) on OfficeQA in the SkillOpt paper's setting,
with Codex as the acting agent and the search confined to the adapter: Codex itself is fixed, and only the adapter's
files change. The adapter is the [`ahe`](../../../hermit/adapter/templates/ahe/) template, the same for any host that
runs its hooks: `AGENTS.md` (the system prompt), `scripts/`, `middleware.py` (the hook pipeline, run here as Codex
hooks), skills, sub-agents and `MEMORY.md`. The [`ahe_adapter`](../../../hermit/agent/evolver/methods/ahe_adapter/) Evolver keeps AHE's protocol: one
step is one iteration over the 50 training questions with k = 2 rollouts each (100 rollouts), ten iterations. Each
step classifies every question by its two rollouts, compares them with the previous iteration and scores the previous
step's change manifest (EFFECTIVE ... HARMFUL), rolls HARMFUL changes back file by file, runs the Agent Debugger (one
Codex session per question over its rollouts' trajectories, hook audits and the judge's verdicts), then runs the Evolve
Agent, a Codex session that edits the harness and writes the next manifest. `finalize` hands back the best-ever
iteration by training pass rate, which `hermit eval` scores once on the 172 test questions. The harness starts from
the seed system prompt with SkillOpt's initial skill appended (`make_seed.py`), so H_0 holds the text the other
methods start from.

Setup: sign in once with `codex login`, and build the image and the egress proxy of the recipe's
[A model on a ChatGPT login](../../../docs/recipes/officeqa.md#a-model-on-a-chatgpt-login) (`officeqa-codex:0.164.0`
with the machine's standalone Codex, and `oq-egress-login` on the `oq-sandbox` network, letting through chatgpt.com
and auth.openai.com). Then, from the repository root:

```bash
python examples/officeqa/utils/prepare.py --csv ... --docs ...                     # once (../README.md)
python examples/officeqa/ahe_adapter/make_rows.py                                  # rows/: k = 2 trial rows
python examples/officeqa/ahe_adapter/make_seed.py                                  # seed_adapter/: H_0
hermit check --config examples/officeqa/ahe_adapter/smoke_codex_luna.yaml
hermit train --config examples/officeqa/ahe_adapter/smoke_codex_luna.yaml         # 4 questions x 2, two steps
hermit eval  --config examples/officeqa/ahe_adapter/smoke_codex_luna.yaml --adapter final   # the 4 smoke test questions
hermit train --config examples/officeqa/ahe_adapter/run_codex_luna.yaml           # 50 questions x 2, ten iterations
hermit eval  --config examples/officeqa/ahe_adapter/run_codex_luna.yaml --adapter final
hermit eval  --config examples/officeqa/ahe_adapter/run_codex_luna.yaml --adapter seed_adapter   # H_0
```

`run_codex_luna.yaml` has the acting agent, judge, models, Codex settings and image of `../gepa/run_codex_luna.yaml`:
the framework's Codex agent with OfficeQA's task functions, GPT-5.6-Luna at medium effort on the machine's ChatGPT
login, one container per session, 3,600 s each, no web search. The Agent Debugger and the Evolve Agent are
Codex sessions on the same login and model, in the run's sandbox, reaching chatgpt.com and auth.openai.com through
its allowlist proxy. Every session on the login shares its plan (`~/.cache/hermit/codex-login/plan/`).

A training question's k rollouts are k rows of `rows/tasks.jsonl`: `<UID>#1` and `<UID>#2`, the question's own row
under another id, with `uid` (by which the Evolver groups them) and `rollout`. `trials.py` hands `../utils` the
question's own row, so a rollout's `task.md`, corpus and score are the question's. The harness is deployed as
`hermit deploy` deploys it: the system prompt as `AGENTS.md`, skills and sub-agents in Codex's places, the adapter's
copy under `hermit/ahe/`, and its middleware as Codex hooks (`.codex/hooks.json`) that run the copy's
`hook_runner.py`. The runner gives the middleware the workspace's `task.md` as the task text (`inp.task_text`), logs
every hook call to `.hermit-hooks/hook-calls.jsonl` in the session's workspace and each session's state to
`.hermit-hooks/hook-state/`; `trials.py:collect` copies both to the rollout's log, beside `agent-codex.jsonl` and
`task.md`.

The learners read Codex's own record of a rollout, where upstream's read NexAU's traces. A rollout's log in the
learning material (`trajectories/<id>/`) holds the session's `codex exec --json` event stream (`agent-codex.jsonl`),
`prompt.txt`, `task.md`, `last_message.txt` and the two hook logs (`hook-calls.jsonl`, `hook-state/`). For each
question the Agent Debugger gets a directory with, per rollout i:

- `trace0i.events.jsonl`: the event stream, and `trace0i/NNNN-<kind>.md`: the same rollout one file per step
  (message, reasoning, command with its exit code and output, file change, tool call, error), the steps
  [`hermit.agent.base.trajectory`](../../../hermit/agent/base/trajectory.py) reads from the stream;
- `prompt0i.txt` and `task0i.md`: the prompt and the task file of the session's workspace;
- `hooks0i.jsonl`: the hook audit, one line per hook call;
- `verifier_0i.txt`, for a rollout that did not pass: the judge's feedback (`predicted '4957.21' but expected
  '4962.46'`), which the acting agent never saw.

The hook state is not copied there, since upstream's Debugger read the traces and the verifier output alone; the
Evolve Agent reads the Debugger's reports and opens the rollouts' logs, `hook-state/` included, in the learning
material. Upstream's traces carry each message's latency, from which its question gets a timing breakdown for a
rollout that timed out. Codex's stream has no timestamps, so a timed-out rollout gets none here; no training rollout
of this run timed out.

Under `runs/luna-ahe-adapter/`, `workspace/state/` (and each step's copy of it, `step_NNNN/state/`) holds AHE's
records: `analysis/step_NNNN/` (the Debugger's overview and one answer per question), `queries/` (the Evolve Agent's
input), `manifests/`, `evaluations/` (the verdicts on the previous step's changes), `rollbacks/`,
`iteration_scores.md` and `evolution_history.md`; `workspace/output/final_selection.json` names the iteration
`finalize` handed back.

## Result

One training (`luna-ahe-adapter`, `run_codex_luna.yaml`) and one evaluation pass of each adapter over the 172 test
questions, exact match, in [the GPT-5.6-Luna setting](../README.md#results): GPT-5.6-Luna at medium effort acting,
debugging and evolving on the machine's ChatGPT login, Codex 0.164.0 in a container per session.

| Adapter | accuracy [95% bootstrap] | − H_0, points |
|---|---|---|
| H_0: the seed harness with SkillOpt's initial skill (`seed_adapter`) | 0.366 [0.297, 0.442] | |
| `final`: the best-ever iteration, 7 (training pass@1 0.69) | **0.616** [0.541, 0.686] | **+25.0** [+16.9, +33.1] |

Paired per question, 95% bootstrap (10,000 resamples), GEPA's and SkillOpt's four trainings averaged per question
first; bold where the interval lies above zero. `final` wins 52 questions and loses 9 against H_0.

- Against the column's other cells: GEPA (0.600) +1.6 [−3.2, +6.4], Harness² (0.610) +0.6 [−5.2, +6.4], SkillOpt
  (0.547) **+7.0** [+1.7, +12.4], ReHarness (0.517) **+9.9** [+3.5, +16.3], Continual Harness (0.477) **+14.0**
  [+7.0, +21.5]. H_0 against the initial skill alone (0.360): +0.6 [−5.2, +6.4], so the seed harness adds nothing by
  itself and the gain is the ten iterations'. The column's GEPA and SkillOpt trainings used SkillOpt's own Codex
  harness with 180 s sessions; this run's sessions are the framework's Codex agent, up to 3,600 s each.
- Most of the gain is the answer's form. Answers longer than 40 characters fall from 30% (H_0) to 0.6%; the share of
  answers that contain the right value anywhere, an automatic upper bound on the right value, rises from 0.558 to
  0.616, about a quarter of the gain. Every miss of `final` is a wrong value.
- Training pass@1 over the 100 trial rows, iterations 1–10 (iteration n scores the harness iteration n − 1 left):
  0.42, 0.61, 0.65, 0.66, 0.64, 0.64, 0.69, 0.67, 0.63, 0.65. The best-ever iteration is 7, the harness the sixth
  Evolve session left (commit `78fe7cb`): H_0 with 34 lines added to its system prompt (a strict-answer protocol, an
  evidence gate, a schema and unit audit before submitting, a source-adjudication checkpoint, a two-pass
  reconciliation gate) and 8 lines to `MEMORY.md`. The one middleware addition (iteration 4, a session-start
  reminder) was judged MIXED and removed by the next Evolve session; no change was judged HARMFUL, so no automatic
  rollback ran.
- Spending, all on the ChatGPT plan: training 1.5 h, 190 M tokens of rollouts and 122 M of Debugger and Evolve
  sessions; the evaluations 9 min (`final`) and 21 min (H_0); 181 K tokens per test question for `final`, 166 K for
  H_0. No evaluation session errored.
