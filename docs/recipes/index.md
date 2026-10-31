# Recipe catalog

Pick a recipe by the task and evaluation protocol. Recipes specify actual integrations; the
[method guide](../methods/index.md) explains algorithm selection independently.

| Recipe | What you need | Methods | What it demonstrates |
|---|---|---|---|
| [OfficeQA](officeqa.md) | Gated dataset, parsed source pages, SkillOpt acting dependencies, model access | GEPA, SkillOpt, Harness², Continual Harness, ReHarness, Meta-Harness Adapter, ACE; direct chat or Codex harness | Skill optimization for a document agent in the SkillOpt paper's split, seed skill, and scoring |
| [ServeLearnBench](servelearnbench.md) | Benchmark package, Linux learner environment, model access | MWM; prompt-only comparison configs | Ordered serving episodes and per-window held-out tests |
| [CL-Bench](clbench.md) | CL-Bench and MetaWorld checkouts, venue environments, Linux, model access | MWM, ReHarness; prompt-only comparison configs | Persistent environment learning over ordered database/poker streams |
| [Lean collaboration](lean-collaboration.md) | Lean/Mathlib, agent hosts, team service | Existing adapter | Multi-agent consumption with shared tools and state |

Before a recipe, the offline [Quick Start](../get-started/quickstart.md) and
[First real-model training](../get-started/first-model.md) run the same loop without benchmark assets.

## Where the files are

Every recipe's code, configs and trained adapters are under `examples/<task>/`; the layout of a task folder and how
to run one are in [`examples/README.md`](https://github.com/Infini-AI-Lab/hermit/blob/main/examples/README.md).
Configuration paths and `eval --adapter` directories resolve against the method config's directory. Stream
launchers can override output directories; use their recipe's explicit output settings. MWM venue text lives
under `utils/task-info/mwm/`, with a configurable deployment path.

## What counts as a result

The offline tutorial has constructed scores. A smoke config checks integration on a small subset. A trained
adapter's `.hermit/CARD.md` records its experiment and measured evaluation. These provide different kinds of evidence.

When reproducing, preserve the framework revision, benchmark revision, model identifiers, task splits, complete
config, seed adapter, and scorer. Endpoint availability and model behavior can change independently of the code.
Expected runtime is a planning observation, not a promise; costs depend on models, trajectories, retries, and
candidate evaluation.

[Reproduce a result](../tutorials/reproduce-a-result.md) gives a checklist for matching an existing experiment.
