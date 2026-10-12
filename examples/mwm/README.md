# MWM

MWM (MetaWorld Model) keeps a world model of one environment: dated, scoped knowledge in `memory/`, worked examples,
a briefing every host reads, an open-questions docket, and tested programs and sub-agent specs hosts can call. Hosts
reach it through one `wm(name, args)` tool. After every block of episodes a learner (a Codex session) reads what the
hosts did and revises the model under a fixed contract.

`template: mwm` with `evolver.name: mwm` runs the native MetaWorld V2 default (docket on, hypothesis off): the
contract, agenda, nine skills, pattern library and maintenance are ported unchanged, and golden tests check the
rendered texts against a metaworld checkout (`examples/tasks/clbench/tests/test_parity_golden.py`).

## One step

One training step is one MWM block (`data.step_size` episodes, `data.shuffle: false`).

1. The Trainer runs the block's episodes through the task's pipeline. The task writes each episode's record into its
   evidence store in the native layout and returns the episode's entry (`RunOutput.extra["mwm"]`).
2. The Evolver runs maintenance on the adapter (journal roll, docket caps, gate advice, memory index) and renders
   `CONTRACT.md` with the venue's deployment texts (`task-info/deployment/`).
3. One Codex session runs with the native review prompt. It starts in the adapter directory, with the evidence store at
   `/evidence`, the venue's environment description at `/environment`, the skills and patterns at `/skills` and
   `/patterns`, and the venue's channels (for example `sql`) as its only tools. It runs in its own network
   namespace; only the model meter and the tool endpoint are forwarded, and every session measures that first.
4. A session that exits cleanly, took at least one action, and left the serving code alone is adopted. Every tool's own
   test then runs and failures are parked in `tools/_rejected/`. Otherwise the adapter is restored. A session that
   took no action is retried once.
5. The first step also runs the native bootstrap session first (the first block is served by the seed). When
   `block_size` is set, a shorter final block gets no session, as natively.

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

## Run

The hosts run in clbench's own environment with metaworld's host seat, so a CL-Bench run needs the clbench and
metaworld checkouts (`MWM_CLBENCH`, `MWM_METAWORLD`; default `~/code/...`) and a Fireworks key at
`<metaworld>/.fireworks`. `launch.sh` starts the two wire meters and one `harnessadapter train` per order:

```bash
python examples/tasks/clbench/database/prepare.py
MW_TAG=mwm_db WM_PORT=3401 MW_ORDERS="0 1 2 3 4" examples/mwm/clbench/launch.sh database
python examples/tasks/clbench/poker/prepare.py
MW_TAG=mwm_poker WM_PORT=3411 MW_ORDERS="0 1 2 3 4" examples/mwm/clbench/launch.sh poker
```

`MW_TRAIN_SPLIT=<file>` runs a prefix of one order's stream instead, for a smoke run.
