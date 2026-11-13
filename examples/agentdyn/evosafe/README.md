# EvoSafeHarness on AgentDyn

[EvoSafeHarness](../../../docs/methods/evosafe.md) searches defense adapters using the
[AgentDyn integration](../utils/). Prepare the cases as described in the
[AgentDyn README](../README.md#prepare-the-cases).

| File | Purpose |
|---|---|
| `run_nemotron_codex.yaml` | Nemotron 3.5 Lightning victim, GPT-6 Sol Codex roles, 57 TRAIN / 186 TEST cases with `--full-test`, no validation gate |
| `run_nemotron_claude.yaml` | Nemotron 3.5 Lightning victim with Claude roles, 57 TRAIN / 54 validation / 186 TEST cases |
| `run.yaml` | Minimal configuration for another victim and proposer |

## Run

Configure the victim's OpenAI-compatible service outside version control, either in
`~/.config/hermit/backends.yaml` or a file named by `HA_BACKENDS`:

```yaml
vllm:
  base_url: https://your-model-service.example/v1
  api_key_env: VICTIM_API_KEY
  key_required: true
  apis: [chat]
```

If the service uses another model ID, add a `models` alias from
`nvidia/nvidia/nemotron-3.5-lightning` to that ID in the backend file.

After committing source changes and choosing fresh run/tree paths:

```bash
cd examples/agentdyn/evosafe
export VICTIM_API_KEY=...
python ../utils/run_experiment.py --config run_nemotron_codex.yaml
```

The runner checks prerequisites, freezes code and inputs, trains the adapter, evaluates
`none`, `root` and `final`, and exports reports and adapters. Roles use the shared CLI Base
Agents with fresh sessions; the victim and classifier use `direct`. Test data is reserved
for frozen evaluation. See [execution details](EXPERIMENT_RUNTIME.md).

Both recipes use the same Lightning victim, 186-case TEST set and `max_iters: 25`. The 57 search cases and
each recipe's search/promotion settings are unchanged. The repeated `nvidia/` is part of the tested service model ID.

## Results

For reference only, not a reported result: the learner ran without Hermit's sandbox (see below), which a reported
result requires. Lightning with Codex roles on 186 TEST cases (18 clean, 168 injected), `max_iters: 25`, one
evaluation repeat:

| Adapter | Clean utility ↑ | Clean episode errors | Attack success rate ↓ | Injected episode errors |
|---|---:|---:|---:|---:|
| No-defense (`none`) | 75.0% (12/16) | 2 | 32.3% (50/155) | 13 |
| Initial (`root`) | 55.6% (10/18) | 0 | 28.7% (45/157) | 11 |
| EvoSafeHarness (`final`) | 62.5% (10/16) | 2 | 0.0% (0/150) | 18 |

Rates use completed episodes; errors are reported separately and are never counted as secure successes.
The final run has 20 errors in total, so its zero observed ASR does not establish success on all 168 injected
cases or a complete-run defense objective. These are descriptive fixed-case measurements. Claude-role
results have not been measured.

Recorded on 2026-11-11 in run `agentdyn-evosafe-codex` at framework revision `9c45a65`, using the
[full TEST protocol](../utils/DATA.md#full-test-variants) and the documented AgentDyn webpage patch.
The run's backend mapping resolved to `nvidia/nvidia/nemotron-3.5-lightning` (also recorded as
`victim_model_id` in its manifest). The learner used local `sandbox.enabled=false` because bubblewrap
was unavailable; learned hooks still ran in networkless Docker containers. Committed sandbox defaults
remain enabled. The runner retains `artifacts/comparison.json`, `artifacts/manifest.json` and per-case
records locally; generated artifacts are not committed.

`hermit eval`'s scalar `mean_score` is not the defense objective. To report utility, ASR and errors:

```bash
python ../utils/report.py runs/agentdyn-evosafe-codex/eval/final/results.jsonl \
  --tasks ../utils/data/full-test-v1/tasks.jsonl --split ../utils/data/full-test-v1/test.txt
```

The experiment runner exports the three-way comparison automatically. For method differences,
see [REPRODUCTION.md](REPRODUCTION.md); framework integration details are in
[FRAMEWORK_SUPPORT.md](FRAMEWORK_SUPPORT.md).
