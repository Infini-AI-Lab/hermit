# A LIBERO task

Write a Python program that makes a simulated robot arm (a Franka Panda) do one task of LIBERO, and make it succeed
on as many scenes of that task as you can. The program is written against a Code-as-Policy API (perception,
grasping, motion, the gripper); a simulator runs it, and the task's own success check decides whether an episode
succeeded.

## Your task

- Suite `{suite}`, task id `{task_id}`. The program reads what the task asks from `get_task_context()` (its
  language, as the task's BDDL file states it): do not rely on the suite's or the task's name.
- Each seed is a different scene of the task. An episode runs at most {max_steps} simulator steps.
- Development seeds: **{dev}**. Run them as often as you like.
- Unseen-check seed(s): **{unseen}**, run once, when your program is final (below).
- After you finish, the harness runs your program once on {heldout} held-out seeds, which you never run; your score
  is the fraction of them that succeed.

## Your workspace

- `docs/api-reference.md`: the API a program may use, and its rules. `docs/run-artifacts.md`: what a run leaves.
  `docs/{benchmark_doc}`: LIBERO in this harness (the scene, the robot, the frames).
  `docs/task-exploration.md` and `scripts/scene_snapshot.py`: a way to look at a scene before you write the program.
- `bin/sim`, your only way to the simulator (perception, IK and planning services are already running):

      bin/sim validate --program <program.py>
      bin/sim run-batch --seeds <seeds> --program <program.py> --output-root <dir> [--no-videos]
      bin/sim run --seed <seed> --program <program.py> --output-root <dir> [--no-videos]

  A run writes `<dir>/results.jsonl`, and per episode its outcome, the trace of every API call the program made
  and keyframes, under `<dir>`. It takes your development seeds, and your unseen-check seeds once.
- Everything you write stays inside this workspace.

## What to hand in

- Your program is `{task_dir}/fix_code.py`. That file, as you leave it, is the one the harness scores.
- When it is final, run it once on the unseen-check seeds:
  `bin/sim run-batch --seeds {unseen} --program {task_dir}/fix_code.py --output-root {task_dir}/debug/holdout`.
- Then reply with a short report: what the program does, its development and unseen-check results, what failed and
  why, and what you would try next. Notes you write in the workspace (for example `{task_dir}/findings.md`) are kept
  with the run.
