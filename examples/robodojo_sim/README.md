# RoboDojo-Sim

[RoboDojo](https://github.com/RoboDojo-Benchmark/RoboDojo)'s simulation benchmark
([arXiv:2607.04434](https://arxiv.org/abs/2607.04434)): forty-two manipulation tasks for a bimanual ARX X5 in Isaac
Sim, in five capability dimensions (generalization, memory, precision, long horizon, open). RoboDojo evaluates a
policy served through [XPolicyLab](https://github.com/XPolicyLab/XPolicyLab): at every control step the policy
receives an observation and returns actions. Here the policy is a program: for each task a Codex session writes
`policy.py` (`act(obs) -> actions`), develops it against the simulator on the task's development layouts, and the
harness scores it once on held-out layouts with RoboDojo's own success check.

**Status: a first implementation.** The policy, the engine and the service were written from RoboDojo's and
XPolicyLab's source and are tested against a stand-in for `scripts/robodojo.sh` (`utils/tests/`). On RoboDojo
`f57e163` with its pinned XPolicyLab (`5d0be14`), with the simulator in RoboDojo's container, a program's actions
move the robot and RoboDojo's result comes back per layout through the engine, through the service and `bin/sim`
on the development layouts, and through the judge on the held-out ones. No session of a model, smoke or training
run has been made; what is left is listed under [Not verified](#not-verified).

| Folder | What it is |
|---|---|
| `utils/policy/hermit_program/` | The harness's XPolicyLab policy: `model.py` (the server side: passes each observation to the program's worker and checks its action chunk), `deploy.py` (the episode loop, which runs in the simulator's process), `deploy.yml` and the launch scripts |
| `utils/policy/worker.py` | The process that runs the program, in a sandbox of its own |
| `utils/policy/install.py` | Copies the policy into the RoboDojo checkout (`XPolicyLab/policy/hermit_program`) |
| `utils/agent/dojo.py` | The engine: one evaluation of a program (`scripts/robodojo.sh eval`), its result per layout |
| `utils/agent/split_eval.sh` | One evaluation with the simulator in RoboDojo's container (`ROBODOJO_IMAGE`) |
| `utils/agent/dojo_service.py`, `sim_cli.py` | The simulator service on the host and `bin/sim`, the actor's client of it (the ASPIRE example's service with this engine) |
| `utils/agent/task.py`, `prompts/`, `docs/interface.md` | The Codex actor's task functions, its prompt and the interface page it is given |
| `utils/prepare.py` | The forty-two tasks with their development and held-out layouts |
| `utils/judge.py` | The program on the held-out layout sets, on the host |
| `prompt_only/` | `run.yaml`, `smoke.yaml`, `smoke_luna.yaml` (the smoke with GPT-5.6-Luna on the machine's ChatGPT login), `launch.sh`: the prompt-only template with the built-in Codex Evolver |

## Protocol

RoboDojo's saved layouts come in sets (`--seed` 0, 1, 2) and an evaluation runs a set's first N. A task's row
keeps development and scoring apart by set: a session develops on set 2 (its first 10 layouts), and the judge
scores on sets 0 and 1 (their first 25 layouts each). The score is the fraction of those fifty layouts that
succeeded; a layout with no result counts as failed (RoboDojo drops a layout whose episode raised or whose scene
went unstable and scores a later one instead, which a program must not gain by, so the later one is not counted). **This is not RoboDojo's leaderboard protocol**, which scores all three sets with fifty layouts each: a
number from here is on two sets, at half their layouts, with the third developed on.

The observation is RoboDojo's default for `arx_x5`: three RGB cameras, the arms' joints and end-effector poses,
the grippers, the instruction. It has no depth and no camera calibration, so a program localizes from images alone;
RoboDojo can emit both with another environment config, which this example does not add yet.

## What a program can reach

RoboDojo's success check reads privileged simulator state, and its checkout holds the saved layouts of every set,
the success conditions and the results, beside which XPolicyLab's policy server runs. So:

- The program is never imported by the policy server. It runs as a worker in a bubblewrap sandbox with no network
  that reads the worker and a directory holding the program alone: not the checkout, and not the run's output,
  where the held-out results are written while it runs (`dojo.Engine._worker`; the sandbox's own check finds both
  missing inside). It receives RoboDojo's observation and returns an action chunk.
- Every action that reaches the simulator is whole and bounded (`model.py`: `checked`): both arms in one mode and
  both grippers, what the program left out filled from the observation, numbers within 1000 after the cast to
  float32. RoboDojo raises on a partial action and drops the layout unscored; with whole actions and the fixed
  denominator above, a program cannot remove a failing layout from its score.
- The episode loop that runs inside the simulator's process (`deploy.py`) is the harness's and fixed; the engine
  refuses to run when the installed policy differs from the one here.
- A session reaches the simulator only through `bin/sim`, and the service runs only its task's development set;
  the held-out sets are run by the judge. The task sandbox is shown the actor's code and the interface page, and
  nothing of RoboDojo; a run's files in the workspace are the result, the videos and the program's own log
  (RoboDojo's log and the sandbox's record, which name host paths, stay with the service).
- A program that fails, answers late or answers with an invalid chunk holds the robot's joints to the horizon; the
  episode fails on its own.

What this does not cover: the layouts of a set run in order, so a program can count episodes; it learns nothing
from that without the layout files. The held-out sets are RoboDojo's public evaluation layouts: a model that has
seen them elsewhere is outside what a sandbox can hold.

In RoboDojo's Assets every task has layouts 0 to 24 in each of the three sets, and the sets differ, except for
`play_tic_tac_toe`, whose three sets are the same layouts: its held-out layouts are its development layouts, so its
score is not a held-out score.

## Setup

1. RoboDojo, as its documentation installs it (Python 3.11, Isaac Sim 5.1, Isaac Lab 2.3; an NVIDIA GPU with 16 GB
   or more; `bash scripts/init_assets.sh` for the Assets), at the commit above, with its `XPolicyLab` submodule.
2. The policy server's environment (`ROBODOJO_POLICY_ENV`: a conda environment's name, default the simulator's
   `RoboDojo`, or an environment's path) needs XPolicyLab's server dependencies and numpy. The program itself runs
   on hermit's Python, which needs numpy.
3. Codex and bubblewrap, as for the other examples (docs/guides/sandbox.md).

Where Isaac Sim does not install on the host (it needs glibc 2.35: RHEL 9 has 2.34), the simulator runs in
RoboDojo's container and the policy server, with the program's worker, on the host (`utils/agent/split_eval.sh`,
RoboDojo's `docker/README.md`). Build the image in the checkout, make the policy server's environment, and name
both:

```bash
docker build -t robodojo:f57e163 .                         # in the RoboDojo checkout; about 36 GB
bash scripts/init_assets.sh && python utils/update_embodiment_config_path.py      # the Assets, about 40 GB
python3.11 -m venv /path/to/policy-env && /path/to/policy-env/bin/pip install numpy pyyaml \
  opencv-python-headless h5py 'websockets>=14' msgpack msgpack-numpy pydantic
export ROBODOJO_IMAGE=robodojo:f57e163 ROBODOJO_POLICY_ENV=/path/to/policy-env
```

The container is shown every GPU, as RoboDojo's image needs to render, and simulates on `ROBODOJO_GPU`; its caches
go under `ROBODOJO_CACHE` (default `.cache/hermit-container` in the checkout), a directory per GPU. One simulator
runs on a GPU at a time: with `ROBODOJO_JUDGE_GPU` the judge has a GPU of its own and does not wait for the
sessions' evaluations.

```bash
cd examples/robodojo_sim/prompt_only
export ROBODOJO_ROOT=/path/to/RoboDojo FIREWORKS_API_KEY=...
ROBODOJO_GPU=0 ROBODOJO_CONFIG=smoke.yaml ./launch.sh     # two tasks, two development and two held-out layouts
ROBODOJO_GPU=0 ./launch.sh                                # the forty-two tasks
```

## Not verified

- `scripts/robodojo.sh eval` on a host with a native install: only the container path was run.
- A session of a model, the smoke and any training run: the engine, the service and the judge were run on
  `stack_bowls` with programs written by hand.
- The configs' timeouts and budgets. Measured on an L40: about four minutes to start the simulator and three
  minutes for a layout of `stack_bowls` (800 steps).
- Whether a program can solve any task from RGB alone.
