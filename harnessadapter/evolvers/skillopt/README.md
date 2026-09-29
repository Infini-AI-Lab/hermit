# SkillOpt optimizer Evolver

[SkillOpt](https://github.com/microsoft/SkillOpt) (Yang et al., *SkillOpt:
Executive Strategy for Self-Evolving Agent Skills*, 2026, arXiv:2605.23904)
trains one skill document with bounded add/delete/replace edits proposed by an
optimizer model from rollout minibatches, accepting an edit only when it
strictly improves a held-out selection score.

`plugin.py` runs the upstream `ReflACTTrainer.train()` unchanged and supplies
the `EnvAdapter` it asks for:

| SkillOpt concept | Harness adapter mapping |
| --- | --- |
| skill document `best_skill.md` | one text file of the adapter (default: its context file, `ADAPTER.md`) |
| harness `h(M, x, s)` | the acting harness with the skill deployed as the adapter's context |
| `rollout(env, skill, out_dir)` | `context.evaluate({file: skill}, tasks)`, trajectories written to `predictions/<id>/conversation.json` |
| train / selection splits | the environment's `train` / `val`; `harnessadapter evaluate` scores `test` for every Evolver alike |
| optimizer model | SkillOpt's `openai_compatible` client, pointed at the session's model through `context.model.serve()` |
| `out_root` | the session's Training State (`runtime_state.json`, `history.json`; the trainer resumes from it) |

SkillOpt ships its defaults in `configs/_base_/default.yaml`, which is part of
the source tree rather than the wheel: install it from a checkout
(`pip install -e /path/to/SkillOpt`) or set `SKILLOPT_HOME`. Its trainer keeps
process-global backend state, so run one SkillOpt session per process.

```sh
harnessadapter evolve prompt-only --evolver skillopt \
  --environment my_benchmark/env.py:make_environment --environment-config env.yaml \
  --model MODEL --set num_epochs=2 --set batch_size=20
```
