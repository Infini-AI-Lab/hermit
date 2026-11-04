# CoEvoSkills Adapter

CoEvoSkills Adapter is the Generator/Verifier loop of CoEvoSkills (Zhang et al., 2026,
[arXiv:2604.01687](https://arxiv.org/abs/2604.01687); [code](https://github.com/Zhang-Henry/CoEvoSkills) at
`4380d4b`) with the search confined to the adapter. For one task at a time, a Generator writes an executable Skill
package (a `SKILL.md` with its scripts) in the task's container, a Verifier reviews it, and a fresh agent runs the
task with the package; the task's hidden tests decide pass or fail, only that verdict returns to the loop, and the
loop revises the package up to a fixed number of checks. Choose it when the acting agent should gain reusable,
executable procedures (scripts it runs, not only instructions it reads) and the tasks have a checkable answer.

**What it is not.** CoEvoSkills evolves a Skill for the task it is then used on (task-local), with its own executor in
the task's container. Here the acting agent is fixed: the fresh executor is the task's own acting agent and judge,
reached through the Trainer, and every package learned on a training question is kept in one adapter, whose packages
the acting agent receives as its native skills on every test question. The packages are learned on the training split
and evaluated frozen on the test split, so the result measures transfer, not the paper's task-local adaptation.

## Setup

Install the `coevoskills` dependency group (`python -m pip install -e . --group coevoskills`, pip 25.1 or newer)
and keep an unmodified checkout of the upstream repository at the pinned commit; `evolver.params.upstream_root`
(default `$COEVOSKILLS_ROOT`) names it, for its prompts, its meta-skills and its `scripts/`. The loop's task
containers need Docker. The run's `llm` is the model of every upstream role. The task supplies three things through
its example: a Harbor task package per training question (the question, its container, and the hidden test with
the reference answer, which only the oracle's container receives), `deliver` (the hook that writes the acting agent's
output where the hidden test reads it) and `path_map` (the task container's paths in the acting agent's workspace).
Before the first rollout `setup` checks the parameters, that the run has an `llm`, Harbor and the checkout (pinned
commit, no local change in `libs/`, `scripts/` or `meta_skills/`), that the package directory holds packages, and
that `deliver` loads. The acting agent's host must install the `skills` export natively (Codex, Claude Code).

## The template

`coevoskills-adapter@1.0` holds the starting context (`ADAPTER.md`) and the packages as the framework's `skills`
export: `skills/<task>-evo-<name>/`, one directory per package, each checked against upstream's Skill schema
(`.hermit/skill_schema.py`, vendored unmodified: the frontmatter `name` equals the directory). The host installs them
as the acting agent's native skills (Codex: `.codex/skills/` of the session's workspace), so the agent sees every
package's name and description and opens and runs the ones it chooses, as upstream's own executors find their Skills.
The paths in a package follow the deployment's convention, which the deployed context states: relative to the
adapter's folder in the workspace (`hermit/coevoskills-adapter/`, which holds `skills/` too).
No existing template fits: `prompt-only` has no executable packages, and `harness2-codex` and `meta-harness-adapter`
let the learner change files beyond the packages. The Evolver never edits `ADAPTER.md` or `exports.yaml`.

## One framework step

```mermaid
flowchart LR
    M["Trainer: the step's training questions<br/>(data.step_size = workers)"] --> G
    G["one worker per question:<br/>upstream Generator / Verifier<br/>(each completion: the run's llm)"] --> O
    O["oracle: check_spec + run_agent<br/>(the acting agent on the question)"]
    O --> T["hidden test in a fresh container:<br/>pass / fail only"]
    T -->|"fail, checks left"| G
    T -->|"pass, or checks used up"| I["best snapshot + upstream's verdict<br/>-> skills/&lt;question&gt;-evo-*/"]
    I --> C["Trainer commits the adapter"]
```

Each question runs in a worker process of its own, at most `workers` at a time; the questions of a step do not see
each other's packages, so `workers` changes the wall clock, not what is learned. A worker copies the question's
package without its `solution`, sets `allow_internet = false` (Harbor then runs the container with network none),
and runs Harbor's Trial with upstream's evolution agent unchanged. Upstream is called at three seams: its model
class (each completion becomes a call of the Trainer's `llm` tool, with the role's temperature and output cap), its
custom-oracle hook (the oracle makes the candidate the skills of a copy of the adapter without packages, checks it
with `check_spec`, runs it on the question with `run_agent`, and writes the acting agent's output through
`deliver`), and its Docker environment (cleanup leaves the images alone). Upstream ends by leaving its best snapshot
in the task's skills directory and recording that snapshot's final verdict, also when the loop then ends in an error
(a timeout, an agent error); the Evolver installs the snapshot's packages, renamed after the question, and records
the verdict and the loop's error. A question whose loop left no valid package, or whose package cannot be installed
(a name another question's skill has, or one the template check refuses: the install is all or nothing), leaves the
adapter as it was; the step fails only when none of its loops produced a package (with `keep: passed`, a step whose
packages all failed upstream's verdict is done and leaves the adapter unchanged). Each worker copies its question's
package under a name of its own, which Harbor gives the containers and the image of that question, and after the
worker the Evolver removes exactly those (each Docker command with a clock; a failure is recorded). A worker has a
clock of its own, `worker_timeout_s`: upstream's loop clock cannot interrupt a model call in progress, which blocks
its event loop. The oracle checks after each run that the acting agent ran the candidate as it was (integrity.py):
the deployed adapter copy by the framework's own deployment check, the native skills file by file. finalize hands
back the last adapter.

## Parameters and defaults

```{literalinclude} ../../hermit/agent/evolver/methods/coevoskills_adapter/evolver.yaml
:language: yaml
```

## Where this integration departs from the authors' code

| Dimension | CoEvoSkills (`4380d4b`) | CoEvoSkills Adapter | Kind | Status |
|---|---|---|---|---|
| When learning happens | task-local: the Skill is evolved on the task it is used on | once per training question; the frozen adapter is evaluated on the test split | decision (transfer under the column's split) | acceptable |
| What the learner sees | the task, its container, the Verifier's review, pass or fail of the hidden test | the same; as in upstream's tasks the container holds the instruction (with where the answer goes) and a background document, which for OfficeQA is the task's own description (`task-info/README.md`, nothing added: upstream's documents are written per task by its authors, and OfficeQA is not one of their tasks); the reference answer stays in the hidden test, the judge's feedback is not passed on | task | acceptable |
| The task container's network | as the task configures it (`allow_internet = true` in the authors' tasks) | always none (`allow_internet = false`): the Generator cannot install packages or fetch anything | decision (the column's isolation) | acceptable |
| The fresh executor (oracle) | a Claude Code (or other) agent with the Skill only, its instruction extended with a hint to use the Skill and code examples; the Skill immutable during the run | the task's own acting agent and judge through `run_agent`, with the task's own prompt (upstream's hint is not given), its output written where the hidden test reads it; after the run the deployed copies are checked against the candidate (the framework's deployment check and the native skills), a change making the check an error | framework (the acting agent is fixed) | acceptable |
| Each component's carrier | Skill packages in the task's container | the same packages as the adapter's skills, `skills/<task>-evo-<name>/`; the container's paths rewritten to the installed skill's and the workspace's (`packaging.py`) | framework | acceptable |
| What the acting agent sees | the task's own Skill, by the agent's native skill discovery | every training question's packages, by Codex's native skill discovery (names and descriptions listed, a package opened on demand) | decision (the per-task Skills transfer) | acceptable; it decides what the result measures |
| The update | revise until the hidden test passes or the checks run out; keep the best snapshot, passed or not | the same loop and the same snapshot; every exported package is installed (`keep: all`), `keep: passed` only a package that upstream's final verdict passed | — | same by default |
| Models | litellm to the configured provider, which drops a setting the model does not take: the roles' temperatures (0.7; the independent Verifier 0.3), an output cap of 16,384 tokens, ten attempts with waits up to 60 s on a rate limit, an empty reply retried | the run's `llm` for every role, always sent the same temperatures and output cap (an API model that refuses either fails the call; the shipped configs' GLM takes both, and a model reached through the Codex CLI ignores them); retries are the shared client's (four, waits doubling from 2 s; an empty reply is not retried); a model reached only through the Codex CLI answers each completion in one session, without sampling settings; a model name litellm does not know gets upstream's 128K-token context default | framework | acceptable; stated with every comparison (the run's `llm` does not expose its retry settings yet: a shared-layer extension) |
| Budgets and clocks | per task: checks, episodes; the task's agent timeout bounds both the whole loop and each oracle check | the same checks and episodes (`max_iterations`, `max_episodes`); the loop bounded by `task_timeout_s` (7,200 s), each oracle check by the package's agent timeout (OfficeQA: 4,500 s, an acting session and its start); each worker by `worker_timeout_s` (12,600 s), since upstream's clock cannot interrupt a model call in progress; the Trainer's budget and call clock | decision (an acting session's clock) | acceptable |
| Extra work | — | before each step the Trainer runs the acting agent on the step's questions with the current adapter (the run's training curve), charged to the budget; the loop does not read these runs | framework (the Trainer's step) | acceptable |
| What is not ported | — | the paper's benchmarks and their native executors | task | open |

## Records to read after a run

Under the run's `step_NNNN/output/`: `questions.json` (each question's outcome, upstream's final verdict, the loop's
error if any, whether and as which skills it was installed), and per question `loop.log`,
`loop/upstream-result.json`, `loop/evolution/` (upstream's trial: the Generator's and Verifier's episodes, the
oracle's checks, `agent/evolution_run_log.json` with the final verdict) and `portability.json` (each file's
rewrites). `state/installed.json` names each question's skills. The `llm` calls are in the run's `tools.jsonl`; the
oracle's acting runs are ordinary `run_agent` runs.

## Limits

- The worker processes run outside the run's sandbox, as the Evolver's own code does: they drive Docker for the loop's
  task containers, inherit the Trainer's environment (the model keys included) and reach the network. The task
  containers have no network; the hidden test with the reference answer reaches only the oracle's container.
- A step the Trainer stops (its call clock or the budget) stops its workers (SIGTERM, then SIGKILL after 15 s) and
  removes their containers, but a `run_agent` or `llm` call already in progress on the Trainer's tool server finishes
  on its own: the shared tool server refuses new calls once the call is cancelled and does not interrupt running ones.
  The same holds for an oracle check upstream gives up on at its clock: its acting run finishes in the background.
- The acting agent's host must install the `skills` export natively (Codex, Claude Code); the copy of the oracle's
  acting log into upstream's trial reads Codex's stream and is skipped for another host.

## Example

[OfficeQA](../recipes/officeqa.md#coevoskills-adapter): `examples/officeqa/coevoskills_adapter/`, with GLM 5.3 Flash
configs (`run_codex.yaml`, `smoke_codex.yaml`) and GPT-5.6-Luna configs (`run_codex_luna.yaml`,
`smoke_codex_luna.yaml`). No result is recorded yet: the earlier run was made with a different harness and is to be
repeated with this one.
