# LIBERO

LIBERO's five suites ([Liu et al., 2023](https://arxiv.org/abs/2306.03310)): Object, Spatial, Goal, Long
(`libero_10`) and LIBERO-90, 130 tasks, and LIBERO-Pro's perturbations of the four ten-task suites, the eighty
(suite, task) pairs ASPIRE's harness requires: `_swap` (the objects' positions exchanged) and `_task` (the
instruction and goal changed, so a task's name no longer states it). A task is a program: a Codex session writes a
Python program against [ASPIRE](https://research.nvidia.com/labs/gear/aspire/)'s Code-as-Policy API (perception,
grasping, motion, the gripper; [code](https://github.com/NVlabs/ASPIRE), `cap-harness --benchmark libero-pro`, whose
pinned LIBERO registers these suites), develops it in simulation on development seeds, and the judge scores it once
on held-out seeds by the task's own success check.

| Folder | What it is |
|---|---|
| `utils/prepare.py` | The 210 tasks (a suite and a task id each) with their seeds and splits; the verifier's rows and the reruns |
| `utils/agent/` | A method-neutral acting prompt (`prompt.md`, written here: LIBERO has none) and the workspace without ASPIRE's method pages, for a method that brings no prompt of its own |
| `utils/task-info/` | What a task and its record are, for the learner |
| `utils/launch.sh` | Starts a method folder's run: the simulator service and hermit |
| [`aspire/`](aspire/) | The ASPIRE method: its fix-loop prompts, verifier and coordinator. `run.yaml` (Object, Spatial, Goal, Long: 40 tasks), `run_90.yaml` (LIBERO-90), `run_pro.yaml` (LIBERO-Pro: 80), `run_pro_long.yaml` (LIBERO-Long-Pro), `smoke*.yaml`, `launch.sh` |
| [`prompt_only/`](prompt_only/) | The prompt-only template: one `ADAPTER.md` the built-in Codex Evolver revises after each task, with the method-neutral prompt. `run.yaml`, `run_pro.yaml`, `smoke.yaml`, `smoke_luna.yaml`, `launch.sh` |

The `*_luna.yaml` configs run GPT-5.6-Luna on the machine's ChatGPT login; the others GLM 5.3 Flash on Fireworks.

## The engine and the protocol

The engine is the [Robosuite example](../aspire_robosuite/)'s: its simulator service (`bin/sim` in the session's
workspace), its task functions (the workspace, the record), its judge and its sandboxes, which that README describes.
It takes the benchmark from the task row (`"benchmark": "libero-pro"`) and with it ASPIRE's settings for LIBERO (its
fix-loop template's Benchmark Profiles):

- `.venv-libero/bin/cap-harness`, with `LIBERO_CONFIG_PATH=$ASPIRE_ROOT/.libero` and
  `TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1`;
- `--init-mode seeded` on every run, the session's and the judge's, so that each seed is a scene of its own;
- ASPIRE's LIBERO seeds: development 51-65, unseen-check 66-80, held-out 1-50, the verifier's 51-55;
- `docs/libero-pro.md` in the session's workspace without its "Task examples" section, which names ASPIRE's worked
  programs per task. The task sandbox is shown the staged copy only, not the checkout's page; a page without that
  section is refused rather than passed whole.

This is not LIBERO's own evaluation protocol, which scores a task on its fifty fixed initial states: here a seed
is a scene LIBERO samples, so a success rate is comparable neither with results reported on LIBERO's fixed states
nor with ASPIRE's. The fixed states are cap-harness's default `--init-mode saved`, which fails on every reset at the
pinned commit ([NVlabs/ASPIRE#31](https://github.com/NVlabs/ASPIRE/pull/31) fixes it); once a fixed commit is
pinned, the judge can score the held-out seeds 1-50 on them. ASPIRE notes that seeded scenes are not guaranteed
distinct for every task: where a task's placement leaves little room, a held-out seed can give a development seed's
layout.

A method's number is measured the same way whatever its prompt: the workspace, `bin/sim`, the seeds, the scored
program (`fix_code.py`) and the judge are the engine's. ASPIRE's configs give the session ASPIRE's own prompts (the
fix-loop subagent and the evolutionary search); `prompt_only/` gives it a method-neutral one (`utils/agent/prompt.md`:
the task, the workspace, what is scored), and a workspace without ASPIRE's evolutionary-search page and assignment
(`utils/agent/task.py:prepare`).

## Setup

As [Robosuite's](../aspire_robosuite/README.md#setup) (the providers, the render device, Codex and the sandboxes),
with LIBERO's venv instead of Robosuite's in the ASPIRE checkout:

```bash
cd ASPIRE && git checkout fa00f0ef2bc1d91d594b9aa502c22133c9f538f7
scripts/bootstrap_libero.sh                 # .venv-libero, third_party/LIBERO-PRO, .libero/
scripts/bootstrap_graspnet.sh               # .venv-graspnet: Contact-GraspNet, LIBERO's default grasp backend
```

Serve Contact-GraspNet on 8115 beside the other three (`scripts/supervise_services.sh --providers
contact_graspnet`); the launcher refuses to start without it, since `generate_grasps` would have no backend to call.

Segmentation is ASPIRE's SAM3 service (`--providers sam3`): LIBERO's scenes hold several similar objects, which
the Robosuite example's stand-in does not tell apart.

## Run

From a method folder:

```bash
cd examples/libero/aspire                                    # or examples/libero/prompt_only
export ASPIRE_ROOT=/path/to/ASPIRE FIREWORKS_API_KEY=...
ASPIRE_SIM_GPU=0 ASPIRE_CONFIG=smoke.yaml ./launch.sh        # two tasks, three seeds each
ASPIRE_SIM_GPU=0 ./launch.sh                                 # Object, Spatial, Goal, Long
ASPIRE_SIM_GPU=0 ASPIRE_CONFIG=run_pro.yaml ./launch.sh      # LIBERO-Pro
./launch.sh eval --adapter final                             # the tasks again with the final adapter
./launch.sh eval --adapter root                              # the same with the empty one
```

`utils/launch.sh` starts the Robosuite example's launcher on this folder and the method folder; its variables
(`ASPIRE_SIM_ENV`, `ASPIRE_SIM_GPU`, `ASPIRE_OUT`, `ASPIRE_EVAL_SEEDS`, ...) are described there. A method on LIBERO
is a folder of configs beside these: `utils/agent/task.py` as its prompt and workspace, or its own. Run one method
folder at a time on a checkout and an `ASPIRE_OUT`: they share the task rows, the staged docs and the service's
ledger, which each launch writes.
