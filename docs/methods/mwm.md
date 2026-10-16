# MWM

MWM (MetaWorld Model) keeps a world model of one environment: dated, scoped knowledge in `memory/`, worked examples,
a briefing every host reads, an open-questions docket, and tested programs and sub-agent specs hosts can call. Hosts
reach it through one `wm(name, args)` tool. After every block of episodes a learner (a Codex session) reads what the
hosts did and revises the model under a fixed contract.

`template: mwm` with `evolver.name: mwm` runs the native MetaWorld V2 default (docket on, hypothesis off): the
contract, agenda, nine skills, pattern library and maintenance are ported unchanged, and golden tests check the
rendered texts against a metaworld checkout (`examples/clbench_database/utils/tests/test_parity_golden.py`).

## One step

One training step is one MWM block (`data.step_size` episodes, `data.shuffle: false`).

1. The Trainer runs the block's episodes through the task's pipeline. The task writes each episode's record into its
   evidence store in the native layout and returns the episode's entry (`RunOutput.extra["mwm"]`).
2. The Evolver runs maintenance on the adapter (journal roll, docket caps, gate advice, memory index) and renders
   `CONTRACT.md` with the venue's texts (`evolver.params.deployment`, default the task's `task-info/mwm/`).
3. One Codex session runs with the native review prompt. It starts in the adapter directory, with the evidence store at
   `/evidence`, the venue's environment description at `/environment`, the skills and patterns at `/skills` and
   `/patterns`, and the venue's channels (for example `sql`) as its only tools. It runs in its own network
   namespace; only the model meter and the tool endpoint are forwarded, and every session measures that first.
4. A session that exits cleanly, took at least one action, and left the serving code alone is adopted. Every tool's own
   test then runs and failures are parked in `tools/_rejected/`. Otherwise the adapter is restored. A session that
   took no action is retried once.
5. The first step also runs the native bootstrap session first (the first block is served by the seed). When
   `block_size` is set, a shorter final block gets no session, as natively.

## What a task prepares for MWM

Besides the task's data, agent and judge, MWM reads a few texts about the venue. They live in the task's
`utils/task-info/mwm/` (the Evolver's `deployment` parameter can point elsewhere); everything the
method fixes itself has a default in `harnessadapter/algorithms/evolvers/mwm/deployment.py`, so a venue writes only
what is its own:

| File | What it says | Who reads it |
|---|---|---|
| `contract/domain_what.md` | what the environment is and how it is scored | the learner's contract |
| `contract/env_evidence.md` | what one evidence record holds and how records are addressed | the learner's contract and `/evidence/README.md` |
| `contract/env_verification.md` | how the environment grades and what its feedback says | the learner's contract |
| `contract/env_consumption.md` | how the host reaches the world model beside the environment's tools | the learner's contract |
| `contract/env_executable.md` | what a learned tool may and may not do in this environment | the learner's contract |
| `delegation` | `managed`, `native` or `none`: whether a host may start a sub-agent | the contract's sub-agent sentence |
| `host.json` | `transport_note`: the one sentence telling the host how wm reaches it | the host's package (ADAPTER.md) |
| `environment/README.md`, `environment/bin/`, `environment/docs/` | what the learner's `/environment` holds: the channels of this run and the documents a host can retrieve | the learner's session |

Every text is information only: facts a new task could state the same way, no experience, no instruction, no
evaluation design. The method's own parts (the directories the learner writes, the mounts it reads, the wm call
sentence, the libraries a `wm run` program may import) are not repeated per task.

The acting side needs two of these too: the host binds `deployment.host_settings(task-info/mwm)` as the adapter's
`deployment` resource, and the evidence store's README is written from `contract/env_evidence.md`
(`python -m harnessadapter.algorithms.evolvers.mwm.evidence.readme`). The learner's evidence records themselves
are written by the method's venue modules in `harnessadapter/algorithms/evolvers/mwm/evidence/`; a new venue adds
one module there.

## Knobs

| Setting | Meaning |
|---|---|
| `data.step_size` | Episodes per block (`learn_every`) |
| `evolver.params.model`, `args` | The learner model and the Codex provider settings (reasoning effort, the meter URL) |
| `evolver.params.evidence` | The task's evidence store; also listed in `evolver_access.environment` |
| `evolver.params.block_size` | `= data.step_size`, so that a shorter final block gets no session |
| `evolver.params.session_timeout_s` | Per session (default 1800) |
| `evolver.params.network_isolation` | The learner's own network namespace (default true) |
| `evolver_access.tools` | The venue's channels the learner may call |

## Examples

| Config | Task | Notes |
|---|---|---|
| `clbench/database.yaml` | CL-Bench database: 40 questions per order, one block per question | accuracy scoring, unlimited host queries, `sql` channel for the learner |
| `clbench/poker.yaml` | CL-Bench poker: 120 hands per order (104 reach the host), one block per 5 host hands | bb/hand from `outcomes.jsonl` |
| `servelearnbench/retail_l3.yaml` | ServeLearnBench retail_l3: 624 serving cases in 8 windows, one block per 12 cases, the benchmark's own agent loop | per-window tests by `windows.py`; scored with `slb score` (README) |

## Run

The hosts run in clbench's own environment with metaworld's host seat, so a CL-Bench run needs the clbench and
metaworld checkouts (`MWM_CLBENCH`, `MWM_METAWORLD`; default `~/code/...`) and a Fireworks key at
`<metaworld>/.fireworks`. `launch.sh` starts the two wire meters (`harnessadapter/runtime/wire_meter.py`) and one `harnessadapter train` per order:

```bash
python examples/clbench_database/utils/prepare.py
MW_TAG=mwm_db WM_PORT=3401 MW_ORDERS="0 1 2 3 4" examples/clbench_database/mwm/launch.sh
python examples/clbench_poker/utils/prepare.py
MW_TAG=mwm_poker WM_PORT=3411 MW_ORDERS="0 1 2 3 4" examples/clbench_poker/mwm/launch.sh
```

`MW_TRAIN_SPLIT=<file>` runs a prefix of one order's stream instead, for a smoke run.
