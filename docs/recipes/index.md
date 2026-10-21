# Recipe catalog

Pick a recipe by the task and evaluation protocol. Recipes specify actual integrations; the
[method guide](../methods/index.md) explains algorithm selection independently.

| Recipe | What you need | Methods | What it demonstrates |
|---|---|---|---|
| [Offline first training](../get-started/quickstart.md) | Included code, Python, Git | Scripted teaching learner | Feedback → adapter update → held-out evaluation → deployment |
| [First real-model training](../get-started/first-model.md) | Model endpoint, id, and credential; included data | Prompt-only | A real actor and learner without external benchmark assets |
| [OfficeQA](officeqa.md) | Gated dataset, parsed source pages, SkillOpt acting dependencies, model access | GEPA, SkillOpt; direct chat or Codex harness | Skill optimization for a document agent in the SkillOpt paper's split, seed skill, and scoring |
| [ServeLearnBench](servelearnbench.md) | Benchmark package, Linux learner environment, model access | MWM; prompt-only comparison configs | Ordered serving episodes and per-window held-out tests |
| [CL-Bench](clbench.md) | CL-Bench and MetaWorld checkouts, venue environments, Linux, model access | MWM; comparison examples in the repository | Persistent environment learning over ordered database/poker streams |
| [Lean collaboration](lean-collaboration.md) | Lean/Mathlib, agent hosts, team service | Existing adapter | Multi-agent consumption with shared tools and state |

## Find files in the task-oriented example layout

Examples are grouped by task, with a method folder inside each task. Paths below refer to the repository root.

| Task | Task integration | Method configurations / launchers |
|---|---|---|
| OfficeQA | `examples/officeqa/utils/` | `examples/officeqa/{gepa,skillopt}/` |
| CL-Bench database | `examples/clbench_database/utils/` | `examples/clbench_database/{mwm,prompt_only}/` |
| CL-Bench poker | `examples/clbench_poker/utils/` | `examples/clbench_poker/{mwm,prompt_only}/` |
| ServeLearnBench | `examples/servelearnbench/utils/` | `examples/servelearnbench/{mwm,prompt_only}/` |
| Lean swarm | `examples/lean_swarm/adapter/` | Existing adapter and explicit service/deployment workflow; no framework training config |

Each task's `utils/` holds data preparation, judge, task description, and either acting-agent code or a prompt/tools
for a built-in agent. Configs select the actor with `agent.name` and the learner with `evolver.name`.
A `prompt_only/` directory in a streaming experiment describes its instruction-only comparison; inspect its
actual `evolver.name` rather than assuming it uses the built-in prompt-only optimizer.

Configuration paths and `eval --adapter` directories resolve against the method config's directory. Stream
launchers can override output directories; use their recipe's explicit output settings. MWM venue text lives
under `utils/task-info/mwm/`, with a configurable deployment path.

## What counts as a result

The offline tutorial has constructed scores. A smoke config checks integration on a small subset. A trained
adapter's `CARD.md` records its experiment and measured evaluation. These provide different kinds of evidence.

When reproducing, preserve the framework revision, benchmark revision, model identifiers, task splits, complete
config, seed adapter, and scorer. Endpoint availability and model behavior can change independently of the code.
Expected runtime is a planning observation, not a promise; costs depend on models, trajectories, retries, and
candidate evaluation.

[Reproduce a result](../tutorials/reproduce-a-result.md) gives a checklist for matching an existing experiment.
