# Task: OfficeQA

Questions about U.S. Treasury Bulletins, answered by a GLM 5.3 Flash tool-calling agent that searches and reads the
bulletins' text (`pipeline.py`, `corpus.py`), and scored by the official strict scorer (`judge.py`). `task-info/`
describes the task to the Evolver.

## Prepare

You need a prepared OfficeQA directory: `agent/` with the questions, the bulletin texts and their search index, and
`private/` with the answers and the official scorer (layout in the [OfficeQA recipe](../../../docs/recipes/officeqa.md)).

```bash
python examples/tasks/officeqa/prepare.py /path/to/officeqa_official_133/prepared
```

This writes `tasks.jsonl` (questions only), `splits/` and `data_root.txt` here. The splits (seed 0) are 60 training,
13 validation and 60 test questions, plus `smoke_*` (4 / 2 / 2) and `try_*` (12 training, 10 test).

The judge reads the answers from the prepared directory's `private/`. A sandboxed Evolver sees only its workspace,
its accesses and `sandbox.read` (empty in these configs), so neither the answers nor this folder are visible to it.

## Configs

[`prompt_only/officeqa`](../../prompt_only/), [`codex/officeqa`](../../codex/), [`gepa/officeqa`](../../gepa/),
[`skillopt/officeqa`](../../skillopt/).
