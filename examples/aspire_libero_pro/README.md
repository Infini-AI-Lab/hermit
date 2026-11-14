# ASPIRE on LIBERO-Pro

The eighty LIBERO-Pro `(suite, task)` pairs [ASPIRE](https://research.nvidia.com/labs/gear/aspire/)'s harness requires
([code](https://github.com/NVlabs/ASPIRE): `cap-harness --benchmark libero-pro`): the `_swap` and `_task`
perturbations (LIBERO-Pro) of LIBERO's Object, Spatial, Goal and Long
(`libero_10`) suites, ten tasks a suite, run as [`aspire_robosuite/`](../aspire_robosuite/) runs Robosuite: the same
actor, verifier, coordinator, simulator service, judge and sandboxes, which that example's README describes. The
LIBERO settings of the engine (ASPIRE's LIBERO venv, seeded initial states, ASPIRE's LIBERO seeds, the staged docs)
are those of [`aspire_libero/`](../aspire_libero/README.md), which runs LIBERO's original suites. LIBERO-Pro's
other perturbations at ASPIRE's pinned commit (`_lan`, `_object`, and the scene variants such as `_with_milk` or
`_temp_x0.1`) are not among the eighty. This folder holds what is LIBERO-Pro's:

| Folder | What it is |
|---|---|
| `utils/prepare.py` | The 80 tasks (a suite and a task id each) with their seeds; the verifier's rows; the reruns |
| `utils/task-info/` | What a task and its record are, for the coordinator |
| `aspire/` | `run.yaml` (all eighty), `run_long.yaml` (LIBERO-Long-Pro: `libero_10_swap`, `libero_10_task`), `smoke.yaml`, `smoke_luna.yaml` (the smoke with GPT-5.6-Luna on the machine's ChatGPT login), `launch.sh` |

A `_task` suite changes the instruction and the goal, so a task's name no longer states it: the program reads the
language from `get_task_context()`. A `_swap` and a `_task` suite share their task ids; a row's id carries the suite.

## Setup and run

As [`aspire_libero/`](../aspire_libero/README.md#setup) (ASPIRE's LIBERO venv, its SAM3 and Contact-GraspNet
services), from this folder:

```bash
cd examples/aspire_libero_pro/aspire
export ASPIRE_ROOT=/path/to/ASPIRE FIREWORKS_API_KEY=...
ASPIRE_SIM_GPU=0 ASPIRE_CONFIG=smoke.yaml ./launch.sh        # two tasks, three seeds each
ASPIRE_SIM_GPU=0 ./launch.sh                                 # the eighty tasks
ASPIRE_SIM_GPU=0 ASPIRE_CONFIG=run_long.yaml ./launch.sh     # LIBERO-Long-Pro
./launch.sh eval --adapter final                             # the reruns with the final library
```
