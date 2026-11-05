# ASPIRE on Robosuite

The Robosuite tasks of [ASPIRE](https://research.nvidia.com/labs/gear/aspire/) (Agentic Skills Discovery for
Robotics, [arXiv:2607.00272](https://arxiv.org/abs/2607.00272), [code](https://github.com/NVlabs/ASPIRE)) as a
continual-learning curriculum, run as ASPIRE runs it: ASPIRE's fix-loop subagent (the actor), its skill verifier
and its coordinator, each a Codex session given ASPIRE's own prompt. For each task the actor writes a Code-as-Policy
program (Python against ASPIRE's perception, grasping and motion API), runs it in ASPIRE's robot execution engine on
development seeds, debugs it from the per-primitive traces and RGB keyframes, and checks it once on unseen seeds; the
harness then scores it once on held-out seeds. After each task the coordinator (the
[`aspire`](../../docs/methods/aspire.md) Evolver) updates the shared skill library, admitting the actor's proposals
only if a fresh verifier re-solves the task from the library and the proposals alone, and chooses the next task.

| Folder | What it is |
|---|---|
| `utils/task.py` | The Codex actor's task functions: the workspace (`bin/sim`, docs, the snapshot program), the prompt (an adaptation page, the assignment, ASPIRE's template verbatim), the held-out run (`collect`) |
| `utils/agent/sim_cli.py` | `bin/sim`: ASPIRE's `cap-harness` for the task, held to the assigned seeds and the workspace |
| `utils/agent/sim.py` | The robot execution engine from the harness's side (the held-out run) and the trace digest |
| `utils/prompts/` | The pages that map ASPIRE's fix-loop, verifier and evolutionary-search templates onto the workspace |
| `utils/agent/agent.py` | The structured direct-chat actor of `aspire/run_direct.yaml` (a comparison, not the method) |
| `utils/prepare.py` | The seven Robosuite tasks; the verifier's rows; the reruns with the final library |
| `utils/seg_service/` | A SAM3-wire-compatible segmentation service from ungated models (optional) |
| `utils/judge.py` | The success rate on the seeds a program is scored on (held-out; the verifier's own) |
| `aspire/` | `run.yaml` (ASPIRE's seeds), `smoke.yaml`, the `*_direct.yaml` comparison, `launch.sh` |

## Setup

1. **ASPIRE.** ASPIRE runs in its own venvs and is never imported into hermit's process (as CL-Bench's runner is
   not), so it is not a dependency group of hermit's `pyproject.toml`: the example pins its commit here and
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

3. **The simulator's render device.** MuJoCo renders offscreen with EGL, and EGL device enumeration ignores
   `CUDA_VISIBLE_DEVICES`. Put the simulator's environment in a YAML map and pass it as `ASPIRE_SIM_ENV`:

   ```yaml
   MUJOCO_GL: egl
   PYOPENGL_PLATFORM: egl
   EGL_PLATFORM: device
   MUJOCO_EGL_DEVICE_ID: "0"      # the EGL device of the GPU you render on; check it, see below
   ```

4. **Codex.** The actor and the coordinator are Codex CLI sessions (`codex` on `PATH`; the configs bind them to GLM
   5.3 Flash on Fireworks). The actor's commands drive a GPU simulator and local services, so it runs without a
   container and with Codex's own sandbox off (`isolation: none`, `codex_sandbox: danger-full-access`); its seed and
   directory rules are held by `bin/sim` and its prompt.

5. **Run** on the host that serves the providers:

   ```bash
   export FIREWORKS_API_KEY=...
   cd examples/aspire_robosuite/aspire
   ASPIRE_ROOT=/path/to/ASPIRE ASPIRE_SIM_ENV=/path/to/sim_env.yaml ASPIRE_CONFIG=smoke.yaml ./launch.sh
   ASPIRE_ROOT=/path/to/ASPIRE ASPIRE_SIM_ENV=/path/to/sim_env.yaml ASPIRE_CONFIG=smoke.yaml ./launch.sh eval --adapter final
   ```

   `./launch.sh eval --adapter final` reruns every task with the final library (the test split's `rerun-*` rows):
   ASPIRE's backward transfer, compared per task with the task's first pass during training (each training step's
   score in the run's `steps.jsonl`; the rerun's in the eval's results). `--adapter root` is the empty library and
   `--adapter none` no library at all: the baselines on the same seeds.

   The protocol is ASPIRE's: each task learns on its development seeds and is scored once on held-out seeds; the
   reruns score the same held-out seeds again, so the comparison reuses that partition (as ASPIRE's Phase 3 does).
   The first evaluation ran `run.yaml` with `agent.params.eval_seeds=1-50` (half of ASPIRE's 1-100), as
   `./launch.sh train agent.params.eval_seeds=1-50`, and its reruns with the same override.

## Protocol

| | `run.yaml` | `smoke.yaml` |
|---|---|---|
| Tasks | the 7 Robosuite tasks, in the order the coordinator chooses (it starts from `splits/all.txt`'s first) | `cube_lifting`, `cube_stack` |
| Development seeds | 101-125 (ASPIRE's) | 101-103 |
| Unseen-check seeds | 126-130 (ASPIRE's), run once by the actor | 126 |
| Held-out seeds | 1-100 (ASPIRE's), run once by the harness | 1-3 |
| Fix loop | ASPIRE's: 3 versions per failure mode, 8 in total | the same |
| Evolutionary search | ASPIRE's, for a task the fix loop leaves below 90%: K = 8, top 3, stop at 80% or 5 rounds | the same |
| Verifier | ASPIRE's: 5 seeds (101-105), 2 attempts, verified at ≥ 4/5 | the same |
| Actor, verifier, coordinator | Codex, GLM 5.3 Flash (Fireworks), reasoning effort medium | the same |

## The actor's rules

The actor's commands run without a sandbox, because they drive a GPU simulator and local services. Its rules
(ASPIRE's reading rules: no worked solution, no shipped library, no other task's directory; only its own seeds) are
held three ways: the workspace holds only what it may read (the docs it is allowed, with the lines that name worked
programs dropped; no repository path in its environment); `bin/sim` runs only its assigned seeds, the unseen check
once, and a verifier's two programs; and `collect` audits the session after the fact (`utils/task.py:audit`: the
commands it ran and every run's seed), voiding a session that broke them (score 0, its report not used). The
audit is detection, not isolation: `bin/sim.json` names the ASPIRE checkout, and a determined session could reach
it; it would be caught only by the commands it ran.

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

EGL's device order need not match `nvidia-smi`'s. Query each EGL device's CUDA device with `EGL_CUDA_DEVICE_NV`
(the NVIDIA EGL vendor library answers it) and confirm with `nvidia-smi` that the render process lands on the GPU
you intended. A host whose libEGL finds only Mesa's driver (`failed to open /dev/dri/cardN`) needs NVIDIA's EGL
vendor library (`libEGL_nvidia.so.0`, the driver's `libnvidia-gl` package) and
`__EGL_VENDOR_LIBRARY_FILENAMES` naming its ICD file.

## Known host issues (Blackwell B300, sm_103)

Found while bringing the engine up on B300 nodes; none of it is in the example's code path, which takes the
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
