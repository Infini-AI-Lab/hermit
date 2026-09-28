# GEPA optimizer Evolver

[GEPA](https://github.com/gepa-ai/gepa) (Agrawal et al., *GEPA: Reflective Prompt
Evolution Can Outperform Reinforcement Learning*, ICLR 2026, arXiv:2507.19457)
evolves text components by reflecting on execution traces and keeping a Pareto
front of candidates over validation tasks.

This preset runs the upstream `gepa.optimize()` unchanged. The framework
supplies a `GEPAAdapter` (`harnessadapter/optimize/plugins/gepa_plugin.py`):

| GEPA concept | Harness adapter mapping |
| --- | --- |
| candidate `dict[str, str]` | the adapter's text surface, `{relative path: text}` (default: its context file, `ADAPTER.md`) |
| system under optimization | the acting harness (e.g. native Codex) with the candidate deployed as an adapter |
| `evaluate(batch, candidate)` | `Environment.evaluate(materialized adapter, tasks)` |
| trace / feedback `mu_f` | the agent's trajectory and the environment's train feedback |
| trainset / valset (Pareto set) | the environment's `train` / `val` splits; `test` is never visible |
| `run_dir` | the session's Training State (`gepa_state.bin`; pass it back to resume) |

```sh
pip install -e '.[gepa]'
harnessadapter optimize prompt-only --evolver gepa \
  --environment my_benchmark.env:make_environment --environment-config env.yaml \
  --optimizer-model accounts/fireworks/models/glm-5p3 \
  --optimizer-base-url https://api.fireworks.ai/inference/v1 --optimizer-key-env FIREWORKS_API_KEY \
  --set max_metric_calls=150 --output runs/
```

Settings are the `gepa.optimize()` arguments listed in `evolver.toml`; override
any of them with `--set key=value` or `--settings file.yaml`.
