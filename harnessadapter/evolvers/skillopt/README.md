# SkillOpt optimizer Evolver

[SkillOpt](https://github.com/microsoft/SkillOpt) (Yang et al., *SkillOpt:
Executive Strategy for Self-Evolving Agent Skills*, 2026, arXiv:2605.23904)
trains one skill document with bounded add/delete/replace edits proposed by an
optimizer model from rollout minibatches, accepting an edit only when it
strictly improves a held-out selection score.

This preset runs the upstream `ReflACTTrainer.train()` unchanged. The framework
supplies a SkillOpt `EnvAdapter` (`harnessadapter/optimize/plugins/skillopt_plugin.py`):

| SkillOpt concept | Harness adapter mapping |
| --- | --- |
| skill document `best_skill.md` | one text component of the adapter (default: its context file, `ADAPTER.md`) |
| harness `h(M, x, s)` | the acting harness with the skill deployed as the adapter's context |
| `rollout(env, skill, out_dir)` | `Environment.evaluate(materialized adapter, tasks)`, trajectories written to `predictions/<id>/conversation.json` |
| train / selection splits | the environment's `train` / `val`; the framework evaluates `test` for every method alike |
| optimizer model | SkillOpt's `openai_compatible` backend pointed at the session's optimizer model |
| `out_root` | the session's Training State (`runtime_state.json`, `history.json`; the trainer resumes from it) |

SkillOpt ships its defaults in `configs/_base_/default.yaml`, which is part of
the source tree rather than the wheel: install it from a checkout
(`pip install -e /path/to/SkillOpt`) or set `SKILLOPT_HOME`. Its trainer keeps
process-global backend state, so run one SkillOpt session per process.

```sh
harnessadapter optimize prompt-only --evolver skillopt \
  --environment my_benchmark.env:make_environment --environment-config env.yaml \
  --optimizer-model accounts/fireworks/models/glm-5p3 \
  --optimizer-base-url https://api.fireworks.ai/inference/v1 --optimizer-key-env FIREWORKS_API_KEY \
  --set num_epochs=2 --set batch_size=20 --output runs/
```
