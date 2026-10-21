# OfficeQA

[OfficeQA](https://github.com/databricks/officeqa) ([dataset](https://huggingface.co/datasets/databricks/officeqa), [paper](https://arxiv.org/abs/2603.08655)): questions about U.S. Treasury Bulletins (1939–2025) whose
answers are figures found in the bulletins, often after a computation. This example runs OfficeQA Full (246
questions) as the [SkillOpt paper](https://arxiv.org/abs/2605.23904) does: its 50 / 24 / 172 train / validation /
test split, each question's parsed source pages in the prompt, and exact match, with the acting agent in either of
the paper's harnesses: direct chat, with read and grep tools limited to the question's source files, or Codex, a
`codex exec` session with the whole corpus. The adapter's `ADAPTER.md` is the agent's skill. Three methods train it,
one folder each: prompt-only, GEPA and SkillOpt; GEPA and SkillOpt are compared with no skill and with SkillOpt's
initial skill.

## Setup

Install the framework first ([Installation](../../docs/get-started/installation.md)), with the `skillopt` extra (the
agent is SkillOpt's own rollout and the judge its scorer) and the `gepa` extra. Also needed: access to the gated
Hugging Face dataset `databricks/officeqa` (`officeqa_full.csv`, and `treasury_bulletins_parsed/` with
`transformed/` and `jsons/`), and a Fireworks key in `FIREWORKS_API_KEY`, or another backend
([Model backends](../../docs/guides/model-backends.md)). The Codex harness also needs Docker: each Codex session runs in
a container that sees only its workspace and the corpus and reaches only the model provider (`utils/agent/docker/`;
set up as in the [recipe](../../docs/recipes/officeqa.md#run-in-the-codex-harness)). Prompt-only's learner is a Codex
session in the framework's sandbox, which needs Linux, bubblewrap and the Codex CLI
([agent learners](../../docs/methods/agent.md)).

```bash
pip install -e '.[skillopt,gepa]'
python examples/officeqa/utils/prepare.py --csv /path/to/officeqa_full.csv --docs /path/to/treasury_bulletins_parsed
```

`prepare.py` writes the questions, the paper's splits, `data.json` and `seed_adapter/` (SkillOpt's initial OfficeQA
skill as a prompt-only adapter, where every config starts) into `utils/`, and the answers into `utils/.private/`.
It fetches the split from SkillOpt's repository at a pinned commit; `--split-dir` reads a local copy instead.

## Run

```bash
harnessadapter train --config examples/officeqa/skillopt/run.yaml        # SkillOpt
harnessadapter train --config examples/officeqa/gepa/run.yaml            # GEPA
harnessadapter train --config examples/officeqa/prompt_only/run.yaml     # prompt-only
harnessadapter train --config examples/officeqa/skillopt/run_codex.yaml  # SkillOpt in the Codex harness, as the others
harnessadapter eval  --config examples/officeqa/skillopt/run.yaml --adapter final   # SkillOpt's trained skill
harnessadapter eval  --config examples/officeqa/gepa/run.yaml     --adapter final   # GEPA's trained skill
harnessadapter eval  --config examples/officeqa/skillopt/run.yaml --adapter none    # no skill
harnessadapter eval  --config examples/officeqa/skillopt/run.yaml --adapter ../utils/seed_adapter   # initial skill
```

An adapter's accuracy is `mean_score` in `<method>/runs/<run name>/eval/<adapter>/summary.json` (172 test
questions, three passes each; prompt-only's runs are under `~/.cache/harnessadapter/officeqa-prompt-only/` instead);
`--adapter adapter` evaluates the adapter included in `gepa/` or `skillopt/`.
Before a `run.yaml`, run its folder's `smoke.yaml` (a few questions) and `harnessadapter check --config ...` (add
`--no-probe` to skip its one model call). `run_codex.yaml` and `smoke_codex.yaml` are the folder's `run.yaml` and
`smoke.yaml` with the agent in the Codex harness (`agent.params.harness: codex`) and no budget; evaluate in that
harness with them, e.g. `--config examples/officeqa/skillopt/run_codex.yaml --adapter none`. Codex's workspaces are
under `~/.cache/harnessadapter/officeqa-codex-actor/`, outside the checkout.

## Methods

All three methods train the same adapter, one `ADAPTER.md` (the prompt-only template), from SkillOpt's initial
skill, with GLM 5.3 (reasoning effort medium) as the learner.
**prompt-only** ([`prompt_only/`](prompt_only/), [method](../../docs/methods/agent.md)): the built-in Codex Evolver on
the prompt-only template, as in [`clbench_poker/prompt_only/`](../clbench_poker/prompt_only/): after every 10
training questions a fresh Codex session reads the trajectories and the judge's feedback and rewrites the skill;
there is no validation gate, and the final skill is the last one. The session runs in the framework's sandbox, which
hides the framework's `examples/` and the answers with them, so these runs are written outside the checkout.
**GEPA** ([`gepa/`](gepa/), [method](../../docs/methods/gepa.md)): GLM 5.3 reflects on three trajectories at a time
to propose a new skill, and the candidates are kept on a Pareto front over the 24 validation questions.
**SkillOpt** ([`skillopt/`](skillopt/), [method](../../docs/methods/skillopt.md)), with the paper's
hyperparameters: GLM 5.3 analysts propose edits from batches of 40 questions, a learning rate bounds how many are
applied, and the validation questions gate every update.

## Results

Agent: GLM 5.3 Flash (reasoning effort medium) in SkillOpt's rollout, in direct chat (`run.yaml`) or in SkillOpt's
Codex harness (`run_codex.yaml`; Codex CLI 0.134 for Linux, one container per session). Learner: GLM 5.3 (medium).
172 test questions, three passes each.

| Method | direct chat | Codex harness |
|---|---|---|
| no skill: the agent without an adapter | 0.415 | 0.543 |
| SkillOpt's initial skill (`utils/seed_adapter/`) | 0.465 | 0.564 |
| **GEPA** (`gepa/`) | 0.564 | 0.595 |
| **SkillOpt** (`skillopt/`) | 0.576 | 0.628 |

Paired per question with 95% bootstrap intervals, in direct chat both trained skills beat no skill (GEPA +14.9
[+9.5, +20.5] points, SkillOpt +16.1 [+10.3, +22.1]) and the initial skill (+9.9, +11.0); SkillOpt − GEPA is +1.2
[−3.1, +5.4], so the two methods are not separated.

In the Codex harness the agent starts higher (it runs shell commands and its own scripts, without direct chat's turn
and reply limits) and the initial skill does not help (+2.1 [−1.7, +6.0]). SkillOpt beats no skill (+8.5 [+3.1,
+14.0]) and the initial skill (+6.4 [+1.4, +11.4]); GEPA is borderline against no skill (+5.2 [−0.2, +10.5]) and not
separated from the initial skill (+3.1 [−1.6, +7.6]). SkillOpt − GEPA is +3.3 [−0.8, +7.4], so the two methods are
not separated. The paper's Codex harness (GPT-5.5) goes from 38.3 without a skill to 51.1 with SkillOpt; it reports
no GEPA in that harness. SkillOpt accepted one update (step 1, validation 0.708) and rejected the rest; GEPA's final
skill is its candidate 13 of 18 (validation 0.750, the initial skill 0.625). Neither trained skill contains a test
answer.

A Codex session longer than 180 s is a wrong answer, as in SkillOpt (of 516 runs: 12 without a skill, 17 with the
initial skill, 6 with GEPA's, 16 with SkillOpt's). SkillOpt's pinned code reports them as
`write() argument must be str, not bytes` (its timeout handler writes Codex's output as bytes), which is also the
failure the learners see. GLM sessions look for data the question lacks (exchange rates, price indexes) outside their
workspace and on the internet, so each session runs in a container of its own (`utils/agent/codex_sandbox.py`) whose
files are only its workspace, a fresh Codex home and the corpus (read-only), with a private `/tmp`, on a Docker network
whose only way out is a proxy to the model provider's API; Codex's own workspace-write sandbox runs inside it, as
SkillOpt asks. In the 3,438 rollouts of these runs (25,151 commands in their transcripts; a session that times out
leaves none), about 200 tried the network (BLS, World Bank, FRASER) and about 310 looked outside their workspace; none
got anything: names did not resolve, the image has no curl or wget, and the container holds nothing else. The proxy
let through only connections to the model provider and refused Codex's own to chatgpt.com, ab.chatgpt.com and
api.github.com. A session's shell gets only Codex's core environment variables (no API key): Codex's
shell snapshot, which would bring back the whole environment, is off. An earlier run on macOS, with Codex's own
sandbox limited to reads of the system, the workspace and the corpus, gave 0.539, 0.531, 0.591 and 0.597 for the
four rows; there `/tmp` stayed shared and the sessions' shells inherited the environment of the shell that started
the runs.

The trained adapters are in `gepa/adapter/` and `skillopt/adapter/`, with their cards. Numbers they copied from
training answers are replaced by made-up numbers of the same form, so the cards' scores are those of the adapters
as trained. Differences from the paper: GLM models instead of GPT-5.x and Qwen; SkillOpt's offline file tools; in the
Codex harness, a Linux container per session with only the workspace and the corpus and only the model provider
reachable, Codex's shell snapshot off, reasoning effort medium (SkillOpt's Codex default leaves Codex's own) and 8
rollouts at a time (SkillOpt: 4); questions and answers from the latest dataset revision
(`data.json` lists the items whose sources changed: 6 test and 2 training questions; the results hold without them,
except that in the Codex harness GEPA − no skill becomes +5.8 [+0.6, +11.2]).

## Code

```text
utils/agent/        OfficeQAAgent (agent.py): SkillOpt's rollout (direct or Codex), the adapter's context as the skill;
                    codex_sandbox.py and docker/: the Codex harness's container, network proxy and image
utils/prepare.py    tasks.jsonl, the splits, data.json and seed_adapter/      utils/judge.py   SkillOpt's exact match
utils/task-info/    the task for the learner
prompt_only/ gepa/ skillopt/   smoke.yaml and run.yaml per method, smoke_codex.yaml and run_codex.yaml for the
                    Codex harness; gepa/ and skillopt/ include the adapter their run.yaml trained
```
