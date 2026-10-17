# CL-Bench environment streams

This integration runs database exploration and poker streams using CL-Bench's environment and MetaWorld's host
seat. MWM maintains environment knowledge between episode blocks. These are environment integrations, not
standalone JSONL benchmarks that can be reproduced by installing the framework alone.

## Requirements and configuration

Use a Linux machine with the configured Codex learner, bubblewrap/namespace support, and the framework's
dependencies. Provide CL-Bench and MetaWorld checkouts with their venue-specific dependencies installed.
The models are backend references (default GLM 5.3 Flash on Fireworks); the launcher needs the backend's key
variable (`FIREWORKS_API_KEY`, else the key file `<MWM_METAWORLD>/.fireworks`). `MW_MODEL=<backend>/<model id>`
binds host and learner to another backend, `MW_HOST_MODEL` and `MW_EVOLVER_MODEL` either alone: for a local
server, `MW_MODEL=vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 VLLM_BASE_URL=http://127.0.0.1:8000/v1`
([Model backends](../guides/model-backends.md)).

Set paths explicitly rather than relying on the launcher's workstation defaults:

```bash
export MWM_CLBENCH=/absolute/path/to/clbench
export MWM_METAWORLD=/absolute/path/to/metaworld
export MW_FRAMEWORK_PYTHON="$PWD/.venv/bin/python"
export MW_RUNS="$PWD/.demo/clbench-runs"
```

The CL-Bench host environment also uses the benchmark checkout's `.venv/bin/python`. For database, the launcher
checks that the benchmark prompt contains the accuracy-scoring contract (`SCORING_UNLIMITED`). Inspect
`examples/clbench_database/utils/task-info/` and the task runners before starting; a vanilla checkout missing that
contract is insufficient.

## Prepare and launch

From the framework repository root:

```bash
python examples/clbench_database/utils/prepare.py --clbench "$MWM_CLBENCH"
MW_TAG=mwm-db WM_PORT=3401 MW_ORDERS="0" examples/clbench_database/mwm/launch.sh
```

For poker, use its own run tag and free port pair:

```bash
python examples/clbench_poker/utils/prepare.py --clbench "$MWM_CLBENCH"
MW_TAG=mwm-poker WM_PORT=3411 MW_ORDERS="0" examples/clbench_poker/mwm/launch.sh
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

The native integrations are described in `examples/clbench_database/README.md` and
`examples/clbench_poker/README.md`; the [MWM guide](../methods/mwm.md) covers the learner contract. Read the produced cards and ledgers for the actual run, including failures and
unknown usage.
