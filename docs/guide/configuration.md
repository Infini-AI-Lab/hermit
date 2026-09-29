# Configuration reference

Use CLI flags directly or a YAML launch file. Both reach the same implementation. YAML paths resolve relative to the configuration file.

The public input is **LearningMaterial**. Use `--learning-material DIR` or YAML `learning_material: DIR`. The old `--evidence` and `evidence` names remain aliases; do not supply both names. Paths in YAML resolve relative to the configuration file.

## Evolution YAML

```yaml
action: evolve
architecture: prompt-only
learning_material: ../examples/mwm_evidence
backend: codex
prompt: Read the LearningMaterial and improve ADAPTER.md.
network: false
read_access:
  paths: []
```

Use adapter instead of architecture to continue an existing candidate. Use prompt_file instead of prompt for a Markdown instruction file. An optional validation field points to a separate validation directory. --template and the YAML template key remain compatibility aliases for architecture.

```sh
harnessadapter run config/evolve.yaml --dry-run
harnessadapter run config/evolve.yaml --prepare-only
harnessadapter run config/evolve.yaml
```

## Deployment YAML

```yaml
action: deploy
adapter: prompt-only
host: codex
workdir: ../.demo/prompt-only/codex
```

## Evolution flags

| Flag | Meaning |
| --- | --- |
| --architecture NAME | Initialize a separate candidate from prompt-only, mwm, or a directory |
| positional adapter | Continue from an existing adapter directory |
| --learning-material DIR | Required read-only storage with README.md |
| --validation DIR | Optional visible read-only validation storage |
| --prompt TEXT / --prompt-file FILE | User update instructions |
| --evolver NAME | Reusable basic, mwm, animation-coach, physics-builder, gepa, or skillopt preset, or local definition directory |
| --model ID | Explicit learner model |
| --reasoning-effort LEVEL | Codex effort: low, medium, high, xhigh |
| --backend | codex or claude-code |
| --workdir DIR | Read-only environment cwd for learner commands |
| --read-dir DIR / --read-all | Additional reads; never grants writes |
| --network / --no-network | Agent tool networking; default on |
| --config FILE | Execution policy in TOML, JSON, or YAML |
| --timeout SECONDS | Model session timeout; default 240 |
| --max-budget-usd N | Claude-only model budget; rejected for Codex |
| --prepare-only | Prepare candidate and run permission preflight, no learner model call |

Optimizer Evolvers (`--evolver gepa`, `skillopt`, or a `kind = "optimizer"` directory) take `--environment SPEC` (required), `--environment-config FILE`, `--set KEY=VALUE` / `--settings FILE`, `--component FILE`, `--training-state DIR`, `--max-tokens N`, `--temperature T` and `--scripted-model`, plus `--model`, `--reasoning-effort`, `--provider-base-url`, `--provider-env-key` and `--output`. They take no LearningMaterial; each kind refuses the other kind's options. Launch YAML uses the same names (`environment`, `environment_config`, `settings` as a mapping, `components`, `training_state`). See [Optimizer Evolvers](optimizer-evolvers.md).

Launch YAML uses backend for the learner and host for the acting agent. Unknown fields fail validation. A list of multiple adapters is explicitly rejected until multi-adapter deployment exists.

## Training State protocol

Training State is an optional input/output for a stateful Evolver, separate from the Adapter and reusable algorithm definition. Optimizer Evolvers write it to the session's `training_state/` and resume from `--training-state DIR` (YAML `training_state`); resuming is refused when the environment's splits changed. Learner Evolvers have none; do not treat their run logs as resumable state.
