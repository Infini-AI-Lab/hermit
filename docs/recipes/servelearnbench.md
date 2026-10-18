# ServeLearnBench with MWM

Train on an ordered serving stream, then test each environment window using its corresponding adapter checkpoint.
The integration uses the benchmark's own acting loop, budgets, tools, and verifier.

The included retail_l3 configuration has 624 serving cases in eight windows. Its example model setting uses
GLM 5.3 Flash for acting and learning. An earlier example run took about six hours; this is a planning observation,
not a guaranteed runtime or cost.

## Requirements

Use Linux with the framework installed, Codex on PATH, bubblewrap and permitted namespaces, and the key of the
models' backend (`FIREWORKS_API_KEY` for the default, GLM 5.3 Flash on Fireworks). Install the benchmark into the
interpreter used by the launcher:

```bash
.venv/bin/python -m pip install "git+https://github.com/Infini-AI-Lab/ServeLearnBench"
```

The launcher uses Bash, `setsid`, `curl`, and local wire-meter processes. It is not the macOS offline walkthrough.
Inspect `examples/servelearnbench/mwm/retail_l3.yaml` before making model calls.

## Set paths and launch

Run from the repository root with the credential already set:

```bash
export MW_FRAMEWORK_PYTHON="$PWD/.venv/bin/python"
export MW_RUNS="$PWD/.demo/slb-runs"
export MW_TAG=slb-retail-demo
export WM_PORT=3451
examples/servelearnbench/mwm/launch.sh retail_l3
```

The launcher prepares the scenario's task export if needed, starts training in the background, and launches
a waiter that runs the per-window tests after training exits. Inspect logs for success; a waiter starting tests
does not itself establish successful training.

Use an unused tag and a free port pair (`WM_PORT` and `WM_PORT+1`). Existing run/task directories are refused.
The models are backend references: `MW_MODEL=<backend>/<model id>` for host and learner, `MW_HOST_MODEL` and
`MW_EVOLVER_MODEL` for either alone (a local server: `MW_MODEL=vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8
VLLM_BASE_URL=http://127.0.0.1:8000/v1`; see [Model backends](../guides/model-backends.md)). The default output
root is `~/code/metaworld-runs`; setting `MW_RUNS` above makes it explicit.

## Inspect outputs

| Path under `$MW_RUNS/$MW_TAG/` | Contents |
|---|---|
| `run.log` | Setting stamp and training output |
| `<tag>/steps.jsonl` | One framework record per block |
| `<tag>/checkout/` | Adapter and final card |
| `task/evidence/` | Serving episode evidence |
| `test.log` | Per-window test output and benchmark scores |
| `task/results/retail_l3_mwm.jsonl` | Result file accepted by `slb score` |
| `wire_ledger.json`, `meta_wire_ledger.json` | Host and learner provider accounting |

Both host and learner reach the provider through local wire meters. The learner meter also adapts requests for
the configured endpoint. Keep the meter settings and ledger with the run when comparing costs.

## Evaluate the windows

With the same environment variables as the launch:

```bash
examples/servelearnbench/mwm/launch.sh retail_l3 test
.venv/bin/slb score "$MW_RUNS/$MW_TAG/task/results/retail_l3_mwm.jsonl"
```

Manual test mode can run while training is in progress and skips windows without the required checkpoints.
The automatic waiter writes to `test.log`; manual test mode prints to the terminal.

The benchmark distinguishes Hidden and Fully Specified scores. Use its scorer and window protocol rather than
the generic evaluator's fixed solved-rate threshold.

## How the integration learns

After each serving task the benchmark supplies its permitted score feedback. The task pipeline appends MWM
context to the acting prompt and offers its `wm` tool alongside the environment tools. Every block of 12 cases
supplies evidence to a learner session. The learner does not play a serving task.

Tests are replayed afterwards against the version at each window boundary. This assumes the benchmark's
test episodes do not update the learning method. Since window serving counts do not all divide by 12, up to
11 cases of a window can remain unlearned at that window's test. Preserve this timing when comparing runs.

`MW_TRAIN_SPLIT` can select a prepared prefix for smoke testing; prefix results do not reproduce the full stream.
The task contract is implemented under `examples/servelearnbench/utils/`. For the world-model requirements see
[MWM](../methods/mwm.md), and for experiment reporting see [Evaluation](../guides/evaluation.md).
