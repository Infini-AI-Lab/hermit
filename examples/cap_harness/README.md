# The cap-harness engine

The robot execution engine of [ASPIRE](https://github.com/NVlabs/ASPIRE)'s Code-as-Policy harness (`cap-harness`), shared
by the benchmarks that run on it: [Robosuite](../robosuite/) and [LIBERO](../libero/) (and, for its simulator
service, [RoboDojo-Sim](../robodojo_sim/)). A session writes a program (Python against the harness's perception,
grasping and motion API), runs it in the engine on its task's development seeds through `bin/sim`, and the judge
scores it once on held-out seeds. The engine is a benchmark's, not a method's: what a method brings to a session
(ASPIRE's fix loop, its search, its verifier: [`aspire`](../../docs/methods/aspire.md)) is the method's own, built on
the task functions here.

| Folder | What it is |
|---|---|
| `utils/agent/task.py` | The task functions every session gets: the workspace (`bin/sim`, the staged docs, the scene-exploration page and snapshot program) and its registration with the simulator service, the facts the judge reads (`collect`: `<task_dir>/fix_code.py` and the unseen check) |
| `utils/agent/sim_service.py` | The simulator service on the host: ASPIRE's `cap-harness` for the sandboxed sessions, holding their seed rules and a row's attempt budget (`max_programs`) |
| `utils/agent/stage_docs.py` | ASPIRE's docs as the sessions may read them, staged outside its checkout by `launch.sh` |
| `utils/agent/sim_cli.py` | `bin/sim`: a session's client of the simulator service |
| `utils/agent/sim.py` | The robot execution engine from the harness's side (the held-out run, a direct actor's runs) and the trace digest |
| `utils/agent/fields.py` | A task directory read back as its row: every config's `data.view` |
| `utils/benchmark/tasks.py` | What a benchmark's `prepare.py` writes with: its tasks and their reruns as task directories in Harbor's format, its splits, the seed presets' checks |
| `utils/benchmark/judge.py` | On the host: the scored program on the row's held-out seeds, its success rate |
| `utils/seg_service/` | A SAM3-wire-compatible segmentation service from ungated models (optional) |
| `launch.sh` | Started by a benchmark's method folder: writes the seeds into the tasks, runs the method folder's `tasks.sh` if it has one, stages the docs, starts the simulator service and runs hermit |

## Setup

1. **ASPIRE.** ASPIRE runs in its own venvs and is never imported into hermit's process (as CL-Bench's runner is
   not), so it is not a dependency group of hermit's `pyproject.toml`: the engine pins its commit here and
   `launch.sh` names the one a run used. Clone [NVlabs/ASPIRE](https://github.com/NVlabs/ASPIRE) and bootstrap its Robosuite venv and the
   providers its programs call (its `docs/host-setup.md`, `docs/robosuite.md`):

   ```bash
   git clone https://github.com/NVlabs/ASPIRE && cd ASPIRE
   git checkout fa00f0ef2bc1d91d594b9aa502c22133c9f538f7   # the commit this example was written against
   git submodule update --init third_party/robosuite third_party/curobo
   scripts/bootstrap_robosuite.sh
   scripts/bootstrap_pyroki.sh                 # IK, CPU
   scripts/bootstrap_curobo.sh                 # collision-aware planning, GPU
   scripts/bootstrap_sam3.sh                   # text segmentation, GPU; the weights are gated on Hugging Face
   ```

2. **The services** on the ports ASPIRE's clients expect: segmentation on 8114, PyRoki on 8116, cuRobo on 8118
   (ASPIRE's `scripts/supervise_services.sh --providers sam3,pyroki,curobo`). The segmentation service may be any
   server that speaks the SAM3 wire format (`POST /segment`, `POST /segment_point`, `GET /openapi.json`; point
   `CAP_HARNESS_SAM3_URL` at it): see [Segmentation without SAM3](#segmentation-without-sam3).

3. **The simulator's render device.** MuJoCo renders offscreen with EGL. Every cap-harness process runs in a
   sandbox of its own (see [A session's rules](#a-sessions-rules)), which shows only the GPUs `ASPIRE_SIM_GPU`
   names: the N of `/dev/nvidia<N>` (`nvidia-smi -q` shows it as a GPU's Minor Number), e.g. `ASPIRE_SIM_GPU=8`.
   Without it the simulator renders in software (Mesa's llvmpipe, which the host must have), slowly. A hand-extracted
   NVIDIA EGL library (`__EGL_VENDOR_LIBRARY_FILENAMES`) is shown to it; other paths it needs go in
   `ASPIRE_SIM_READ` (`:`-separated). Put the rest of the simulator's environment in a YAML map and pass it as
   `ASPIRE_SIM_ENV`:

   ```yaml
   MUJOCO_GL: egl
   PYOPENGL_PLATFORM: egl
   EGL_PLATFORM: device
   MUJOCO_EGL_DEVICE_ID: "0"      # inside the sandbox the GPUs ASPIRE_SIM_GPU names count from 0
   ```

4. **Codex and the sandboxes.** A session (and a method's learner, such as ASPIRE's coordinator) is a Codex CLI
   session (`codex` on `PATH`; the configs bind them to GLM 5.3 Flash on Fireworks), each in the framework's sandbox (`sandbox.enabled: true`, `enforce:
   required`; bubblewrap on `PATH`, docs/guides/sandbox.md). The task sandbox has no GPU, so the simulator runs on the
   host as a service (`utils/agent/sim_service.py`, started by `launch.sh`), which the session reaches through `bin/sim`
   on a forwarded loopback port; see [A session's rules](#a-sessions-rules).

## Disk

A session runs the simulator hundreds of times (ASPIRE's: a fix loop of up to 8 programs on 25 seeds, an evolutionary
search of up to 40 more), and each run keeps its record, trace and images. `collect` deletes the images
(`media/`) when the session ends (`agent.params.prune_media: false` keeps them); the rest still adds
up over a campaign. Set `ASPIRE_OUT` to a large disk: `launch.sh` puts the runs and the workspaces there.


## A session's rules

A session runs in the task sandbox, which shows it only what the config declares (`environment.read`): the engine's
code (`utils/agent/`), of the ASPIRE checkout the snapshot program it is given (and a method's prompt templates, which
its configs declare), and ASPIRE's docs as `launch.sh` stages them outside the checkout (`utils/agent/stage_docs.py`:
without the lines, and for LIBERO the section, that name ASPIRE's worked programs; the checkout's own pages are not
shown). ASPIRE's shipped skill library, its worked solutions and examples, other sessions' workspaces, the task rows
and the judge do not exist inside; its network reaches only its model and the simulator service's forwarded port. The
simulator service is the trusted side of the seed rules: it reads the task rows on the host, `prepare` registers the
session (its workspace and task id; one session per rollout directory, which the sandbox cannot create) before the
agent starts, and the service runs only the row's development and unseen-check seeds, the unseen check once and, for
a row with an attempt budget (`max_programs`), that many programs, writing the runs into the workspace. The held-out
seeds are run only by the judge, on the host (ASPIRE's Stage 2), on the program the session selected. The session's
Codex runs with its own sandbox off (`codex_sandbox: danger-full-access`), the task sandbox being the boundary.

The programs a session writes run inside cap-harness on the host, and ASPIRE's program validator is no boundary (a
format string's attribute traversal passes it). So every cap-harness process, the service's, the judge's and the
direct actor's, runs in a bubblewrap sandbox of its own (`utils/agent/sim.py`: `confine`): it reads ASPIRE's
checkout but not its worked solutions, agents' skills or tests (`examples/`, `.claude/`, `.agents/`, `.codex/`,
`tests/`) nor run storage inside it, writes only its run's own output, reaches only the providers (their loopback
ports; one a `CAP_HARNESS_*_URL` puts on another host through an allowlist proxy for that host), and sees only the
GPUs `ASPIRE_SIM_GPU` names (hermit's `Sandbox(gpu=...)`). A program that escapes the validator reaches no other
session, none of the judge's runs and no host file, and what it leaves in its output is read without following a
link. It still runs in cap-harness's own process, so it could write its own run's outcome: the score of such a
program is not trustworthy, and the boundary is that it can affect nothing else. This sandbox is the engine's own:
it is on whatever the run's `sandbox.enabled` says, so the simulator always needs bubblewrap (the service refuses
to start without it, or with an `ASPIRE_SIM_GPU` the machine lacks). Each run gets a HOME of its own, removed
after it; the host's `CUDA_VISIBLE_DEVICES` is not passed in (inside, the GPUs count from 0); each confined run
leaves its sandbox record under `$ASPIRE_OUT/sim-service/sandbox/` (the service's) or `$ASPIRE_OUT/sandbox/` (the
judge's).


## Segmentation without SAM3

ASPIRE's programs segment with SAM3 (`facebook/sam3`, gated). Any service with the SAM3 wire format can stand in.
A stand-in built from ungated models (Grounding-DINO for the text prompt's boxes, SAM for the masks) is enough for
the Robosuite scenes; it is a different perception stack from the paper's, and results with it are reported as
such. `utils/seg_service/` is one:

```bash
python -m venv .venv-seg && .venv-seg/bin/pip install -r utils/seg_service/requirements.txt
CUDA_VISIBLE_DEVICES=0 .venv-seg/bin/python utils/seg_service/service.py --port 8114
```

The models (`IDEA-Research/grounding-dino-base`, `facebook/sam-vit-huge`; `SEG_DINO_MODEL`, `SEG_SAM_MODEL`) are
downloaded on first start.


## Checking the render device

Inside the simulator's sandbox EGL finds only the GPUs `ASPIRE_SIM_GPU` names, so `MUJOCO_EGL_DEVICE_ID: "0"` is the
first of them. Confirm with `nvidia-smi` that the render process lands on the GPU you intended; a run whose
keyframes take seconds each is rendering in software (no `ASPIRE_SIM_GPU`, or a GPU the sandbox could not
use). A host whose libEGL finds only Mesa's driver (`failed to open /dev/dri/cardN`) needs NVIDIA's EGL
vendor library (`libEGL_nvidia.so.0`, the driver's `libnvidia-gl` package) and
`__EGL_VENDOR_LIBRARY_FILENAMES` naming its ICD file.


## Known host issues (Blackwell B300, sm_103)

Found while bringing the engine up on B300 nodes; none of it is in the engine's code path, which takes the
simulator's environment and the service URLs from the host.

- **NVRTC 12.8 rejects sm_103.** The cu128 PyTorch wheels' runtime compiler fails on B300
  (`nvrtc: error: invalid value for --gpu-architecture`) in kernels compiled at run time (PyTorch's jiterator, cuRobo's
  `cuda.core` kernels). Use the cu130 wheels for a service that can (the segmentation service), and for cuRobo compile
  its kernels for a binary-compatible architecture (sm_100) until its venv carries NVRTC 12.9 or newer.
- **No NVIDIA EGL vendor library on the host.** libEGL then loads Mesa and fails on `/dev/dri`. Extract
  `libnvidia-gl-<driver>` of the installed driver's version and point `__EGL_VENDOR_LIBRARY_FILENAMES` at its ICD.
- **EGL's device order is not nvidia-smi's.** Map each EGL device to its GPU by UUID (`EGL_DEVICE_UUID_EXT`) or CUDA
  ordinal before setting `MUJOCO_EGL_DEVICE_ID`. Robosuite asserts that `MUJOCO_EGL_DEVICE_ID` appears in
  `CUDA_VISIBLE_DEVICES`; the simulator needs no CUDA, so give it `CUDA_VISIBLE_DEVICES=""`.
- **Defaults that pick a GPU.** ASPIRE's provider profiles place services on GPU 0 and `bootstrap_curobo.sh`
  verifies on GPU 3 (`CAP_HARNESS_CUROBO_GPU`); on a shared node set both explicitly.
- **Contact-GraspNet** is not needed by the Robosuite programs; without its service `generate_grasps` reports
  `provider_unavailable` and programs fall back to geometric grasps.
