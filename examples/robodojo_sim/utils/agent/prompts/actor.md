You are writing a robot control program for one RoboDojo-Sim manipulation task.

## The task

- Task: `{task}` (capability dimension: {dimension})
- The task's instruction is given to your program in every observation (`obs["instruction"]`); it can differ
  between episodes.

## What to deliver

A file `policy.py` in this workspace that defines `act(obs)` (and optionally `reset()`), as `docs/interface.md`
specifies. Read that page first: it is the whole interface (the observation, the action, the robot, how an episode
ends). When you finish, `policy.py` is evaluated once on layouts you have not run; that success rate is your score.
Also write `notes.md`: what you tried, what worked and what did not, and what you would tell someone solving
another task of this benchmark.

## How to work

Evaluate your program in the simulator from this workspace:

    bin/sim validate --program policy.py
    bin/sim run --layouts <N> --program <program.py> --output-root <dir>

`bin/sim run` runs the first N of your {dev} development layouts and prints each layout's result. It writes into
`<dir>`: `records.json` (success and score per layout), `robodojo/` (RoboDojo's `_result.json` and a video per
camera and episode, named with `success` or `fail`), `program.jsonl` and `program.stderr.log` (your program's
failures and what it printed). A layout with no result counts as failed when you are scored.

`bin/sim run` does not return until the evaluation has ended: several minutes to start the simulator and several
minutes a layout. Wait for it, however long your tool lets a command run before it stops watching it (the command
says once a minute that it is still running), and read `<dir>/records.json` only after it has returned: a program
you have not seen run is untested. Start with one layout, look at what the robot did, and widen once it works. The observation has no depth and no object poses, so a program has to work
from the three camera images and the robot's own state; you can print from your program to see what it receives.

The layouts you are scored on are a different set, which you cannot run. Do not look for the simulator, its files
or other sessions: none of it is available to you, and your score is only what your program does in the scene.

{library}
