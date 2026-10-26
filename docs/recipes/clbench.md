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

### ReHarness

The same streams also run with ReHarness (Xiao et al., [arXiv:2610.12668](https://arxiv.org/abs/2610.12668)): the
adapter is a bank of reflections, one per past instance, and before each question or hand the harness retrieves the
five closest by the embedding of the instance's prompt and appends them to the host's context; after it, the
Evolver revises the retrieved reflections and writes a new one. The learner is the run's `llm` tool on the host's
model (no Codex and no MetaWorld checkout; without one, `MW_KEY_FILE` names the key file), one step per instance
(`data.step_size: 1` in both venues). The launcher needs the key of the
embedding model's backend too (`MW_EMBED_MODEL`, default Qwen3-Embedding-8B on Fireworks) and a third port,
`WM_PORT+2`, for the embedding relay that the adapter's sandboxed reader reaches on the loopback.

```bash
MW_TAG=reharness-db WM_PORT=3481 MW_ORDERS="0" examples/clbench_database/reharness/launch.sh
MW_TAG=reharness-poker WM_PORT=3491 MW_ORDERS="0" examples/clbench_poker/reharness/launch.sh
```

The reported runs were made this way inside the offline container of [`docker/offline.sh`](https://github.com/Infini-AI-Lab/hermit/blob/main/docker/README.md),
whose only route out is Fireworks:

```bash
docker/offline.sh run MW_TAG=rh_db_flash WM_PORT=3481 MW_ORDERS="0 1 2 3 4" MW_RUNS=<runs> \
  examples/clbench_database/reharness/launch.sh
docker/offline.sh run MW_TAG=rh_poker_flash WM_PORT=3491 MW_ORDERS="0 1 2 3 4" MW_RUNS=<runs> \
  examples/clbench_poker/reharness/launch.sh
```

The protocol is the paper's: streaming. Each question or hand is answered with the bank as it stands, then the
Evolver reads the trajectory with the venue's verdict as its outcome (`feedback: true`, the Evolver's default: the
accuracy verdict, or the hand's net chips), so the stream is online-supervised, and the stream's own scores are the
result; nothing is evaluated afterwards. On the database venue the correct answer is also in the trajectory itself,
in the venue's observation after an answer, so `feedback: false` would not make that stream label-free.

Each run's `checkout/reflections/` is the bank after the stream, and `workspace/state/reharness.json` records what
every step retrieved, revised and wrote. The venue metrics are read as for MWM below.

Observed on the database venue with GLM 5.3 Flash at reasoning effort high (five orders of 40 questions, one run
each, in the offline container of `docker/offline.sh`): 36 to 42 minutes per order, about one minute per question,
of which the learner's five revision calls side by side and the reflection call after them take about 40 seconds; per order
177 to 215 host calls (1.4 to 1.9 M input tokens, three quarters of them cached, USD 0.10 to 0.13 at the backend's
prices), 231 to 235 learner calls (1.2 to 1.5 M input and 0.47 to 0.54 M output tokens, USD 0.41 to 0.48) and 79 to
80 embedding calls of about 18 K tokens in all. Accuracy 0.740 over the five orders
([the example's results](https://github.com/Infini-AI-Lab/hermit/blob/main/examples/clbench_database/README.md#results)).
On the poker venue (five orders of 104 instances, 120 hands each): 86 to 108 minutes per order, 242 to 341 host
calls (USD 0.23 to 0.35), 610 to 613 learner calls (USD 1.07 to 1.32) and 208 embedding calls; 0.28 bb/hand over the
five orders, below the stateless baseline
([the example's results](https://github.com/Infini-AI-Lab/hermit/blob/main/examples/clbench_poker/README.md#results)).

## On local GPUs

Any server the backends layer knows can stand in for Fireworks. On a machine of L40s (vLLM 0.31), the poker stream
ran with two instances of Qwen3.6-35B-A3B-FP8 (two GPUs each), one for the host and one for the learner, declared
once in a backends file:

```yaml
# lab-backends.yaml (HA_BACKENDS points here)
lab-host:    {from: vllm, base_url: http://127.0.0.1:8105/v1, api_key_env: LAB_HOST_KEY, context_window: 131072}
lab-learner: {from: vllm, base_url: http://127.0.0.1:8104/v1, api_key_env: LAB_LEARNER_KEY, context_window: 131072}
```

```bash
vllm serve Qwen/Qwen3.6-35B-A3B-FP8 --tensor-parallel-size 2 --language-model-only --max-model-len 131072 \
    --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder \
    --served-model-name qwen3.6-35b --port 8104            # and again on two other GPUs, --port 8105
HA_BACKENDS=lab-backends.yaml MW_HOST_MODEL=lab-host/qwen3.6-35b MW_EVOLVER_MODEL=lab-learner/qwen3.6-35b \
    MW_TAG=mwm-poker-local WM_PORT=3551 MW_ORDERS=0 examples/clbench_poker/mwm/launch.sh
```

What was observed on order 0 (2026-10-17), one run per method:

| Method | Hands | bb/hand | Note |
|---|---|---|---|
| MWM | 120 | +0.79 | all 22 learner sessions completed; 100 minutes |
| prompt-only | 20 | +1.65 | stopped at hand 21, see below |
| MWM, the same first 20 hands | 20 | +3.20 | |

These are one order with one local model, not a comparison with the results in the example's README.

- The host model matters more than the server. Qwen3-30B-A3B-Instruct-2507 looped as a host: it alternated two
  `wm` calls up to the 100-call cap of a hand (3 million input tokens for one hand), and as the database host it
  wrote its answer as text instead of calling the tool. Qwen3.6 did neither.
- A hand whose host takes no action fails twice and is never settled, and the stream cannot go past it: every later
  hand fails with `stream mismatch` and is scored 0 (`runner_logs/` shows it). The prompt-only run stopped this way
  at hand 21, after its learner's instructions told the host to reply with text such as `FOLD`.
- A reasoning model can run one answer past clbench's request timeout (`CLB_REQUEST_TIMEOUT`, 600 s); clbench
  retries it. The prompt-only host's first block took 27 minutes, most of it in two such answers, before its learner
  had written any instructions.

## Outputs and scoring

| Path under `$MW_RUNS/$MW_TAG/` | Meaning |
|---|---|
| `<tag>-o<order>/steps.jsonl` | Framework progress and scores for the order |
| `<tag>-o<order>/checkout/` | Selected adapter and card |
| `order<order>/run.log` | Training process output |
| `order<order>/task/evidence/` | Episode evidence |
| `order<order>/task/outcomes.jsonl` | Poker outcome ledger |
| `wire_ledger.json`, `meta_wire_ledger.json`, `embed_wire_ledger.json` (ReHarness) | Provider usage captured by the meters: the host's, the learner's and, for ReHarness, the embedding relay's (its calls carry no price; the card's calls of unknown cost are the unpriced rollouts plus the Evolver's own embedding calls, not the relay's) |

Database uses accuracy scoring with the configured query policy. Poker uses its outcome ledger and bb/hand;
do not interpret the framework evaluator's generic `solved_once` field as the poker metric.

The MWM example documentation reports roughly a day per order for its model setting, an observed planning estimate;
verify with a prefix run. The ReHarness figures above are observed runs, reported with their settings in the
examples' READMEs, and the local-GPU table is one observed order with one local model; no number on this page
comes from a smoke run.

## Reproduction limits

The ReHarness runs differ from the paper's CL-Bench evaluation ([arXiv:2610.12668](https://arxiv.org/abs/2610.12668),
section 4):

- Models: GLM 5.3 Flash at reasoning effort high acts, reflects and revises, instead of the paper's Claude Opus 4.8,
  GPT-5.5 and Gemini 3.7 Flash at high thinking effort (one backbone for every agent, as here).
- Tasks and metric: the database and poker venues, reported as raw accuracy or bb/hand over
  clbench's five orders with one run each, instead of the paper's six CL-Bench tasks reported as normalized gain over
  the stateless backbone, (r_method − r_stateless) / (r_max − r_stateless), averaged over three independently
  sampled stream rollouts with a shared seed. In that measure, over clbench's stateless baseline (0.400) with
  r_max = 1, the database accuracies are ReHarness 0.57, MWM 0.66, ICL 0.30; the paper's Table 5 reports 0.361 ±
  0.004 for its database task with Claude Opus 4.8, under its own host and metric, so the numbers are not comparable;
  on poker it reports only marginal gains, and here the method scores below the stateless baseline.
- Scoring and host: the fork's accuracy scoring with unlimited queries (`CLB_SCORING=accuracy`, `CLB_QUERY_BUDGET=0`,
  the `SCORING_UNLIMITED` contract the launcher checks for), and the venue's own host prompt and tools as the decision
  agent, with the retrieved reflections appended below `ADAPTER.md`
  ([where the integration departs from the paper](../methods/reharness.md#where-this-integration-departs-from-the-paper));
  the paper uses its own decision-agent prompt and each task's native metric. Ranking is by cosine, a choice the
  paper leaves open.
- Code: the authors publish none; the Evolver follows the paper's equations and Figure 5 prompts, verbatim.
- Revision of the tree: both runs predate the commit that brings ReHarness into this tree. The database runs were
  made from main's `1b0161e` with the ReHarness files still uncommitted (the launcher stamped the working tree
  `1b0161e-dirty`), before the Evolver read the agents' replies tolerantly and asked again with twice the budget for
  a reply cut off at `max_tokens` (3 of the 200 reflection writes were lost under the earlier reader). The poker runs
  were made from `9125458`, a commit of the branch's history before its rebase, not in this tree, with that reader;
  the committed code differs from it by the Evolver's `feedback` switch (whose default, true, is what the runs did),
  the rendering of Codex logs, which CL-Bench does not produce, and the host's handling of a failing reader. Both
  used clbench fork `2cc7eef`, recorded with the framework revision in each `order<i>/run.log`'s setting stamp.

Hosted models can change independently of the code, so the same commit need not give the same numbers.

## Preserve the protocol

Record stream order, model settings, benchmark and MetaWorld revisions, learner block size, and environment reset
behavior. Do not shuffle a stream or replace the benchmark protocol with a single final-adapter test.

The native integrations are described in `examples/clbench_database/README.md` and
`examples/clbench_poker/README.md`; the [MWM guide](../methods/mwm.md) covers the learner contract. Read the produced cards and ledgers for the actual run, including failures and
unknown usage.
