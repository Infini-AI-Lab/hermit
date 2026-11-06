# Run harness evolution with your coding plan

Use your Codex / ChatGPT plan to run both the acting agent and the learner in Hermit. You can try methods such as
GEPA, SkillOpt, ACE and RRSI Adapter without configuring a separate model API key or API billing account. Hermit
runs the method's evaluation and selection loop; Codex supplies the model sessions that act and learn.

This tutorial uses the existing **RRSI Adapter smoke run on OfficeQA**: four training questions, two trials per
question, one round of two candidate adapters, then four held-out questions. It checks the integration rather than
establishing a performance gain. It still needs the benchmark data and execution environment described below.

## Start with a coding-agent prompt

Open the Hermit checkout in your coding agent and give it this prompt. Replace the model placeholder with a model
available to your Codex login.

```text
Help me run a small harness-evolution experiment using my Codex / ChatGPT plan.
Use <my Codex model name> for both acting and learning, without a model API key.

Read AGENTS.md, docs/tutorials/codex-plan.md and the RRSI Adapter example.
Check the prerequisites, then use examples/officeqa/rrsi_adapter/smoke_luna.yaml.
Keep its one-round protocol. Set acting concurrency to 2 and the training time
budget to 2 hours. Use the same model settings for check, train and evaluation.

Use the existing data-preparation scripts, container and egress-proxy setup.
If the OfficeQA dataset or another prerequisite is missing, tell me exactly what
I need to provide. Do not substitute synthetic data for the benchmark.

Run the checks, train, and evaluate both the seed and final adapter. Inspect
whether the round completed, which candidate was selected, and any errors.
Report the commands, output paths and results. Keep generated artifacts local;
do not commit or push anything.
```

The following sections explain the setup and commands behind that prompt.

## 1. Prepare Codex and the benchmark

Use a **Linux machine with Python 3.12+, Git, bubblewrap and Docker** for this example. RRSI's proposer and search
roles use Hermit's Linux sandbox; acting sessions run in Docker. Complete [Installation](../get-started/installation.md)
and install the two dependency groups from the repository root:

```bash
python -m pip install --upgrade "pip>=25.1"
python -m pip install -e . --group rrsi --group skillopt
```

`rrsi` supplies the search method; `skillopt` supplies OfficeQA's shared data parsing and task utilities.

Install [Codex CLI](https://learn.chatgpt.com/docs/codex/cli), then sign in with ChatGPT on the machine that will run
Hermit. Hermit currently needs Codex's file-based login cache:

```bash
codex -c 'cli_auth_credentials_store="file"' login
codex login status
```

Confirm that the authentication method is ChatGPT. Hermit reads `auth.json` under `CODEX_HOME`, or `~/.codex` when
it is unset; a login held only in the OS credential store is not enough. Hermit manages per-session copies and
refreshes through its shared login directory. See [OpenAI's authentication guide](https://learn.chatgpt.com/docs/auth)
for sign-in and credential-storage options.

Complete these existing OfficeQA setup steps once:

- [Prepare the data](../recipes/officeqa.md#prepare-the-data): obtain the gated dataset, then run `prepare.py` with
  the CSV and parsed corpus paths. This writes the task splits and initial skill used below. Dataset access is
  separate from model authentication.
- [Run in the Codex harness](../recipes/officeqa.md#run-in-the-codex-harness) and
  [A model on a ChatGPT login](../recipes/officeqa.md#a-model-on-a-chatgpt-login): build the Linux Codex image,
  create the internal Docker network, and start the login egress proxy. The example names
  `officeqa-codex:0.164.0`, `oq-sandbox` and `oq-egress-login`. The image needs a Linux Codex distribution matching
  Docker's architecture, even if you normally use Codex on another operating system.

Then prepare RRSI's seed and trial rows:

```bash
python examples/officeqa/rrsi_adapter/make_seed.py
python examples/officeqa/rrsi_adapter/make_rows.py
```

## 2. Check, train and evaluate

Run from the repository root. Choose a model your login can actually use; the checked-in recipe's
`gpt-5.6-luna` is the model used for its reported experiment, not a requirement to use your plan.

```bash
HERMIT_CODEX_MODEL='your-codex-model-name'

hermit check --config examples/officeqa/rrsi_adapter/smoke_luna.yaml --no-probe \
  agent.model.name="$HERMIT_CODEX_MODEL" evolver.model.name="$HERMIT_CODEX_MODEL" \
  agent.concurrency=2 trainer.budget.hours=2

hermit train --config examples/officeqa/rrsi_adapter/smoke_luna.yaml \
  agent.model.name="$HERMIT_CODEX_MODEL" evolver.model.name="$HERMIT_CODEX_MODEL" \
  agent.concurrency=2 trainer.budget.hours=2

hermit eval --config examples/officeqa/rrsi_adapter/smoke_luna.yaml --adapter seed_adapter \
  agent.model.name="$HERMIT_CODEX_MODEL" evolver.model.name="$HERMIT_CODEX_MODEL" \
  agent.concurrency=2

hermit eval --config examples/officeqa/rrsi_adapter/smoke_luna.yaml --adapter final \
  agent.model.name="$HERMIT_CODEX_MODEL" evolver.model.name="$HERMIT_CODEX_MODEL" \
  agent.concurrency=2
```

`check --no-probe` checks prerequisites without a task rollout; it does not prove model access or learner execution.
Training normally evaluates H_0 and two candidates: **24 acting rollouts**, plus candidate liveness smokes and the
proposer, analyst, digester and critic sessions. Each evaluation command adds four acting rollouts. A small task
split can still consume substantial plan usage. The two-hour training budget can end the run before its round
finishes; probes and separate evaluations are outside that training budget.

Outputs are under `examples/officeqa/rrsi_adapter/runs/smoke-luna-rrsi/`. Inspect:

- `checkout/ADAPTER.md` and any additional learned files;
- `step_NNNN/output/report.md` for candidate screening, round decisions and the selected incumbent;
- `workspace/state/rrsi/frontier.json` for the incumbent and round trajectory;
- `eval/seed_adapter/summary.json` and `eval/final/summary.json` for held-out scores and execution errors.

Check that the round completed before treating the output as a completed RRSI smoke. `final` is the incumbent H_T;
an interrupted run can return its last incumbent. A run that completes with no accepted candidate is also valid.
Keep model and task settings fixed when comparing seed and final scores.

For a separate experiment, change both `trainer.run_name` and `trainer.tree_dir`; apply both overrides consistently
to its training and evaluation commands. An unfinished run resumes automatically. Keep generated records and adapter snapshots local,
as described in [Contributing](https://github.com/Infini-AI-Lab/hermit/blob/main/CONTRIBUTING.md).

## How this works for other methods

There are two learner configurations, depending on how the method calls its model:

| Method's learner | Configuration | Examples |
|---|---|---|
| Requests text replies for reflection or revision | `llm.cli: codex` with `login: chatgpt`; each request becomes a Codex session in a sandbox of its own | GEPA, SkillOpt, ACE, Continual Harness |
| Edits files as an agent | `evolver.base: codex` and `evolver.model.login: chatgpt`; sessions run in the method's workspace | Harness², Meta-Harness Adapter, AHE Adapter, RRSI Adapter |

For the first form, the model settings look like this; use the method's existing recipe for its remaining fields:

```yaml
llm:
  cli: codex
  model: your-codex-model-name
  login: chatgpt
  effort: medium
  web_search: false
  isolation: sandbox     # the default: a bubblewrap sandbox of its own, the login's hosts through a proxy
  timeout: 3600
```

`isolation: container` with `container: {image, network, proxy}` runs each session in that image instead.

For the second form, keep the method's `evolver.name` and parameters, and configure its Base Agent and model:

```yaml
evolver:
  name: rrsi_adapter
  # Keep the recipe's method-specific params here.
  base: codex
  model: {name: your-codex-model-name, login: chatgpt, effort: medium}
```

The acting agent also needs `agent.base: codex` and `agent.model.login: chatgpt`, as in the smoke configuration.
Changing only the learner does not change an API-based actor. Use a complete recipe for the task's preparation,
scoring, container and sandbox settings. For example, [ACE](../methods/ace.md) already provides
`examples/officeqa/ace/smoke_codex_luna.yaml`; GEPA and SkillOpt have `run_codex_luna.yaml` recipes.

Plan-backed sessions use the account's available models and usage limits. Hermit reports unknown dollar cost when
there is no per-token price; use time/token budgets and inspect the shared plan status described in
[the login setup](../recipes/officeqa.md#a-model-on-a-chatgpt-login). Extra services still need their own setup:
ReHarness's embedding model, for example, is not provided by this Codex text-model route.

## What about a Claude Code plan?

Claude Code also supports subscription sign-in, including Pro and Max; see
[Claude's subscription setup](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-max-or-ultra-plan).
Hermit already has a Claude Code Base Agent, but its current coverage differs from the Codex walkthrough above:

| Path | Current support |
|---|---|
| Acting agent | `agent.base: claude` uses Claude Code with the task's preparation and scoring functions. The current container runner is Codex-only, so it needs its own execution setup. |
| Agent-style learner | The built-in `claude` Evolver and Meta-Harness Adapter support Claude Code sessions. RRSI Adapter supports a Claude proposer, but its analyst, digesters and critic still require Codex. AHE Adapter's learners are Codex-only. |
| Text-reply learner (`llm.cli`) | Only `codex` is accepted today. Switching GEPA, SkillOpt or ACE to `llm.cli: claude` requires shared-layer support first. |

Hermit's Claude sign-in path copies `~/.claude/.credentials.json` into the session's home when no API binding is
configured; it does not read a login stored only in macOS Keychain. The Codex-specific `login: chatgpt`, container
and plan-management settings above do not configure Claude Code. A complete Claude-plan recipe has not been
validated in this tutorial.

Continue with [Choose a method](../methods/index.md), the [OfficeQA recipe](../recipes/officeqa.md), or
[Bring your task](your-own-task.md) to use your own benchmark.
