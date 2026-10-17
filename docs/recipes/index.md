# Recipe catalog

Pick a recipe by the task and evaluation protocol. Recipes specify actual integrations; the
[method guide](../methods/index.md) explains algorithm selection independently.

| Recipe | What you need | Methods | What it demonstrates |
|---|---|---|---|
| [Offline first training](../get-started/quickstart.md) | Included code, Python, Git | Scripted teaching learner | Feedback → adapter update → held-out evaluation → deployment |
| [First real-model training](../get-started/first-model.md) | Model endpoint, id, and credential; included data | Prompt-only | A real actor and learner without external benchmark assets |
| [OfficeQA](officeqa.md) | Prepared Treasury corpus, index, answers, scorer, model access | Prompt-only, Codex, GEPA, SkillOpt | Context optimization in a tool-calling document agent |
| [OfficeQA paper setting](officeqa-paper.md) | Gated dataset, parsed source pages, SkillOpt acting dependencies, model access | SkillOpt, GEPA | A defined split, seed skill, and paper-style scoring |
| [ServeLearnBench](servelearnbench.md) | Benchmark package, Linux learner environment, model access | MWM; prompt-only comparison configs | Ordered serving episodes and per-window held-out tests |
| [CL-Bench](clbench.md) | CL-Bench and MetaWorld checkouts, venue environments, Linux, model access | MWM; comparison examples in the repository | Persistent environment learning over ordered database/poker streams |
| [Lean collaboration](lean-collaboration.md) | Lean/Mathlib, agent hosts, team service | Existing adapter | Multi-agent consumption with shared tools and state |

## What counts as a result

The offline tutorial has constructed scores. A smoke config checks integration on a small subset. A trained
adapter's `CARD.md` records its experiment and measured evaluation. These provide different kinds of evidence.

When reproducing, preserve the framework revision, benchmark revision, model identifiers, task splits, complete
config, seed adapter, and scorer. Endpoint availability and model behavior can change independently of the code.
Expected runtime is a planning observation, not a promise; costs depend on models, trajectories, retries, and
candidate evaluation.

[Reproduce a result](../tutorials/reproduce-a-result.md) gives a checklist for matching an existing experiment.
