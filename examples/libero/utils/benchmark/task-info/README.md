# LIBERO

Each task is one LIBERO task, a suite and a task id: LIBERO's own suites (`libero_object`, `libero_spatial`,
`libero_goal`, `libero_10`, `libero_90`) or LIBERO-Pro's perturbations of the four ten-task ones (`_swap`: the
objects' positions exchanged; `_task`: the instruction and goal changed). It runs in ASPIRE's Code-as-Policy harness
(NVlabs/ASPIRE): the acting agent writes a Python program against the CaP API (perception, grasping, motion,
gripper), the harness runs it in simulation, and the program is developed on a set of development seeds and scored
once on held-out seeds. A task's language is the one its BDDL file states, which the program reads from
`get_task_context()`.

A task's record (learning_material/current/tasks/<id>.json) carries, under `pipeline_extra.task`:

- `fix_code`: the scored program (`scored_program`: `fix_code.py`, or the evolutionary search's best when ASPIRE's
  prompts ran one), which the judge runs once on the held-out seeds; `judge.score` is its success rate;
- `unseen`: the unseen check, the program's one run on seeds it was not developed on;
- `findings`: the session's findings.md, where it wrote one (ASPIRE's prompts ask for root causes, what fixed them,
  generalizable patterns with code and a non-privileged API audit); `skill_report`: skill_report.json, ASPIRE's
  verdicts on library entries and proposed edits, where it wrote one; `evosearch.findings`: the evolutionary
  search's, when it ran.

The record's trajectory directory holds the session's files (`files/`: fix_code.py and, where written,
task_analysis.md, code/, findings.md, skill_report.json) and its session log. The held-out runs are not in it.
