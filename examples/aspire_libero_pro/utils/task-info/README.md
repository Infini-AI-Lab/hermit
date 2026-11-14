# ASPIRE on LIBERO-Pro

Each task is one LIBERO-Pro task (a suite and a task id; the suites are the `_swap` and `_task` perturbations of
LIBERO's Object, Spatial, Goal and Long suites) of ASPIRE's Code-as-Policy harness (NVlabs/ASPIRE). The acting agent
writes a Python program against the CaP API (perception, grasping, motion, gripper), the harness runs it in simulation, and the program is debugged
on a set of development seeds and scored once on held-out seeds. A task's language is the one its BDDL file states,
which the program reads from `get_task_context()`.

A task's record (learning_material/current/tasks/<id>.json) carries, under `pipeline_extra.task`:

- `findings`: the actor's findings.md (root causes, what fixed them, generalizable patterns with code, skill report,
  non-privileged API audit); `evosearch.findings`: the evolutionary search's, when it ran;
- `skill_report`: skill_report.json: `consulted` (verdicts on library entries), `proposed_edits`, `proposed_new`;
- `fix_code`: the selected program (`scored_program`: the fix loop's `fix_code.py`, or the search's best), which the
  judge runs once on the held-out seeds (Stage 2); `judge.score` is its success rate; `unseen`: the unseen check.

The record's trajectory directory holds the actor's files (`files/`: task_analysis.md, code/, findings.md,
skill_report.json, fix_code.py) and its session log. The held-out runs are not in it.
