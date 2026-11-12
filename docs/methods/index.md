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
| Refine a harness per task from trajectories without labels, with a library of adopted harnesses | [Harness²](harness2.md) | `harness2-codex@1.1` | Learner model endpoint, Codex, the `harness2` dependency group |
| Refine a policy, sub-agents, skills and memory in place from each step's trajectories, without a gate | [Continual Harness](continual_harness.md) | `continual-harness@1.1` | Learner model endpoint, Codex |
| Grow an itemised playbook by incremental deltas from reflections, keeping the best validation playbook | [ACE](ace.md) | `ace@1.0` | Learner model endpoint; optional validation split |
| Evolve an agent's harness (prompt, middleware, scripts, skills, sub-agents, memory) from k rollouts per task, with each change attributed and reversible | [AHE Adapter](ahe_adapter.md) | `ahe@1.0` | An acting agent that runs hooks (Codex, Claude Code), a learner CLI (Codex, Claude Code or Pi), k rows per task |
| Search a dual-surface defense (prompt + tool hooks) for a frozen victim against frozen attack cases | [EvoSafeHarness](evosafe.md) | `evosafe@1.0` | Hook-bridge pipeline, utility/attack judge (verdicts in `JudgeResult.extra`), optional validation split, Claude Code, Codex or Pi |
| Search instructions with a model that reads the best earlier instructions with their training scores | [OPRO](opro.md) | `prompt-only@1.0` | Learner model endpoint; validation split for `select: validation` |
| Build a persistent world model from an ordered environment stream | [MWM](mwm.md) | `mwm` | Evidence store, deployment description, tools, Linux learner environment, a learner CLI (Codex, Claude Code or Pi) |
| Evolve a system prompt, rule bank, safety memory and tool policy from diagnosed trajectories | [SHE on AgentDyn](she.md) | `she@1.0` | Pinned SHE library, Docker hook bridge, disjoint validation split and a learner |
| Learn a bank of reflections online, retrieved per instance of an ordered stream | [ReHarness](reharness.md) | `reharness` | The run's `llm`, an embedding endpoint (or BM25), an agent that reads the adapter through `Session.context(query, task)` |
| Search whole adapters with a coding-agent proposer that reads every earlier candidate's files, scores and trajectories | [Meta-Harness Adapter](meta_harness_adapter.md) | `meta-harness-adapter` | A training split (the search set), a proposer CLI (Codex, Claude Code or Pi), an acting agent that delivers the adapter |
| Evolve whole adapters round by round under regularizers against overfitting the evolve set: an annealed edit budget, the edit history, exploration when stalled, a pre-evaluation leakage critic, a noise-adjusted floor and a cost rule | [RRSI Adapter](rrsi_adapter.md) | `rrsi-adapter` | A training split (the evolve set, k rows per task), a proposer CLI (Codex, Claude Code or Pi), the `rrsi` dependency group, a domain module with the benchmark's texts |
| Learn the framework contract offline | [Quick Start](../get-started/quickstart.md) | Prompt-only with a teaching learner | Included toy tasks |
| Grow a library of verified skill entries from robot tasks, each proposal re-solved by a fresh actor before it is admitted, with the coordinator choosing the next task | [ASPIRE](aspire.md) | `aspire` | A coordinator CLI (Codex, Claude Code or Pi), verifier task rows in `data.val`, an acting agent that delivers the adapter |

A learner model endpoint above can be your Claude subscription, and a learner CLI can run on it, with no API key:
[Claude subscription](../guides/claude-subscription.md) says which roles go which way.

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
| AHE Adapter | Per step: one Agent Debugger session per task (up to three attempts) and one Evolve Agent session; no candidate rollout, no validation | `state/`: stats, Debugger reports, manifests, verdicts, rollbacks, best-ever | Best-ever iteration by training pass rate, or the last |
| EvoSafeHarness | Per trial: Designer, Criticizer and Analyzer calls, plus candidate runs on the training cases (and the parent's validation cases when a candidate qualifies); the parent reuses the step's rollouts when they match | Lessons archive and trial history | Last promoted bundle |
| OPRO | `n_candidates` optimizer calls per step; each new instruction run on every training task; with `select: validation`, the `top_k` best run on validation at the end | `state/opro.json` (every instruction with its per-task training scores) | Best training score, or the best validation mean of the top `top_k` |
| MWM | Maintenance and a learner session per eligible block, plus native bootstrap/retry behavior | World-model adapter plus method/session records | Current world-model adapter |
| ReHarness | Per instance, one reflection call, one revision call per retrieved reflection (up to k) and one embedding call; no candidate rollouts or validation | Reflection bank in the adapter; source trajectories and per-instance retrieval and revision records in `state/` | Current adapter: the bank after the last instance |
| Meta-Harness Adapter | One proposer session per round writing k candidates; each candidate is a full rollout of the search set, one step per candidate | Every candidate's files, hypothesis, base and per-task scores in `state/` | Best mean on the search set |
| ASPIRE | Per task, one coordinator session and, when it proposes entries, a verifier rollout (a fresh actor on the task's verifier row) | The coordinator's curriculum and changelog notes in `state/` | Current adapter: the library after the last task |
| RRSI Adapter | Per round: the analyst with a digester per rendered trace (a session per turn), m proposer sessions with the critic's review and repairs, a smoke on the domain's smoke tasks per screened candidate; each candidate is a full rollout of the evolve set (k rows per task), one step per candidate | Every candidate's files and digest, the edit history, the attribution and the frontier in `state/` (upstream's files under `state/rrsi/`) | The incumbent H_T, admitted above the noise floor and within the cost rule |

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
