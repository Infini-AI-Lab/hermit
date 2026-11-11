# ASPIRE on LIBERO

LIBERO's five suites ([Liu et al., 2023](https://arxiv.org/abs/2306.03310)): Object, Spatial, Goal, Long
(`libero_10`) and LIBERO-90, as tasks of [ASPIRE](https://research.nvidia.com/labs/gear/aspire/)'s Code-as-Policy
harness ([code](https://github.com/NVlabs/ASPIRE): `cap-harness --benchmark libero-pro`, whose pinned LIBERO
registers them), run as [`aspire_robosuite/`](../aspire_robosuite/) runs Robosuite: the same actor, verifier,
coordinator, simulator service, judge and sandboxes, which that example's README describes. This folder holds what
is LIBERO's:

| Folder | What it is |
|---|---|
| `utils/prepare.py` | The 130 tasks (a suite and a task id each) with their seeds; the verifier's rows; the reruns |
| `utils/task-info/` | What a task and its record are, for the coordinator |
| `aspire/` | `run.yaml` (Object, Spatial, Goal, Long: 40 tasks), `run_90.yaml` (LIBERO-90), `smoke.yaml`, `smoke_luna.yaml` (the smoke with GPT-5.6-Luna on the machine's ChatGPT login), `launch.sh` |

The engine (`../aspire_robosuite/utils/agent/`, `../aspire_robosuite/utils/judge.py`) takes the benchmark from the
task row (`"benchmark": "libero-pro"`) and with it ASPIRE's settings for LIBERO (its fix-loop template's Benchmark
Profiles):

- `.venv-libero/bin/cap-harness`, with `LIBERO_CONFIG_PATH=$ASPIRE_ROOT/.libero` and
  `TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1`;
- `--init-mode seeded` on every run, the actor's and the judge's, so that each seed is a scene of its own (the
  default restores one of a task's 50 saved states, and seeds past 50 would repeat them);
- ASPIRE's LIBERO seeds: development 51-65, unseen-check 66-80, held-out 1-50, the verifier's 51-55;
- `docs/libero-pro.md` in the actor's workspace without its "Task examples" section, which names ASPIRE's worked
  programs per task (one of them a LIBERO-90 task). The task sandbox is shown the staged copy only, not the
  checkout's page; a page without that section is refused rather than passed whole.

ASPIRE's own LIBERO results are on LIBERO-Pro's perturbed suites (`libero_*_swap`, `libero_*_task`), not on these.

## Setup

As [Robosuite's](../aspire_robosuite/README.md#setup) (the providers, the render device, Codex and the sandboxes),
with LIBERO's venv instead of Robosuite's in the ASPIRE checkout:

```bash
cd ASPIRE && git checkout fa00f0ef2bc1d91d594b9aa502c22133c9f538f7
scripts/bootstrap_libero.sh                 # .venv-libero, third_party/LIBERO-PRO, .libero/
scripts/bootstrap_graspnet.sh               # .venv-graspnet: Contact-GraspNet, LIBERO's default grasp backend
```

Serve Contact-GraspNet on 8115 beside the other three (`scripts/supervise_services.sh --providers graspnet`). The
launcher does not check that port: without the service `generate_grasps` has no backend to call.

## Run

```bash
cd examples/aspire_libero/aspire
export ASPIRE_ROOT=/path/to/ASPIRE FIREWORKS_API_KEY=...
ASPIRE_SIM_GPU=0 ASPIRE_CONFIG=smoke.yaml ./launch.sh        # two tasks, three seeds each
ASPIRE_SIM_GPU=0 ./launch.sh                                 # Object, Spatial, Goal, Long
ASPIRE_SIM_GPU=0 ASPIRE_CONFIG=run_90.yaml ./launch.sh       # LIBERO-90
./launch.sh eval --adapter final                             # the reruns with the final library
```

`launch.sh` starts the Robosuite example's launcher on this folder; its variables (`ASPIRE_SIM_ENV`,
`ASPIRE_SIM_GPU`, `ASPIRE_OUT`, `ASPIRE_EVAL_SEEDS`, ...) are described there.
