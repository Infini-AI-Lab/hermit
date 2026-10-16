# OfficeQA

Questions about U.S. Treasury Bulletins (1939–2025), 133 of them, answered by a tool-calling agent that searches and
reads the bulletins' text, scored by the official strict scorer (tolerance 0). The agent is the framework's built-in
`react` agent: this task has no agent code, only a prompt and two tools.

## Layout

```text
utils/agent/prompt.md           the system prompt (the adapter's context is appended to it)
utils/tools.py      the task's tools, search and read, over the corpus index (corpus.py)
utils/judge.py      the official strict scorer; feedback names the reference answer unless OFFICEQA_FEEDBACK=score
utils/prepare.py    writes tasks.jsonl, splits/ and data_root.txt beside itself
utils/task-info/                the task as the learner reads it
prompt_only/ codex/ gepa/ skillopt/   one folder per method: smoke.yaml, run.yaml (try.yaml for prompt_only)
```

## Prepare

You need a prepared OfficeQA directory: `agent/` with the questions, the bulletin texts and their search index, and
`private/` with the answers and the official scorer (layout in the [OfficeQA recipe](../../docs/recipes/officeqa.md)).

```bash
python examples/officeqa/utils/prepare.py /path/to/officeqa_official_133/prepared
```

The splits (seed 0) are 60 training, 13 validation and 60 test questions, plus `smoke_*` (4 / 2 / 2) and `try_*`
(12 training, 10 test). The judge reads the answers from the prepared directory's `private/`; a sandboxed Evolver
sees only its workspace, so neither the answers nor this folder are visible to it.

## Run

```bash
export FIREWORKS_API_KEY=...
harnessadapter check --config examples/officeqa/gepa/smoke.yaml
harnessadapter train --config examples/officeqa/gepa/smoke.yaml
harnessadapter eval  --config examples/officeqa/gepa/smoke.yaml --adapter final
```

## Methods

| Folder | Evolver | Configs |
|---|---|---|
| [`prompt_only/`](prompt_only/) | one model call per step rewrites ADAPTER.md | smoke, try, run |
| [`codex/`](codex/) | a Codex agent edits the adapter (`codex_glm/`: configured for GLM 5.3 Flash) | smoke |
| [`gepa/`](gepa/) | GEPA | smoke, run |
| [`skillopt/`](skillopt/) | SkillOpt | smoke, run |
