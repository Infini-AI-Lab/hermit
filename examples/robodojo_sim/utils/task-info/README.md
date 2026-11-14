# RoboDojo-Sim

Each task is one manipulation task of RoboDojo-Sim (a bimanual ARX X5 in Isaac Sim; five capability dimensions:
generalization, memory, precision, long horizon, open). The acting agent writes `policy.py`, a program that
receives the observation at every control step (three RGB cameras, the arms' joints and end-effector poses, the
instruction; no depth, no object poses) and returns the next actions. It develops the program on the task's
development layouts and is scored once on held-out layouts by RoboDojo's own success check.

A task's record (learning_material/current/tasks/<id>.json) carries, under `pipeline_extra.task`:

- `program`: the session's `policy.py`, which the judge evaluated; `judge.score` is its success rate on the held-out
  layouts, and `judge.feedback` the count;
- `notes`: the session's `notes.md` (what it tried, what worked, what it would tell another task's session);
- `development`: the session's own evaluations in order (the run's directory, layouts run, how many succeeded).

The record's trajectory directory holds the session's files (`files/`: policy.py, notes.md) and its session log.
The held-out runs are not in it.
