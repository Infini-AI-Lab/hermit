# Choose a learning method

Choose the editable adapter content first, then a method that understands it. A template defines the files,
exports, and checks; an Evolver defines how evidence becomes an update. Sharing the interface does not make all
template–method combinations meaningful.

| Goal | Starting method | Template | Additional inputs |
|---|---|---|---|
| Improve a short instruction document with few moving parts | [Prompt-only](prompt-only.md) | `prompt-only@1.0` | Codex CLI, Linux learner environment |
| Let a coding agent edit several files, skills, or code | [Codex / Claude](agent.md) | A template with the intended learnable paths | Authenticated learner CLI |
| Search text candidates with validation-based selection | [GEPA](gepa.md) | Start with prompt-only | Validation split and the `gepa` dependency group |
| Make bounded skill edits with a validation gate | [SkillOpt](skillopt.md) | Start with prompt-only or the paper seed | Validation split and the `skillopt` dependency group |
| Refine a harness per task from trajectories without labels, with a library of adopted harnesses | [Harness²](harness2.md) | `harness2-codex@1.0` | Learner model endpoint, Codex, the `harness2` dependency group |
| Refine a policy, sub-agents, skills and memory in place from each step's trajectories, without a gate | [Continual Harness](continual_harness.md) | `continual-harness@1.0` | Learner model endpoint, Codex |
| Build a persistent world model from an ordered environment stream | [MWM](mwm.md) | `mwm` | Evidence store, deployment description, tools, Linux learner environment |
| Learn the framework contract offline | [Quick Start](../get-started/quickstart.md) | Prompt-only with a teaching learner | Included toy tasks |

## Compare the work behind each step

All methods share the main acting batch. The table describes additional work, excluding retries and any
task-specific service overhead. Call counts vary with evidence, candidates, and settings.

| Method | Additional model / acting work | Persistent learner state | Final selection |
|---|---|---|---|
| Prompt-only | One Codex learner session per step that rewrites `ADAPTER.md`; no candidate rollout gate | Notes the session keeps in `state/` | Current adapter |
| Codex / Claude | A multi-action learner session; candidate tests depend on what the learner does | Saved files and optional supported session handle | Current adapter unless the method customizes finalization |
| GEPA | Initial-parent validation, reflection, child minibatch tests, and validation for a child that passes the minibatch criterion | Candidate pool, validation scores, per-candidate next file | Highest retained validation mean |
| SkillOpt | Reflection/merge/ranking calls, validation (cached by skill content), optional epoch-end and final validation work | Current/best skills, validation cache, update history and meta skill | Retained best, with optional final promotion |
| Harness² | Two editors, a composer and a reflector per task, plus three probe executions (k = 1); the adapter stays H0 | Library of adopted harnesses with lessons | Unchanged adapter; the stream's own deliverables are the result |
| Continual Harness | Four Refiner calls per step (policy, sub-agents, skills, memory); no candidate rollout, no validation | Step log in `state/`; the memory the agent writes comes back with each trajectory | Current adapter |
| MWM | Maintenance and a learner session per eligible block, plus native bootstrap/retry behavior | World-model adapter plus method/session records | Current world-model adapter |

`use_gate: false` in SkillOpt removes the ordinary improvement requirement, **not** the validation calls.
GEPA's minibatch criterion is separate from its validation-based parent/final selection.
Use [run records](../guides/runs.md) to compare actual calls and costs rather than treating every step as equal work.

## Decide what the learner may change

For context-only experiments, start with `ADAPTER.md`. To learn skills or tool implementations, declare those
paths as learnable in a custom template. Changing `evolver.name` does not expand those permissions. The
[customization tutorial](../tutorials/customize-an-adapter.md) shows how to create a separate template identity.

A text optimizer writes selected text files. A coding-agent Evolver can make more general edits, within the
template's rules. MWM has its own memory, programs, evidence protocol, and learner contract; choosing it requires
a task integration that supplies those resources.

## Plan the data and comparison

- Keep test tasks for evaluation. GEPA and SkillOpt additionally require validation tasks for candidate selection.
- For an ordered stream, set `data.shuffle: false`; choose batch boundaries according to the environment's timeline.
- Hold acting model, tools, judge, and evaluation protocol fixed when comparing methods.
- Compare against both no adapter and the starting adapter. Their difference measures the template's initial contribution.
- Record learning calls, candidate evaluations, and acting calls. Equal training steps do not imply equal cost.

The framework checks declared needs and file validity. It does not automatically establish learning effectiveness
or choose a method for you. Continue with [Train an experiment](../guides/training.md).
