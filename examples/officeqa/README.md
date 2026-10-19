# OfficeQA

[OfficeQA](https://github.com/databricks/officeqa) ([dataset](https://huggingface.co/datasets/databricks/officeqa), [paper](https://arxiv.org/abs/2603.08655)): questions about U.S. Treasury Bulletins (1939–2025) whose
answers are figures found in the bulletins, often after a computation. This example runs OfficeQA Full (246
questions) as the [SkillOpt paper](https://arxiv.org/abs/2605.23904) does: its 50 / 24 / 172 train / validation /
test split, each question's parsed source pages in the prompt, and exact match, with the acting agent in either of
the paper's harnesses: direct chat, with read and grep tools limited to the question's source files, or Codex, a
`codex exec` session with the whole corpus. The adapter's `ADAPTER.md` is the agent's skill. Three methods train it,
one folder each: prompt-only, GEPA and SkillOpt; they are compared with no skill and with SkillOpt's initial skill.

## Setup

Install the framework first ([Installation](../../docs/get-started/installation.md)), with the `skillopt` extra (the
agent is SkillOpt's own rollout and the judge its scorer) and the `gepa` extra. Also needed: access to the gated
Hugging Face dataset `databricks/officeqa` (`officeqa_full.csv`, and `treasury_bulletins_parsed/` with
`transformed/` and `jsons/`), and a Fireworks key in `FIREWORKS_API_KEY`, or another backend
([Model backends](../../docs/guides/model-backends.md)). The Codex harness also needs the Codex CLI (`codex`).

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
questions, three passes each); `--adapter adapter` evaluates the adapter included in `gepa/` or `skillopt/`.
Before a `run.yaml`, run its folder's `smoke.yaml` (a few questions) and `harnessadapter check --config ...` (add
`--no-probe` to skip its one model call). `run_codex.yaml` and `smoke_codex.yaml` are the folder's `run.yaml` and
`smoke.yaml` with the agent in the Codex harness (`agent.params.harness: codex`) and no budget; evaluate in that
harness with them, e.g. `--config examples/officeqa/skillopt/run_codex.yaml --adapter none`. Codex's workspaces and
homes are under `~/.cache/harnessadapter/officeqa-codex-actor/`, outside the checkout.

## Results

Agent: GLM 5.3 Flash (reasoning effort medium) in SkillOpt's rollout, in direct chat (`run.yaml`) or in SkillOpt's
Codex harness (`run_codex.yaml`; Codex CLI 0.134). Learner: GLM 5.3 (medium). 172 test questions, three passes each.

All three methods train the same adapter, one `ADAPTER.md` (the prompt-only template), from SkillOpt's initial
skill; each folder is named after its Evolver.
**prompt-only** ([`prompt_only/`](prompt_only/), [method](../../docs/methods/prompt-only.md)): after every 10
training questions, one call to GLM 5.3 rewrites the skill from the failures and the judge's feedback; there is no
validation gate, and the final skill is the last one.
**GEPA** ([`gepa/`](gepa/), [method](../../docs/methods/gepa.md)): GLM 5.3 reflects on three trajectories at a time
to propose a new skill, and the candidates are kept on a Pareto front over the 24 validation questions.
**SkillOpt** ([`skillopt/`](skillopt/), [method](../../docs/methods/skillopt.md)), with the paper's
hyperparameters: GLM 5.3 analysts propose edits from batches of 40 questions, a learning rate bounds how many are
applied, and the validation questions gate every update.

| Method | direct chat | Codex harness |
|---|---|---|
| no skill: the agent without an adapter | 0.415 | 0.539 |
| SkillOpt's initial skill (`utils/seed_adapter/`) | 0.465 | 0.531 |
| **prompt-only** (`prompt_only/`) | not run yet | 0.514 |
| **GEPA** (`gepa/`) | 0.564 | 0.591 |
| **SkillOpt** (`skillopt/`) | 0.576 | 0.597 |

Paired per question with 95% bootstrap intervals, in direct chat both trained skills beat no skill (GEPA +14.9
[+9.5, +20.5] points, SkillOpt +16.1 [+10.3, +22.1]) and the initial skill (+9.9, +11.0); SkillOpt − GEPA is +1.2
[−3.1, +5.4], so the two methods are not separated.

In the Codex harness the agent starts higher (it searches the whole corpus and runs its own scripts) and the initial
skill does not help (−0.8 [−4.7, +3.1]). GEPA beats no skill (+5.2 [+0.6, +10.1]) and the initial skill (+6.0 [+1.4,
+10.7]); SkillOpt beats the initial skill (+6.6 [+0.6, +12.6]) and is borderline against no skill (+5.8 [−0.4,
+12.2]); SkillOpt − GEPA is +0.6 [−4.1, +5.4]. Both beat prompt-only (GEPA +7.8 [+2.5, +13.0], SkillOpt +8.3 [+4.1,
+12.8]), whose skill grew to about 5,900 words and is not separated from no skill (−2.5 [−8.9, +4.1]). The paper's
Codex harness (GPT-5.5) goes from 38.3 without a skill to 51.1 with SkillOpt; it reports no GEPA in that harness.
SkillOpt accepted one update (step 1) and rejected the rest; GEPA's final skill is its candidate 14 of 20 (validation
0.792, the initial skill 0.542).

A Codex session longer than 180 s is a wrong answer, as in SkillOpt (of 516 runs: 22 without a skill, 43 with the
initial skill, 47 with prompt-only's, 5 with GEPA's, 29 with SkillOpt's). SkillOpt's pinned code reports almost all
of them as `write() argument must be str, not bytes` (its timeout handler writes Codex's output as bytes), which is
also the failure the learners see. Codex's own workspace-write sandbox lets a session read the whole disk, and GLM
sessions look outside their workspace for data the question lacks (exchange rates, price indexes): they list home
folders, query Spotlight and try the network. `utils/agent/codex_sandbox.py` keeps that sandbox but limits reads to
the system, the workspace and the corpus, and gives each rollout its own Codex home and TMPDIR. Codex's built-in
rules keep `/tmp` readable and writable for every session whatever the profile says, so for these runs the folders
under `/tmp` that held answers (copies in earlier work folders) were made inaccessible (mode 000). In their 24,787
commands every read outside the workspace, the rollout and the corpus was refused, no command or output names an
answer file, and no transcript shows a session reading a file another session left in `/tmp`. The sessions' shells
inherited the environment of the shell that started the runs, and 11 sessions printed it (paths and account
identifiers of the launching shell); a session's shell now gets only Codex's core variables.

The trained adapters are in `gepa/adapter/` and `skillopt/adapter/`, with their cards. Numbers they copied from
training answers are replaced by made-up numbers of the same form, so the cards' scores are those of the adapters
as trained. Differences from the paper: GLM models instead of GPT-5.x and Qwen; SkillOpt's offline file tools; in the
Codex harness, reads limited to the workspace and the corpus, reasoning effort medium (SkillOpt's Codex default
leaves Codex's own) and 8 rollouts at a time (SkillOpt: 4); questions and answers from the latest dataset revision
(`data.json` lists the items whose sources changed: 6 test and 2 training questions; the results hold without them).

## Code

```text
utils/agent/        OfficeQAAgent (agent.py): SkillOpt's rollout (direct or Codex), the adapter's context as the skill
utils/prepare.py    tasks.jsonl, the splits, data.json and seed_adapter/      utils/judge.py   SkillOpt's exact match
utils/task-info/    the task for the learner
prompt_only/ gepa/ skillopt/   smoke.yaml and run.yaml per method, smoke_codex.yaml and run_codex.yaml for the
                    Codex harness; gepa/ and skillopt/ include the adapter their run.yaml trained
```
