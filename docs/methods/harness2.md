# Harness²

Harness² ([Xu et al., 2026](https://arxiv.org/abs/2610.14224), [code](https://github.com/google-research/harness2))
refines an agent's harness without labels. Each step runs a contrastive Propose–Probe–Compose round: two scoped
editors propose changes, each candidate is executed on the same tasks, and a composer keeps only what the
trajectories support. A per-domain library stores every adopted harness with the lesson it taught. No judge score,
judge feedback or reference answer reaches any session, so the method needs no validation split.

## Setup

```yaml
template: harness2@1.0
evolver:
  name: harness2
  params: {k: 1, slots: slots.yaml}
llm: <backend>/<model>          # the editors' model; the run's llm tool
```

The template is a tree of harness components rendered into `ADAPTER.md`: four always-on files under `harness/`
(system prompt, guardrails, long-term memory, tool guidance) and skills under `skills/<name>/SKILL.md`. Its
`harness2.yaml` declares how the acting agent receives them; the built-in template renders skill bodies inline, for
an agent that reads only `ADAPTER.md`. To start from an existing instruction text, put it in
`harness/systemprompt.md` and render, as [`make_seed.py`](../../examples/officeqa_paper/harness2/make_seed.py) does.

`slots.yaml` (or `task-info/harness2.yaml`) gives the task-shaped text the stage prompts interpolate: `work`,
`domain_word`, `deliverable_word`, `inv_levers`, `dom_levers`, `yardstick`, `domain_guidance`, `selection_checks`.
They describe the work, never its measurement: the Evolver refuses the words benchmark, evaluation, grader, rubric
and score there. Spell out the deliverable's exact shape in `work`: the stage prompts keep a deliverable's shape
fixed, but only the shape the work names. On OfficeQA, a `work` that said only "in the form the question requests"
let adopted edits move derivations and caveats inside the `<answer>` tags.

## One framework step

1. **Evidence.** The step's rollouts are the reference set: one trajectory per task in a shared
   `[ACTION]/[OBSERVATION]` grammar, what each delivered, the task texts, the current harness, the acting agent's
   prompt, and the domain's library. Judge fields are never read, and a tripwire refuses evidence that carries them.
2. **Propose.** An INV editor (how the agent works: tool use, procedure, completion) and a DOM editor (what a
   practitioner knows) each edit a copy of the harness. An invalid proposal is retried once with the validator's
   message, then falls back to the current harness.
3. **Probe.** Each candidate runs on the same tasks through `run_agent`; its cost counts toward the run's budget.
4. **Compose.** A composer reads the reference set and every candidate's probes and writes the adopted harness,
   recording `kept|revised|dropped -> edit -> evidence` per decision. The composition is probed too.
5. **Select.** At `k = 1` the composition is adopted. At `k > 1` a blind selector compares the finals'
   deliverables per task, anonymised, and the final with the most picks is carried.
6. **Reflect.** A reflector writes a verdict per changed component (`helped`, `hurt`, `no observed effect`,
   `confounded`) and at most one transferable lesson. Only a step with a lesson enters
   `state/library/<domain>/`, with its full harness and an `EXPERIENCE.md`.

A step at `k = 1` costs three extra rollouts per task (two candidates and the composition) and four or five model
calls (two editors, the composer, the reflector, and selectors when `k > 1`). Every step's `output/report.md`
records the stages, their notes and fallbacks, the ledger, the edit verdicts and the lesson. `output/sessions/`
keeps each call's mission and reply.

## Parameters and defaults

```{literalinclude} ../../harnessadapter/algorithms/evolvers/harness2/evolver.yaml
:language: yaml
```

| Parameter | Effect |
|---|---|
| `k`, `recursion` | `parallel`: k INV and k DOM candidates, k independent compositions. `sequential`: k chained rounds. Both spend 1 + 3k batch rollouts and 3k editing calls per step. |
| `select` | `auto` adopts the composition at k = 1 and runs blind selection at k > 1. |
| `inline_chars`, `obs_trunc` | The `llm` channel inlines the evidence into one prompt; trajectories are clipped to fit, with explicit markers. |
| `domain_field` | A task field naming the library's domain. Tasks without it share one library. |

## Differences from the paper

- The paper resets every task to the base harness and keeps only the library. In a training loop the adopted
  harness is carried to the next step, and `eval --adapter final` measures it on held-out tasks.
- A step's reference set is one rollout of each task of the batch, not k replicates of one task; the prompts read
  it as a set across tasks.
- Sessions run on the `llm` channel: one call per stage with the evidence inlined, as GEPA and SkillOpt do. The
  paper's editors are agentic sessions that open the evidence files themselves; an `agent` channel is planned.
- Hooks and plugins (the paper's C8) are not part of the template, and scripts (C7) only where the actor can run
  them.

## The paper's protocol, by the upstream code

`harness2_upstream` runs Harness² as the paper does, with the paper's own code: the upstream package
(`pip install -e '.[harness2]'`, pinned) drives every task through its benchmark seam, and nothing above is reused.
The training split is the task stream:

```yaml
data: {train: stream.txt, step_size: 1, shuffle: false}   # one task per step, in the stream's order
trainer: {epochs: 1}
evolver:
  name: harness2_upstream
  params: {model: <backend>/<model>, effort: medium, k: 1, slots: slots.yaml}
```

Each step hands the step's task to `harness2.core.schedule.run_task`: the reference execution is the Trainer's
rollout of the starting adapter H0; the INV and DOM candidates and the composition are executed on the same task
through `run_agent`; the composition's execution is the task's deliverable; a reflection adds the adopted harness and
its lesson to the domain's library, which is the only thing the next task inherits. The adapter is never written, so
every task starts from H0. The editor sessions are upstream's `claude_cli` channel (Claude Code on the bound model,
which must serve the Messages API), each in a sandbox that shows only its evidence and reaches only the model. A
task's score is the judge's score of its deliverable, in `step_NNNN/step_result.json` (`extra.harness2`), never seen
by a session. Upstream knows OpenCode's and Codex's surfaces; this one describes the template's, where every
component is in the agent's prompt and there are no scripts, subagents or plugins.

`library_from: <run directory>` continues a finished stream on other tasks: the run starts from that stream's
library, copied into its Training State before the first task, and is the same stream with the new tasks at its end.
It refuses a run that has not finished, a library carrying grader wording, and a task that stream already took.

## Examples

[`examples/officeqa_paper/harness2/`](../../examples/officeqa_paper/harness2/) runs the `harness2` Evolver in the
SkillOpt paper's OfficeQA setting, with the split, rollout, scorer, seed skill, optimizer model and budget of the
GEPA and SkillOpt runs beside it.
[`examples/officeqa_paper/harness2_stream/`](../../examples/officeqa_paper/harness2_stream/) runs
`harness2_upstream` on the same test questions as one stream, with the same executor and optimizer model.
