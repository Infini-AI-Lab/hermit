# OfficeQA with the AHE Adapter

[Agentic Harness Engineering](https://arxiv.org/abs/2604.25850) (Lin et al. 2026;
[code](https://github.com/china-qijizhifeng/agentic-harness-engineering)) on OfficeQA in the SkillOpt paper's setting,
with Codex as the acting agent and the search confined to the adapter: Codex itself is fixed, and only the adapter's
files change. The adapter is the [`ahe-codex`](../../../hermit/adapter/templates/ahe_codex/) template: `AGENTS.md`
(the system prompt), `scripts/`, `middleware.py` (the hook pipeline, run as Codex hooks), skills, sub-agents and
`MEMORY.md`. The [`ahe_adapter`](../../../hermit/agent/evolver/methods/ahe_adapter/) Evolver keeps AHE's protocol: one
step is one iteration over the 50 training questions with k = 2 rollouts each (100 rollouts), ten iterations. Each
step classifies every question by its two rollouts, compares them with the previous iteration and scores the previous
step's change manifest (EFFECTIVE ... HARMFUL), rolls HARMFUL changes back file by file, runs the Agent Debugger (one
Codex session per question over its rollouts' trajectories, hook logs and the judge's verdicts), then runs the Evolve
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
copy under `hermit/ahe-codex/`, and its middleware as Codex hooks (`.codex/hooks.json`) that run the copy's
`hook_runner.py`. The runner gives the middleware the workspace's `task.md` as the task text (`inp.task_text`), logs
every hook call to `.hermit-hooks/hook-calls.jsonl` in the session's workspace and each session's state to
`.hermit-hooks/hook-state/`; `trials.py:collect` copies both logs to the rollout's log, beside `agent-codex.jsonl` and
`task.md`, where the learning material keeps them for the Debugger.

Under `runs/luna-ahe-adapter/`, `workspace/state/` (and each step's copy of it, `step_NNNN/state/`) holds AHE's
records: `analysis/step_NNNN/` (the Debugger's overview and one answer per question), `queries/` (the Evolve Agent's
input), `manifests/`, `evaluations/` (the verdicts on the previous step's changes), `rollbacks/`,
`iteration_scores.md` and `evolution_history.md`; `workspace/output/final_selection.json` names the iteration
`finalize` handed back.

## Result

Pending: no run on this route has finished yet.

An earlier run of the pre-port code through the Harbor interface (`agent.name: harbor`, which is not on main) scored
0.628 on the 172 test questions. That is not this example's result. The earlier run differed in these ways:

- Its acting agent was Codex through Harbor, prompted with Harbor's instruction: an instruction line, then the
  question with its oracle pages and hints. Here the prompt is `../utils/prompt.md`, which only names `task.md`, and
  `task.md` in the workspace holds the question with its pages and hints.
- The middleware's `inp.task_text` was that instruction, passed as the trial's task file (`--task-file`). Here it is
  the workspace's `task.md`, the same text without the instruction line.
- The deployed `AGENTS.md`, which Codex injects whole at every turn, had only a short note on the adapter's folder
  (1,885 bytes for H_0). Main's deployment writes a longer folder note and lists the adapter's other files
  (`hook_runner.py`, `hermit_hooks.py`, `middleware.py`, `MEMORY.md` and the README placeholders): 2,754 bytes for
  H_0.
- Its Agent Debugger and Evolve Agent ran `codex exec` themselves rather than as sessions of the codex Base Agent, in
  a run sandbox with an open network (`network: open`, no allowlist). Here they reach chatgpt.com and auth.openai.com
  through the sandbox's allowlist proxy (`network: restricted`).
- None of its sessions went through the login's plan.
