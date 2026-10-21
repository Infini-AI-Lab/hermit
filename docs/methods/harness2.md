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

```{literalinclude} ../../hermit/algorithms/evolvers/harness2/evolver.yaml
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

## Example

[`examples/officeqa_paper/harness2/`](../../examples/officeqa_paper/harness2/) runs Harness² in the SkillOpt
paper's OfficeQA setting, with the split, rollout, scorer, seed skill, optimizer model and budget of the GEPA and
SkillOpt runs beside it.
