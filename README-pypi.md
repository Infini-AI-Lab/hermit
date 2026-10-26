# Hermit

A framework for continual learning and recursive self-improvement through harness adapter evolution.

Hermit trains the instructions, skills, roles and tools an existing agent uses, from the feedback its task attempts
produce. The learned files form a **harness adapter**: a versioned directory the agent consumes (Codex, Claude Code
and native plugins are supported hosts). Model weights stay unchanged. You bring an acting pipeline, tasks and a
judge; the framework runs the training loop, checks every update and keeps the records.

```bash
python -m pip install hermit-harness
hermit --help
```

The package holds the framework, the adapter templates and the built-in learning methods (prompt-only, GEPA,
SkillOpt, MetaWorld Model, Harness², Continual Harness). GEPA, SkillOpt and Harness² call their authors' packages,
pinned to Git commits in the repository's `requirements-gepa.txt`, `requirements-skillopt.txt` and
`requirements-harness2.txt`. The examples, the benchmark recipes and the documentation are in the repository:

- Repository: https://github.com/Infini-AI-Lab/hermit
- Documentation: https://github.com/Infini-AI-Lab/hermit/tree/main/docs
- Changelog: https://github.com/Infini-AI-Lab/hermit/blob/main/CHANGELOG.md

Apache-2.0.
