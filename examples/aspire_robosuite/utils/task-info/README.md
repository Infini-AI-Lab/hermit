# ASPIRE on Robosuite

Each task is one Robosuite task of ASPIRE's Code-as-Policy harness (NVlabs/ASPIRE). The acting agent writes a
Python program against the CaP API (perception, grasping, motion, gripper), the harness runs it in simulation, and
the program is debugged on a set of development seeds and scored once on held-out seeds.

A task's record (learning_material/current/tasks/<id>.json) carries, under `pipeline_extra`:

- `findings`: the actor's report (root causes, what fixed them, generalizable patterns with code, skill report,
  non-privileged API audit);
- `skill_report`: the same as JSON: `consulted` (verdicts on library entries), `proposed_edits`, `proposed_new`
  (skill, title, trigger, code, evidence);
- `versions`: every program version with its development-seed score;
- `program`: the selected program; `debug_passed` / `debug_total`; `eval_passed` / `eval_total`.

The trajectory directory holds every version (`code/`), each sweep's per-seed records and per-primitive digests
(`sweeps/<version>/`), the keyframes the actor was shown (`keyframes/`), `findings.md`, `skill_report.json` and the
transcript.
