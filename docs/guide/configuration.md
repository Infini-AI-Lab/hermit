# Configuration reference

Use CLI flags directly or a YAML launch file. Both reach the same implementation. YAML paths resolve relative to the configuration file.

The conceptual input is **Experience**. The existing CLI and YAML spellings remain `--evidence` and `evidence`; use these names in executable configuration.

## Evolution YAML

```yaml
action: evolve
architecture: prompt-only
evidence: ../examples/mwm_evidence
backend: codex
prompt: Read the evidence and improve ADAPTER.md.
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
| --evidence DIR | Required read-only storage with README.md |
| --validation DIR | Optional visible read-only validation storage |
| --prompt TEXT / --prompt-file FILE | User update instructions |
| --evolver NAME | Reusable basic or mwm preset, or local definition directory |
| --backend | codex or claude-code |
| --workdir DIR | Read-only environment cwd for learner commands |
| --read-dir DIR / --read-all | Additional reads; never grants writes |
| --network / --no-network | Agent tool networking; default on |
| --config FILE | Execution policy in TOML, JSON, or YAML |
| --timeout SECONDS | Model session timeout; default 240 |
| --max-budget-usd N | Claude-only model budget; rejected for Codex |
| --prepare-only | Prepare candidate and run permission preflight, no learner model call |

Launch YAML uses backend for the learner and host for the acting agent. Unknown fields fail validation. A list of multiple adapters is explicitly rejected until multi-adapter deployment exists.

## Training State protocol

Training State is an optional input/output for a stateful Evolver, separate from the Adapter and reusable algorithm definition. No generic training-state CLI or YAML field is implemented yet; do not treat run logs as resumable optimizer state. Stateless methods require no state artifact.
