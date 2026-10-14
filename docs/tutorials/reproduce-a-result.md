# 2. Reproduce a result

You want to run one of our experiments as we ran it: an existing task, an existing template and Evolver, our
settings. Every run below is one launcher or a few commands; the recipe page of each has the details and what the
run produces.

| Run | Task | Method | Model | Time | Recipe |
|---|---|---|---|---|---|
| ServeLearnBench retail_l3 | the benchmark's serving stream and per-window tests | mwm | GLM 5.3 flash (host and learner) | 1–2 days | [ServeLearnBench with MWM](../recipes/servelearnbench.md) |
| CL-Bench database | 40-question streams, 5 orders | mwm | GLM 5.3 flash | ~1 day per order, orders in parallel | `examples/mwm/README.md` |
| CL-Bench poker | 120-hand streams, 5 orders | mwm | GLM 5.3 flash | ~1 day per order | `examples/mwm/README.md` |
| OfficeQA | 133 questions over Treasury Bulletins | prompt_only, codex, gepa, skillopt | GLM 5.3 flash | hours | [OfficeQA](../recipes/officeqa.md) |

Common to all: the framework installed in `.venv/` (Python 3.12+), a Fireworks key in `FIREWORKS_API_KEY`, and
for the `mwm` and `codex` Evolvers the `codex` CLI and bubblewrap (`bwrap`) on the PATH. The host and the learner
reach the provider through the framework's wire meter (`harnessadapter/runtime/wire_meter.py`), started by the
launchers, which records the tokens of every run.

## ServeLearnBench

```bash
.venv/bin/pip install "git+https://github.com/Infini-AI-Lab/ServeLearnBench"
FIREWORKS_API_KEY=... examples/mwm/servelearnbench/launch.sh retail_l3
```

The launcher exports the scenario, trains in the background, then plays the per-window tests and writes the
benchmark's scores to `~/code/metaworld-runs/slb_retail_l3/test.log`.

## CL-Bench

The host plays in clbench's own environment with metaworld's host seat, so these runs need the clbench and
metaworld checkouts beside the framework (`MWM_CLBENCH`, `MWM_METAWORLD`):

```bash
python examples/tasks/clbench/database/prepare.py
MW_TAG=mwm_db WM_PORT=3401 MW_ORDERS="0 1 2 3 4" examples/mwm/clbench/launch.sh database
python examples/tasks/clbench/poker/prepare.py
MW_TAG=mwm_poker WM_PORT=3411 MW_ORDERS="0 1 2 3 4" examples/mwm/clbench/launch.sh poker
```

Each order is one `harnessadapter train`; `steps.jsonl` under `<runs>/<tag>/<tag>-o<i>/` has the per-question
scores (database: accuracy; poker: the bb/hand ledger is in `<runs>/<tag>/order<i>/task/outcomes.jsonl`).

## OfficeQA

```bash
python examples/tasks/officeqa/prepare.py /absolute/path/to/prepared     # the dataset is an external input
harnessadapter train --config examples/prompt_only/officeqa/run.yaml
harnessadapter eval  --config examples/prompt_only/officeqa/run.yaml --adapter final
harnessadapter eval  --config examples/prompt_only/officeqa/run.yaml --adapter none     # the agent without an adapter
```

Swap the config for `examples/codex/officeqa/`, `examples/gepa/officeqa/` or `examples/skillopt/officeqa/` for the
other methods.

## Reading a run

| File | What it tells you |
|---|---|
| `<run>/steps.jsonl` | one line per step: the rollouts' scores, the Evolver's status and summary, the commit |
| `<run>/step_NNNN/output/report.md` | what the learner says it changed |
| `<run>/learning_material/step_NNNN/` | what the learner saw: every task's input, output, judge and trajectory |
| `<run>/checkout/CARD.md` | the final adapter's card: provenance and evaluation results |
| `<run>/eval/<adapter>/results.jsonl` | per-task scores of an `eval` |

Next: [Your own task](your-own-task.md).
