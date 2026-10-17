# CL-Bench environment streams

This integration runs database exploration and poker streams using CL-Bench's environment and MetaWorld's host
seat. MWM maintains environment knowledge between episode blocks. These are environment integrations, not
standalone JSONL benchmarks that can be reproduced by installing the framework alone.

## Requirements and configuration

Use a Linux machine with the configured Codex learner, bubblewrap/namespace support, and the framework's
dependencies. Provide CL-Bench and MetaWorld checkouts with their venue-specific dependencies installed.
The launcher reads the Fireworks credential from `<MWM_METAWORLD>/.fireworks`.

Set paths explicitly rather than relying on the launcher's workstation defaults:

```bash
export MWM_CLBENCH=/absolute/path/to/clbench
export MWM_METAWORLD=/absolute/path/to/metaworld
export MW_FRAMEWORK_PYTHON="$PWD/.venv/bin/python"
export MW_RUNS="$PWD/.demo/clbench-runs"
```

The CL-Bench host environment also uses the benchmark checkout's `.venv/bin/python`. For database, the launcher
checks that the benchmark prompt contains the accuracy-scoring contract (`SCORING_UNLIMITED`). Inspect
`examples/tasks/clbench/database/task-info/` and the task runners before starting; a vanilla checkout missing that
contract is insufficient.

## Prepare and launch

From the framework repository root:

```bash
python examples/tasks/clbench/database/prepare.py --clbench "$MWM_CLBENCH"
MW_TAG=mwm-db WM_PORT=3401 MW_ORDERS="0" examples/mwm/clbench/launch.sh database
```

For poker, use its own run tag and free port pair:

```bash
python examples/tasks/clbench/poker/prepare.py --clbench "$MWM_CLBENCH"
MW_TAG=mwm-poker WM_PORT=3411 MW_ORDERS="0" examples/mwm/clbench/launch.sh poker
```

These commands make real model calls. Begin with one order. `MW_ORDERS="0 1 2 3 4"` selects all five.
`MW_TRAIN_SPLIT` can select a prepared prefix split for a smoke run; a prefix is not the full experiment.
The launcher uses `WM_PORT` and `WM_PORT+1` for host and learner wire meters.

## Outputs and scoring

| Path under `$MW_RUNS/$MW_TAG/` | Meaning |
|---|---|
| `<tag>-o<order>/steps.jsonl` | Framework progress and scores for the order |
| `<tag>-o<order>/checkout/` | Selected adapter and card |
| `order<order>/run.log` | Training process output |
| `order<order>/task/evidence/` | Episode evidence |
| `order<order>/task/outcomes.jsonl` | Poker outcome ledger |
| `wire_ledger.json`, `meta_wire_ledger.json` | Provider usage captured by the meters |

Database uses accuracy scoring with the configured query policy. Poker uses its outcome ledger and bb/hand;
do not interpret the framework evaluator's generic `solved_once` field as the poker metric.

The example documentation reports roughly a day per order for its model setting. This is an observed planning
estimate; verify with a prefix run. No fresh benchmark measurements are implied by this guide.

## Preserve the protocol

Record stream order, model settings, benchmark and MetaWorld revisions, learner block size, and environment reset
behavior. Do not shuffle a stream or replace the benchmark protocol with a single final-adapter test.

The full native integration and block lifecycle are described in `examples/mwm/README.md` and the
[MWM guide](../methods/mwm.md). Read the produced cards and ledgers for the actual run, including failures and
unknown usage.
