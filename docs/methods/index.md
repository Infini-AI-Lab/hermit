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
| Grow an itemised playbook by incremental deltas from reflections, keeping the best validation playbook | [ACE](ace.md) | `ace@1.0` | Learner model endpoint or a Codex login; optional validation split |
| Build a persistent world model from an ordered environment stream | [MWM](mwm.md) | `mwm` | Evidence store, deployment description, tools, Linux learner environment |
| Learn a bank of reflections online, retrieved per instance of an ordered stream | [ReHarness](reharness.md) | `reharness` | The run's `llm`, an embedding endpoint (or BM25), an agent that reads the adapter through `Session.context(query, task)` |
| Search whole adapters with a coding-agent proposer that reads every earlier candidate's files, scores and trajectories | [Meta-Harness Adapter](meta_harness_adapter.md) | `meta-harness-adapter` | A training split (the search set), a proposer CLI (Codex or Claude Code), an acting agent that delivers the adapter |
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
| ACE | Per sample: one Reflector call, or for a wrong answer up to `max_num_rounds` Reflector calls and agent re-runs, one Curator call; validation at each epoch's end when `data.val` is set | `state/ace.json` (sample records, validation scores) and the best validation playbook | Best validation playbook, else the current one |
| MWM | Maintenance and a learner session per eligible block, plus native bootstrap/retry behavior | World-model adapter plus method/session records | Current world-model adapter |
| ReHarness | Per instance, one reflection call, one revision call per retrieved reflection (up to k) and one embedding call; no candidate rollouts or validation | Reflection bank in the adapter; source trajectories and per-instance retrieval and revision records in `state/` | Current adapter: the bank after the last instance |
| Meta-Harness Adapter | One proposer session per round writing k candidates; each candidate is a full rollout of the search set, one step per candidate | Every candidate's files, hypothesis, base and per-task scores in `state/` | Best mean on the search set |

`use_gate: false` in SkillOpt removes the ordinary improvement requirement, **not** the validation calls.
GEPA's minibatch criterion is separate from its validation-based parent/final selection.
Use [run records](../guides/runs.md) to compare actual calls and costs rather than treating every step as equal work.

## Decide what the learner may change

For context-only experiments, start with `ADAPTER.md`. To learn skills or tool implementations, declare those
paths as learnable in a custom template. Changing `evolver.name` does not expand those permissions. The
[customization tutorial](../tutorials/customize-an-adapter.md) shows how to create a separate template identity.

A text optimizer writes selected text files. A coding-agent Evolver can make more general edits, within the template's
rules. MWM has its own memory, programs, evidence protocol, and learner contract; choosing it requires a task
integration that supplies those resources. ReHarness writes its reflection bank (`reflections/*.json`, `index.json`)
and leaves `ADAPTER.md` alone; its acting agent must read the adapter through `Session.context(query, task)`.
Meta-Harness Adapter may write any file of the adapter outside `.hermit/`; what reaches the acting agent besides
`ADAPTER.md` depends on the agent, and the task information says what it is.

## Plan the data and comparison

- Keep test tasks for evaluation. GEPA and SkillOpt additionally require validation tasks for candidate selection.
- For an ordered stream, set `data.shuffle: false`; choose batch boundaries according to the environment's timeline.
- Hold acting model, tools, judge, and evaluation protocol fixed when comparing methods.
- Compare against both no adapter and the starting adapter. Their difference measures the template's initial contribution.
- Record learning calls, candidate evaluations, and acting calls. Equal training steps do not imply equal cost.

The framework checks declared needs and file validity. It does not automatically establish learning effectiveness
or choose a method for you. Continue with [Train an experiment](../guides/training.md).
