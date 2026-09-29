# GEPA optimizer Evolver

[GEPA](https://github.com/gepa-ai/gepa) (Agrawal et al., *GEPA: Reflective Prompt
Evolution Can Outperform Reinforcement Learning*, ICLR 2026, arXiv:2507.19457)
evolves text components by reflecting on execution traces and keeping a Pareto
front of candidates over validation tasks.

`plugin.py` runs the upstream `gepa.optimize()` unchanged and supplies the
`GEPAAdapter` it asks for:

| GEPA concept | Harness adapter mapping |
| --- | --- |
| candidate `dict[str, str]` | the adapter's text surface, `{relative path: text}` (default: its context file, `ADAPTER.md`) |
| system under optimization | the acting harness (e.g. native Codex) with the candidate deployed as an adapter |
| `evaluate(batch, candidate)` | `context.evaluate(candidate, tasks)` in the Environment |
| trace / feedback `mu_f` | the agent's trajectory and the environment's train feedback |
| trainset / valset (Pareto set) | the environment's `train` / `val` splits; `test` is never visible |
| `run_dir` | the session's Training State (`gepa_state.bin`; pass it back to resume) |

```sh
pip install -e '.[gepa]'
harnessadapter evolve prompt-only --evolver gepa \
  --environment my_benchmark/env.py:make_environment --environment-config env.yaml \
  --model MODEL --set max_metric_calls=150
```

Settings are the `gepa.optimize()` arguments in `evolver.toml`; override any of
them with `--set key=value` or `--settings file.yaml`.
