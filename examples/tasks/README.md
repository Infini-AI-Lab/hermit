# Tasks

The tasks the example configs train and evaluate on, shared by every method. Each folder has the acting-agent
`pipeline.py` (runs one task with an adapter), the `judge.py` that scores its output, `task-info/` (the task as the
Evolver reads it) and a `prepare.py` that writes `tasks.jsonl` and the splits next to them. Prepare a task once; the
configs in the method folders point here.

| Folder | Task | Methods |
|---|---|---|
| [`officeqa/`](officeqa/) | OfficeQA: 133 questions about U.S. Treasury Bulletins; a GLM 5.3 Flash agent searches and reads the bulletins; the official strict scorer | `prompt_only`, `codex`, `gepa`, `skillopt` |
| [`officeqa_paper/`](officeqa_paper/) | OfficeQA Full in the SkillOpt paper's setting: its 50 / 24 / 172 split, each question's source pages, exact match | `gepa`, `skillopt` |
