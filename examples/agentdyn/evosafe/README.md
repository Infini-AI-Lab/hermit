# EvoSafeHarness on AgentDyn

[EvoSafeHarness](../../../docs/methods/evosafe.md) searches defense adapters using the
[AgentDyn integration](../utils/). Prepare the cases as described in the
[AgentDyn README](../README.md#prepare-the-cases).

| File | Purpose |
|---|---|
| `run_nemotron_codex.yaml` | Nemotron Super 49B victim, GPT-6 Sol Codex roles, 57 TRAIN / 54 TEST cases, no validation gate |
| `run_nemotron_claude.yaml` | The same victim with Claude roles and the 57 TRAIN / 54 validation / 54 TEST configuration |
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
`nvidia/llama-3.3-nemotron-super-49b-v1.5` to that ID in the backend file.

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

## Results

| Method | Clean utility ↑ | Attack success rate ↓ |
|---|---|---|
| No-defense | 50.0% | 30.3% |
| EvoSafeHarness | 52.9% | 8.8% |

Rates use completed episodes in each condition.

`hermit eval`'s scalar `mean_score` is not the defense objective. To report utility, ASR and errors:

```bash
python ../utils/report.py runs/agentdyn-evosafe-codex/eval/final/results.jsonl \
  --tasks ../utils/data/all-groups-v1/tasks.jsonl --split ../utils/data/all-groups-v1/test.txt
```

The experiment runner exports the three-way comparison automatically. For method differences,
see [REPRODUCTION.md](REPRODUCTION.md); framework integration details are in
[FRAMEWORK_SUPPORT.md](FRAMEWORK_SUPPORT.md).
