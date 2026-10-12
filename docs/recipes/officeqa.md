# OfficeQA

This recipe makes real model calls. It uses a GLM tool-calling agent to answer questions about Treasury
Bulletins, and the `prompt_only` Evolver to revise `ADAPTER.md` from the scored answers.

Complete [Installation](../get-started/installation.md), activate the virtual environment, and run the commands from
the repository root. For a deployment-only walkthrough without data or model access, use [Quick Start](../get-started/quickstart.md).

## Prepare the data and model access

You need a prepared OfficeQA directory with this layout. The dataset and official scoring code are external inputs;
this repository does not download or create them:

```text
prepared/
  agent/
    tasks.jsonl       records with task_id and question
    corpus/           Treasury Bulletin text files
    corpus.sqlite     search index used by corpus.py
  private/
    answers.jsonl     records with task_id and answer
    reward.py         official scorer
```

`reward.py` must expose `extract_final_answer` and `score_answer`. The index must use the schema expected by
`examples/tasks/officeqa/corpus.py`. The prepared dataset used by this example has 133 tasks; the smoke test uses four
training tasks and two held-out tasks. If you do not have these assets, the [deployment walkthrough](../get-started/quickstart.md) is still
usable; training this example requires obtaining the prepared data first.

Point the example at your prepared directory:

```bash
python examples/tasks/officeqa/prepare.py /absolute/path/to/prepared
```

This writes `tasks.jsonl`, split files, and `data_root.txt` inside `examples/tasks/officeqa/`. It does not build the corpus
index. Set `FIREWORKS_API_KEY` in your shell before continuing. Both the acting pipeline and the learner in
`smoke.yaml` use Fireworks; the model configured in that file must be available to your account.

## Check, train, evaluate

Inspect `examples/prompt_only/officeqa/smoke.yaml`. It selects `prompt-only@1.0`, the `prompt_only` Evolver, batches of two tasks,
one epoch, and a training budget of USD 3 / one hour. Dollar accounting uses the prices stored in the example;
verify them for your endpoint. Usage becomes visible at call boundaries, so the budget is not a provider-side
spending cap. The separate `check` probe and `eval` calls are outside the training budget.

```bash
harnessadapter check --config examples/prompt_only/officeqa/smoke.yaml
harnessadapter train --config examples/prompt_only/officeqa/smoke.yaml
harnessadapter eval --config examples/prompt_only/officeqa/smoke.yaml --adapter final
harnessadapter eval --config examples/prompt_only/officeqa/smoke.yaml --adapter root
harnessadapter eval --config examples/prompt_only/officeqa/smoke.yaml --adapter none
```

`check` runs one training task without an adapter to probe the pipeline and judge; use `--no-probe` to skip that model
call. `train` executes two steps. On each step it runs the acting pipeline, scores the answers, lets the learner
rewrite the instructions, and checks and saves the returned adapter. `eval` separately measures a selected version
on the two test tasks. A smoke run checks that the integration works; two test questions cannot establish a reliable
performance gain.

The example requests sandboxing. On a machine without `bwrap`, programs launched through that sandbox fail; see
[Installation](../get-started/installation.md) before using an agent Evolver. The in-process `prompt_only` method calls the run's
model tool directly; enabling the sandbox does not turn the whole Python process into an isolated environment.

## Inspect the result

Paths in `smoke.yaml` are relative to that file, so the outputs are under `examples/prompt_only/officeqa/runs/`:

```text
runs/
  trees/prompt-only/          Git history of adapter versions
  smoke/
    checkout/                current adapter, including ADAPTER.md and CARD.md
    steps.jsonl              scores, changes, costs, and commit IDs per step
    learning_material/       results and available logs shown to the learner
    step_0001/state/          learner state saved after step 1
    summary.json             final tag, checkpoints, and stop reason
    eval/                    results for final, root, and none
```

```bash
cat examples/prompt_only/officeqa/runs/smoke/checkout/ADAPTER.md
cat examples/prompt_only/officeqa/runs/smoke/checkout/CARD.md
```

You can pass the `checkout/` adapter directory to `deploy`, just as you passed `.demo/adapter` earlier. That checks
host integration; effectiveness in another host needs its own evaluation. Keep the directory available if exported
tools refer to its files.

Rerunning `train` resumes an unfinished run at its last completed step. For another experiment after this one finishes,
choose another name, for example `trainer.run_name=smoke-02`, and use the same override on the corresponding `eval`
commands. The framework does not automatically choose the best checkpoint: `final` is the Evolver's final selection,
which defaults to the last adapter.

## Train with GEPA or SkillOpt

The same task trains with the `gepa` or `skillopt` Evolver instead of `prompt_only`. Both score candidate
adapters on validation questions, so their configs add a validation split (rerun `prepare.py` if `splits/` has no
`smoke_val.txt`).

```bash
python -m pip install -e '.[gepa]'
harnessadapter train --config examples/gepa/officeqa/smoke.yaml
harnessadapter eval --config examples/gepa/officeqa/smoke.yaml --adapter final
```

For SkillOpt, install `'.[skillopt]'` and use `examples/skillopt/officeqa/smoke.yaml`. The smoke configs check the
integration on a few questions; `run.yaml` next to each trains on the 60 training questions and selects on the 13
validation questions. Outputs are under each config's folder, in `runs/`; `state/` holds each method's candidates and
records. Each method's README in [`examples/`](https://github.com/haizhongzheng/MetaHarnessAdapter/tree/main/examples)
explains how it works and which settings matter.

[`examples/tasks/officeqa_paper`](https://github.com/haizhongzheng/MetaHarnessAdapter/tree/main/examples/tasks/officeqa_paper)
is OfficeQA in the SkillOpt paper's setting (its split, the questions' source pages, its scorer). The configs in
`examples/gepa/officeqa_paper/` and `examples/skillopt/officeqa_paper/` train both methods on it, and each folder
includes an adapter trained by its `run.yaml`.

For your own task, implement the pipeline and judge described in [Task integration](../framework/task.md). For how
the components exchange data, continue to [Using and training adapters](../framework/adapter-lifecycle.md).
