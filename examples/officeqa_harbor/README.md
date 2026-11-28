# OfficeQA through Harbor

[OfficeQA](https://github.com/databricks/officeqa) ([paper](https://arxiv.org/abs/2603.08655)): questions about U.S.
Treasury Bulletins whose answers are figures found in the bulletins, often after a computation. This example runs
OfficeQA Full (246 questions) as the OfficeQA paper evaluates agents: each question in its own
[Harbor](https://github.com/harbor-framework/harbor) environment with the whole corpus of 697 parsed bulletins, the
paper's agent prompt, and the paper's scorer on the agent's final answer at a 0.0% allowable relative error. The split
is 50 train, 24 validation and 172 test questions
([source](https://github.com/microsoft/SkillOpt/tree/79124b37e9a6371e13b753f8bcd7adb1e493ade1/data/officeqa_id_split));
93 of the 172 test questions are OfficeQA Pro's, on which the paper reports. It evaluates the agent with no adapter:
Codex with GPT-5.6-Luna on a ChatGPT login, or Claude Code with Claude Haiku 5.5 on a Claude subscription. Its numbers
are not comparable with [`../officeqa/`](../officeqa/), which runs OfficeQA in the SkillOpt paper's setting.

## Setup

Install the framework first ([Installation](../../docs/get-started/installation.md)). Also needed: Docker, Harbor in an
environment of its own, and the agent's login (`codex login` once for Codex; for Claude Code, the token of
`claude setup-token` exported as `CLAUDE_CODE_OAUTH_TOKEN`).

```bash
python -m venv ~/.cache/hermit/harbor-venv
~/.cache/hermit/harbor-venv/bin/pip install 'harbor==0.25.0'
python examples/officeqa_harbor/utils/prepare.py
```

`prepare.py` downloads Harbor's OfficeQA tasks at a pinned commit, rewrites them to the paper's evaluation, checks them
against `utils/dataset.lock`, and builds the image they run in. It writes `utils/tasks/` and `utils/tasks.jsonl`
(git-ignored: they hold the answers). Wait for any evaluation using this checkout to finish before preparing it again.
A failed install restores the previous data files. If prepare was interrupted, rerun it before evaluating.

## Run

```bash
hermit eval --config examples/officeqa_harbor/baseline/codex.yaml --adapter none     # Codex, the 172 test questions
hermit eval --config examples/officeqa_harbor/baseline/claude.yaml --adapter none    # Claude Code
hermit eval --config examples/officeqa_harbor/baseline/codex.yaml --adapter none \
    data.test=../utils/splits/smoke.txt trainer.run_name=codex-smoke                 # a smoke on 4 validation questions
```

The accuracy is `mean_score` in `baseline/runs/<run name>/eval/none/summary.json`, at the paper's 0.0% threshold; each
record in `results.jsonl` beside it also holds the verdicts at 0.1%, 1% and 5% (`judge.extra.rewards`). A method's
configs go in a folder of their own beside `baseline/`, with the same `agent` and `environment` and an adapter.

## Results

Agent: GPT-5.6-Luna (reasoning effort medium) with Codex 0.164.0, one Harbor environment per question, no web search.
One pass over the 172 test questions, 2026-11-25, before the verifier update for interrupted Codex turns.
The original trials have not been rescored.

| Method | All 172 test questions | The 93 OfficeQA Pro questions (same run) |
|---|---|---|
| no adapter | 58.1% | 43.0% |

The Codex no-adapter baseline scored 58.1% on all 172 test questions and 43.0% on the 93 OfficeQA Pro questions within
that same run. Claude Code's baseline has not been run. The other thresholds, the standard errors, the cost and how
these tasks differ from Harbor's and from the paper's evaluation: the
[recipe](../../docs/recipes/officeqa_harbor.md).

## Code

```text
baseline/             codex.yaml and claude.yaml: no adapter on the 172 test questions, with Codex or Claude Code
utils/prepare.py      writes tasks/, tasks.jsonl and dataset.lock from harbor-datasets, and builds the image (Dockerfile)
utils/verifier/       test.sh and judge.py: each task's verifier, with the task's own reward.py
utils/splits/         train.txt, val.txt and test.txt (50 / 24 / 172 ids), smoke.txt (4 validation questions)
utils/benchmark.yaml  the Harbor settings every config shares
utils/task-info/      the task, as a learner reads it
utils/tests/          the configs, prepare.py and the verifier, with no network and no Harbor
```
