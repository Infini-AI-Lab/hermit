---
orphan: true
---

# Harness² integration plan

Status: proposal, 2026-10-19. Nothing in this document is implemented yet. It is written against
MetaHarnessAdapter `main` at `3ad559b` and the Harness² release at
[google-research/harness2](https://github.com/google-research/harness2) `1cfe254` (paper:
[arXiv:2610.14224](https://arxiv.org/abs/2610.14224), site: [harness2.github.io](https://harness2.github.io)).

The README already lists Harness² under *Planned support*. This document says what the method is, where it
does and does not fit the framework's contracts, and gives three concrete plans with a recommendation.

## TL;DR

Harness² is an **online, label-free, test-time** method: for each arriving task it refines a fresh copy of the
base execution harness through a contrastive **Propose–Probe–Compose** step (two scoped candidates, probed on the
same task, composed into a successor, executed again), picks a deliverable blindly, and then stores the adopted
harness plus an evidence-backed lesson in a **per-domain library** that guides the refinement of the next task.
The persistent, learned object is the library (the *improvement harness*), not the execution harness, which resets
to the base for every task.

MetaHarnessAdapter is a **training loop**: batches of tasks, judge-scored material, one adapter handed back per
step, evaluation on a held-out split. The two fit together in two different ways, and both are worth having:

| | Plan A: execution-harness Evolver | Plan B: online stream (agent + library Evolver) | Plan C: vendor `harness2` with shims |
|---|---|---|---|
| What is learned | the execution harness (ADAPTER.md, skills, roles), carried across steps | the library (adopted harnesses + EXPERIENCE.md), the execution harness resets per task | whatever `harness2.core` learns; MHA supplies tasks and the actor |
| Protocol fidelity | a contrastive step per batch; a training-loop variant of the paper | the paper's protocol (per-task recursion, blind selection, reflection) | exact paper code |
| Fits `train` / `eval` as they are | yes | yes, with `step_size: 1`, `shuffle: false`; the stream's own scores are the result | partly: a benchmark-shaped outer loop |
| New framework surface | none required | an acting-agent wrapper (additive) | a Python 3.12 optional extra, Vertex-shaped config to neutralise |
| Effort (one person) | 3 to 5 days | 2 to 3 days on top of A | 2 to 3 days, low reuse value |

**Recommendation:** build the shared core once (Section 6), ship Plan A first as `evolver.name: harness2` with
`mode: exec`, then Plan B as `mode: library` plus the `Harness2Agent` wrapper. Keep Plan C as a reproduction
path only if the team wants numbers comparable to the paper on JobBench, LAB or WorkBuddy-Bench; it is not a
good foundation for the framework.

## 1. What Harness² is, precisely

### 1.1 The two recursions

The paper models an agent as *frozen model + editable harness*, the harness being a tree of eight component
files (Table 5 of the paper), and runs two coupled loops over a stream of tasks without ground truth:

1. **Task-level recursion** refines the current task's execution harness `H` with an operation
   `Φ(q_t, H, E; R_t) → (H', e', ξ)`: a successor harness, its execution record, and a step record `ξ` (reference
   executions, candidates, probes, composition decisions, successor execution). The improvement harness `R_t`
   stays fixed during the task.
2. **Harnessing-level recursion** updates the improvement harness after the task:
   `R_{t+1} = Update(R_t, H*_t, D_t)`, where `H*_t` is the adopted harness and `D_t` the full improvement record.
   `R` is a per-domain library; each entry is the full adopted component tree plus an `EXPERIENCE.md` (why it was
   chosen, what each edit did, one transferable lesson) and an `INDEX.md`.

Every task starts again from the common base harness `H_0`. The library informs the next task's proposals and
compositions; it is never inherited automatically. The paper's ablation shows the library is worth about 3 points
on WorkBuddy-Bench and 2 on JobBench.

### 1.2 The contrastive step: Propose, Probe, Compose

- **Reference executions.** `k` rollouts of the task under the unmodified harness. They are read *as a set*: a
  failure that repeats across them is a property of the harness; a defect in 1 of `k` is noise.
- **Propose.** Two scoped editors, each an agentic session working in a copied evidence directory:
  - `INV` (invariant): how the agent *works*: tool errors, mechanical repetition, triggered workflows, ignored
    invariants, sequencing. It must not add domain rules.
  - `DOM` (domain): what a practitioner *knows*: analytic moves, artifact contracts, decision rules, encoded as
    skills by default. It must not touch procedure, scripts or hooks.
  Each editor edits the harness files in place; the edited directory *is* the proposal. A `<notes>` block ties
  every change to a trajectory moment. `KEEP_BASE` (no change) is a valid answer. The library is available as
  examples to copy from and re-verify, never as part of the edited tree.
- **Probe.** Each candidate is executed once on the same task, same model, same environment. The contrast with
  the reference is evidence about behavior, not correctness.
- **Compose.** A composer reads the reference set, both candidates, their diffs against the current tree, their
  rollouts and the earlier decisions, then merges, revises, adopts one, or keeps the base. The composed harness is
  executed. Decisions are recorded as `kept|revised|dropped -> edit -> evidence`.

Two scalings, at budget `k`: **parallel** (`k` reference runs, `k` INV + `k` DOM candidates all probed, `k`
independent compositions each executed; width) and **sequential** (`k` chained steps, each round's composer
sees every earlier round; depth). Both cost exactly `R = 4k` rollouts and `P = 3k` improver sessions per task,
and the code asserts that identity in a ledger.

### 1.3 Selection and reflection

- **Final selection** (`k > 1` only): a selector sees `k` anonymized, shuffled *deliverables* with the task and
  source material, never the harnesses or trajectories, and must pick one by delivery, conformance, fidelity to
  the source, internal consistency, operational fitness and finish. At `k = 1` the single final is adopted without
  a session.
- **Reflection** (every task): a reflector diffs every candidate against the base, writes one row per changed
  component with a verdict in `{helped, hurt, no observed effect, confounded}`, and at most one durable
  `<experience>` entry (`cue`, `lesson` naming a component, `evidence`, `confidence`) or `NO_ENTRY`. Only a task
  with an entry enters the library.

### 1.4 Invariants the code enforces (and that any port must keep)

- **Label-free.** No prompt names a rubric, score or judge; `BenchPrompts` refuses the words. A tripwire
  (`assert_no_grader_verdicts`, `assert_no_leak_paths`) refuses any evidence tree, candidate tree or library entry
  that contains grader output or answer-key paths. Scores are reserved for `harness2-judge`, run after adoption.
- **Validation with one retry.** A proposed tree must pass the surface validators (skill frontmatter, a trigger
  line naming every skill in always-on text, scripts that are referenced exist, Python compiles, always-on cap of
  24k chars on OpenCode and 8k on Codex, hooks schema) plus the benchmark's own `validate_tree`. A failing
  proposal is retried once with the validator's message; a second failure inherits the tree it started from, and
  the fallback is recorded.
- **Evidence as files, not prompt text.** Each session gets a `context/` directory (`task.md`, `source/`,
  `rollouts/<label>.md`, `deliverables/<label>/`, `harness/`, `pool/<label>/{harness,harness.diff,rollout.md,produced.md}`,
  `decisions/`, `library/`) and a short `MISSION.md`. Nothing is truncated to fit a prompt.
- **Reproducible stream and resume.** Task order is a seeded shuffle per domain; run directories are namespaced
  by `(benchmark, substrate, mode, k, seed)`; every rollout is fingerprinted by the harness that produced it and
  reused only on a match; a resumed task replays its adopted harness into the library.

### 1.5 The eight components and the two substrates

| Component | OpenCode path | Codex path | Reaches the model |
|---|---|---|---|
| C1 system prompt | `systemprompt.md` | `AGENTS.md` | every call |
| C2 guardrails | `guardrails.md` | (in `AGENTS.md`) | every call |
| C3 memory | `LongTermMEMORY.md` | (in `AGENTS.md`) | every call |
| C4 tool guidance | `tool_descriptions/tool_guidance.md` | (in `AGENTS.md`) | every call |
| C5 subagents | `sub_agents/<n>/AGENT.md` | `sub_agents/<n>/AGENT.md` | the `task` tool |
| C6 skills | `skills/<n>/SKILL.md` (+ `scripts/`) | `.agents/skills/<n>/SKILL.md` | the `skill` tool, on demand |
| C7 scripts | `scripts/<n>.py` | `scripts/<n>.py` | bash |
| C8 plugin / hooks | `plugin/<n>.ts` | `.codex/hooks.json` | inside the tool loop |

The code keeps everything substrate-specific as data on a `SurfaceSpec` (paths, caps, frontmatter rules, stub
READMEs) and everything benchmark-specific in a `BenchSpec` (install mode, leak globs, prompt slots such as
`work`, `inv_levers`, `dom_levers`, `yardstick`, `selection_checks`) plus a `BenchmarkAdapter` protocol
(`tasks`, `domain_of`, `task_prompt`, `task_files`, `execute`, `delivered`, `trajectory`, `deliverable_text`,
`base_harness`, `validate_tree`, `score`). Improver sessions run through a `Proposer` on one of two channels,
OpenCode (run from source through bun) or the Claude CLI, both configured for Vertex AI.

### 1.6 Results, in one table

Gemini 3.5 Flash under OpenCode, means over three runs (site data, `data/results.json`):

| Method | WB Code | WB Web | WB Office | WB Avg | JobBench | LAB Partial |
|---|---|---|---|---|---|---|
| Base harness | 73.4 | 69.9 | 74.0 | 72.4 | 31.5 | 79.9 |
| ReasoningBank | 72.6 | 70.7 | 76.8 | 73.4 | 30.8 | 77.3 |
| Meta-Harness (offline) | 75.5 | 75.1 | 78.6 | 76.4 | 35.8 | 81.6 |
| Harness Scaling | 74.0 | 74.0 | 77.9 | 75.3 | 32.9 | 78.9 |
| Harness² k=1 | 76.5 | 76.4 | 79.5 | 77.5 | 36.0 | 81.9 |
| Harness² k=5 parallel | 80.8 | 78.3 | 83.4 | 80.8 | 39.0 | 84.5 |

Weaker executors gain more (GLM-5 under OpenCode: 64.2 to 75.7 on WB Avg at k=1). The same model can serve as
executor and editor (Figure 8). Skills and scripts carry about half the edits; removing skills from an improved
harness costs the most on Code and Web, removing scripts the most on Office (Table 6).

## 2. How it maps onto MetaHarnessAdapter

| Harness² | MetaHarnessAdapter | Note |
|---|---|---|
| execution harness `H` (8 files) | an adapter's exports: `ADAPTER.md`, `skills/`, `agents/` | no `scripts/` or hooks export today; see 2.3 |
| base harness `H_0` | the template's root (`eval --adapter root`) | |
| improvement harness `R` (library) | Plan A: Evolver `state/`; Plan B: the adapter itself | Plan B makes the library deployable and transferable |
| editor / composer / selector / reflector sessions | an `AgentEvolver`-style sandboxed Codex or Claude Code session, or the run's `llm` tool | MWM already runs native Codex sessions this way |
| reference and probe rollouts | `ctx.tools["run_agent"](task_ids, adapter=candidate)` | candidate must be inside the run directory; training tasks also return the log path |
| trajectory in the `[ACTION]/[OBSERVATION]` grammar | `RunOutput.messages` (react) or the CLI event stream in `log` (codex, claude-code) | renderers needed, see 6.6 |
| deliverable | `RunOutput.output` | plus files the agent wrote, when the task reports them in `extra` |
| `domain_of(task)` | none; tasks are flat | add an optional `domain` task field (6.8) |
| benchmark prompt slots (`work`, `inv_levers`, ...) | `task-info/` texts | a `task-info/harness2.yaml` or `README.md` sections |
| label-free evidence | learning material carries `judge.score` and `feedback` | the evidence builder must strip them in label-free mode and run the tripwire |
| `harness2-judge` after adoption | the Trainer's judge on every rollout; `eval` on test | in Plan B the stream's own rollout scores are the metric |
| run namespace, fingerprinted resume | the Trainer's steps, Git tree, `state/` snapshots | per-task caching inside a step lives in `scratch/` or `state/` |

### 2.1 Mismatch one: training loop vs. online stream

The Trainer runs a batch, writes material, calls `step`, and uses the returned adapter for the *next* batch.
Harness² refines the harness *for the current task* and never carries it forward. Plan A accepts the
training-loop framing (the composed harness is carried, like TTHE does) and gains a deployable trained adapter.
Plan B keeps the paper's framing by moving the task-level recursion into the acting agent, so a "rollout" of the
stream task is the whole refine-and-select procedure and the Evolver only does the harnessing-level update.

### 2.2 Mismatch two: judged material vs. label-free evidence

Every material record has `judge.score` and `judge.feedback`, and `run_agent` returns scores. Harness² must not
see them. The port keeps a switch, `evidence: label_free | judged`. Label-free (the default and the paper's
setting) strips `judge` from every record copied into `context/`, never shows `run_agent`'s scores to a session,
and runs the grader-verdict tripwire on every evidence tree and library entry. Judged mode is an explicit,
documented deviation for tasks where a judge exists and selection by score is wanted (it then competes with GEPA
and SkillOpt on their own terms).

### 2.3 Mismatch three: the component surface

| Component | Representation in an MHA adapter | Reaches the actor through |
|---|---|---|
| C1 to C4 (always-on text) | `harness/systemprompt.md`, `harness/guardrails.md`, `harness/LongTermMEMORY.md`, `harness/tool_guidance.md`, composed into `ADAPTER.md` by the Evolver (as `compose_prompt` does for WorkBuddy) and checked by the template's `check.py` | `ADAPTER.md`: the context block of AGENTS.md / CLAUDE.md, or the react system prompt |
| C6 skills (+ scripts) | `skills/<name>/SKILL.md`, `skills/<name>/scripts/*.py` | `exports.skills`: Codex `.codex/skills`, Claude Code `.claude/skills`, Pi |
| C5 subagents | `agents/<name>.md` (frontmatter `name`, `description`) | `exports.agents`: Codex roles, Claude Code subagents |
| C7 scripts | carried inside skills (`skills/<n>/scripts/`), which the hosts copy whole | bash, as the skill says |
| C8 hooks | not representable today | out of scope for v1; a later `exports.hooks` for Codex `.codex/hooks.json` and Claude Code `settings.json` |

Two consequences to state in the docs: the built-in `react` agent consumes only `ADAPTER.md` and tools, so on
react tasks (OfficeQA) Harness² reduces to the always-on components unless skills are inlined into the context
(a `skills: inline` option in the template renderer); the full surface needs a `codex` or `claude-code` acting
agent, which `examples/visual_showcase` (branch `feat/toolsmith-v2`) already uses.

## 3. Plan A: `harness2` Evolver, execution-harness mode

The first deliverable. One Evolver, one template, no framework change.

### 3.1 Files (all additive)

```text
hermit/algorithms/evolvers/harness2/
  evolver.yaml                      class: evolver.py:Harness2Evolver, needs: [], params below
  evolver.py                        the step: evidence -> propose -> probe -> compose -> probe -> select -> reflect
  core/                             ported from harness2/core, Apache-2.0 (NOTICE kept), trimmed of Vertex/bench code
    surface.py                      SurfaceSpec for the MHA adapter + validate(), autotrigger(), diff_summary(), diversity()
    context.py                      evidence tree builder, leak/verdict tripwires, mission()
    prompts.py                      the six stage prompts, verbatim, with the MHA surface block
    proposer.py                     sessions: agent channel (codex|claude in the sandbox), llm channel (one call)
    library.py                      per-domain library: add(), retain(), rebuild_index(), EXPERIENCE.md
    selection.py                    anonymised blind selection, choice parsing, reflection parsing
    render.py                       trajectory renderers: react messages, codex exec jsonl, claude stream-json
    ledger.py                       R / P accounting per step (an assertion, as upstream)
  README.md
hermit/algorithms/templates/harness2/
  adapter.yaml                      id harness2, version "1.0"; exports: context, skills: skills, agents: agents
  ADAPTER.md                        rendered from harness/ (initially empty sections)
  harness/systemprompt.md  harness/guardrails.md  harness/LongTermMEMORY.md  harness/tool_guidance.md
  skills/README.md  agents/README.md             stubs that name the lever (as upstream's placeholders do)
  check.py                          ADAPTER.md == render(harness/, skills, agents); surface validators
  EVOLVER.md  README.md
docs/methods/harness2.md
examples/officeqa/harness2/smoke.yaml             react actor, llm channel (cheap, context-only surface)
examples/visual_showcase/harness2/run.yaml        codex or claude-code actor, agent channel (full surface), once that task lands
tests/test_harness2_core.py                       surface, validators, tripwires, renderers, parsers (pure, offline)
tests/test_harness2_evolver.py                    end to end on the Trainer with a deterministic proposer and a fake codex
```

Template manifest sketch:

```yaml
template: {id: harness2, version: "1.0", contract: "1", name: "Harness² execution harness"}
exports: {context: ADAPTER.md, skills: skills, agents: agents}
structure:
  learnable:
    - {path: ADAPTER.md, format: {text: true, max_bytes: 60000}}
    - {path: harness/*.md, format: {text: true, max_bytes: 24000}}
    - {path: skills/**, format: {text: true, max_bytes: 40000, max_files: 60}}
    - {path: agents/*.md, format: {text: true, max_bytes: 8000, frontmatter: [name, description]}}
  fixed: [check.py, EVOLVER.md, README.md]
check: check.py:check
```

`check.py` is the port of `tree.validate` plus the render identity: it rejects a skill without `description:`,
a skill never named in the always-on text, a referenced `scripts/*.py` that does not ship, a helper that does not
compile, always-on text over the cap, grader-verdict markers, and an `ADAPTER.md` that is not the rendering of
`harness/` + the skill and subagent index. The Trainer's repair then restores the offending file from the parent,
exactly as for any template.

### 3.2 The step

Inputs: the step's `B = data.step_size` rollouts of the current adapter (the reference set, `k = 1` per task,
read as a set), the task texts, the library in `state/library/<domain>/`, the adapter working copy.

1. **Evidence.** Build `scratch/context/` from `learning_material/current/`: `task.md` per task (or one
   `tasks/` directory for a batch), `rollouts/<task>.md` rendered in the shared grammar, `deliverables/<task>/`,
   `harness/` (the working copy's component tree), `library/` (the domain's entries), `decisions/` (earlier
   steps' notes from `state/`). Strip `judge` fields in label-free mode; run the tripwire.
2. **Propose.** Two sessions, `inv` and `dom`, each in a copy of the evidence directory, with the stage prompt
   and the MHA surface block. Harvest `context/harness/` as the candidate; validate; retry once with the message;
   fall back to the current tree. Record `<notes>`.
3. **Probe.** `run_agent(task_ids=<the batch>, adapter=scratch/candidates/<label>)` for each candidate that
   changed. Render the probe trajectories and deliverables into `context/pool/<label>/` with `harness.diff`.
4. **Compose.** One session (`agg1`) over the reference set and the pool; harvest, validate, retry, fall back.
   Probe the composed tree on the batch.
5. **Select** what the next step carries. Default `select: blind`: the selector session compares the
   anonymised deliverables of `{base, inv, dom, composed}` per task and the carried tree is the one whose
   deliverables win most tasks (ties favour the composed, then base). `select: judge` (judged mode only) carries
   the tree with the best mean score. `select: composed` always carries the composition (the paper at `k = 1`).
6. **Reflect.** One session over the full step record; parse `<edits>` and `<experience>`; add the carried tree
   to `state/library/<domain>/harnesses/step_NNNN/` only when an experience entry exists; rebuild `INDEX.md`.
7. **Hand back.** Write the carried tree into `ctx.workspace.adapter`, render `ADAPTER.md`, call `check_spec`,
   write `output/report.md` (decisions, ledger, dropped candidates) and `state/harness2.json` (one record per
   step: labels, notes, fallbacks, usage). Return `StepResult("done", summary, usage=...)`.

Cost per step at `k = 1`: `3B` extra rollouts (two probes and the composed run on `B` tasks) and 3 sessions, plus
one selector and one reflector session. Parallel `k > 1` multiplies candidates and compositions as upstream;
sequential mode chains rounds inside one step. Both keep the `R = 4k`, `P = 3k` identity per batch.

`bootstrap` is not used: the first step's rollouts are the first reference set. `finalize` returns the current
adapter; with `select: judge` it may return the library entry with the best recorded probe mean, as GEPA does.

### 3.3 Parameters (`evolver.yaml`)

```yaml
params:
  mode: exec                 # exec (Plan A) | library (Plan B)
  k: 1                       # task-level recursion budget
  recursion: parallel        # parallel | sequential
  channel: agent             # agent (a sandboxed codex|claude session per stage) | llm (one metered call per stage)
  backend: codex             # agent channel: codex | claude; model, args, env, home, price as for the codex Evolver
  model: null
  evidence: label_free       # label_free | judged
  select: blind              # blind | judge | composed
  domain_field: domain       # the task field naming the library domain; missing -> "default"
  always_on_max: 24000       # the always-on cap the validators enforce
  obs_trunc: 50000           # per-observation clip in rendered trajectories (upstream's default, min 10000)
  stage_attempts: 2          # validation retries per stage, as upstream
  propose_workers: 2         # concurrent sessions in a phase
  session_timeout_s: 3600
  hide: null                 # paths hidden from the sessions besides the framework's examples, tests, docs, .git
  prompts: null              # a directory overriding the stage prompts (task-specific slots stay in task-info)
```

Task-specific prompt slots (`work`, `domain_word`, `deliverable_word`, `inv_levers`, `dom_levers`, `yardstick`,
`selection_checks`) come from `task-info/harness2.yaml` when present; the Evolver refuses slot text that names a
benchmark, grader, rubric or score, as `BenchPrompts` does.

### 3.4 Config example (OfficeQA, cheap smoke)

```yaml
template: harness2@1.0
evolver:
  name: harness2
  params: {k: 1, channel: llm, evidence: label_free, select: blind}
llm: fireworks/accounts/fireworks/models/glm-5p3-flash
data: {tasks: ../utils/tasks.jsonl, train: ../utils/splits/smoke_train.txt, test: ../utils/splits/smoke_test.txt, step_size: 2}
agent: {name: react, params: {prompt: ../utils/agent/prompt.md, tools: ../utils/tools.py, user: "{question}"}}
judge: {path: ../utils/judge.py}
trainer: {run_name: harness2-smoke, epochs: 1, budget: {usd: 5, hours: 2}, call_timeout_min: 30}
```

With `channel: agent` the sessions run under the sandbox exactly like the `codex` Evolver: the evidence
directory and the candidate tree are the only writable paths, the Trainer's tool endpoint is reachable through a
`ToolGate` that exposes `check_spec` only, and the model's endpoint comes from the backend binding.

### 3.5 Deviations from the paper, stated up front

- The composed harness is carried to the next batch; the paper resets to `H_0` per task. The library still
  records only evidence-backed entries, so a bad carry is visible in the record and `start_from` can branch.
- Reference executions are one per task over a batch, not `k` per task. Divergence across tasks replaces
  divergence across replicates; the prompts' "read the base set as a set" wording still applies.
- C8 hooks are absent; C7 scripts live inside skills.
- Selection compares deliverables of the same candidate across a batch rather than finals of one task.

### 3.6 Tests

- `test_harness2_core.py`: the validators and autotrigger on hand-written trees; the tripwire on a tree that
  contains `"pass_rate"`; the three renderers on captured fixtures (a react JSON log, a codex `exec --json`
  stream, a claude `stream-json` stream); choice and experience parsing.
- `test_harness2_evolver.py`: the Trainer on the toy task of `tests/test_training.py` with a deterministic
  proposer (the port of upstream's `DemoProposer`: it edits a known file and always picks the correct total) and
  with a fake `codex` script as in `tests/test_sandboxed_evolvers.py`. Asserts: the ledger identity per step, a
  rejected proposal retried once with feedback, `KEEP_BASE` leaving the adapter unchanged, `NO_ENTRY` keeping a
  task out of the library, no `judge` text anywhere under `scratch/context/` in label-free mode, and the library
  entry visible to the next step's evidence.

## 4. Plan B: the online stream

The paper's protocol: each task is refined from `H_0`, a deliverable is chosen blindly, and only the library
persists. Two additive pieces on top of Plan A's core.

### 4.1 `Harness2Agent`: task-level recursion as an acting agent

An `Agent` subclass, selected as `agent.name: hermit.algorithms.evolvers.harness2.agent:Harness2Agent`
(no change to `agents/base.py`; a `harness2` alias can be added later). Its `run(task, adapter)`:

1. Takes `adapter` as the **library adapter** (template `harness2-library`, below) and the base execution
   harness `H_0` from `params.base` (a directory in the `harness2` template's layout) or from the library's
   `base/` directory.
2. Runs the inner actor, `params.inner: {name: codex | claude-code | react, params: {...}}`, through
   `load_agent`, `k` times with `H_0` rendered as an MHA adapter in a scratch directory: the reference set.
3. Proposes, probes, composes and probes again with the Plan A core, with the library read from the adapter
   (`library/<domain>/`), all under a per-task work directory `<logs>/<task id>/` that keeps every rollout,
   candidate tree, decision and the ledger.
4. Selects one deliverable blindly among the `k` finals (at `k = 1` the single final), and returns
   `RunOutput(output=<selected deliverable>, log=<work dir>, cost_usd=<everything spent>, tokens=...,
   extra={"harness2": {"domain", "adopted": <path to the adopted tree>, "chosen_label", "why", "record": <ξ>,
   "ledger"}})`.

The inner rollouts and the sessions are the task's cost; `run_agent` is not needed. Sessions reuse the agent
channel (sandboxed codex or claude) or the llm channel. The judge then scores the selected deliverable as for any
rollout: with `data.shuffle: false` and `step_size: 1`, `steps.jsonl` holds the per-task scores of the adopted
outputs over the stream, which is the paper's measurement.

### 4.2 `Harness2Evolver` in `mode: library`: harnessing-level recursion

With `mode: library`, `step` reads the step's `extra["harness2"]` records (one per task, as MWM reads
`extra["mwm"]`), runs the reflector over each task's record (or reads a reflection the agent already produced,
when `params.reflect_in_agent: true` keeps the whole task on the acting side), and writes the adopted tree plus
`EXPERIENCE.md` into the adapter's `library/<domain>/harnesses/<task>/`, rebuilding `INDEX.md`. The Trainer
commits it; the next task's `Harness2Agent` reads it from the exported rollout adapter. Nothing else in the
adapter changes.

Template `harness2-library@1.0`:

```yaml
exports: {context: ADAPTER.md}                      # ADAPTER.md: a short index of the library, for inspection
structure:
  learnable:
    - {path: ADAPTER.md, format: {text: true, max_bytes: 20000}}
    - {path: library/**, format: {max_files: 2000}}  # adopted trees carry Python helpers: not text-only
  fixed: [base, check.py, EVOLVER.md, README.md]     # base/: H_0; check.py: every entry validates, no verdict markers
check: check.py:check
```

Because the library is the adapter: `eval --adapter final` on a held-out stream measures *library transfer*
(Figure 7 right); `start_from=<run>/final` on another task is the cross-benchmark transfer experiment; the Git
tree shows exactly which task added which entry; `deploy` is meaningless for this template and `check.py` says
so.

### 4.3 Recipe and cost

```yaml
template: harness2-library@1.0
agent:
  name: hermit.algorithms.evolvers.harness2.agent:Harness2Agent
  concurrency: 1                                     # tasks of a domain must stay in order; see 6.8 for domain lanes
  params:
    k: 1
    recursion: parallel
    inner: {name: codex, params: {model: fireworks/accounts/fireworks/models/glm-5p3-flash, prompt: ../utils/agent/prompt.md}}
    editor: {backend: claude, model: anthropic/claude-opus-4-8}      # the paper: a stronger editor is helpful, not required
    base: ../utils/base_harness
    logs: .logs/harness2
evolver: {name: harness2, params: {mode: library}}
data: {step_size: 1, shuffle: false, ...}
```

Per task at `k = 1`: 4 inner rollouts, 3 editor sessions, 1 reflection; at `k = 5` parallel: 20 rollouts, 15
sessions, 1 selection, 1 reflection. The paper's `k = 5` run is where most of the gain over `k = 1` appears, and
also what lifts later tasks' refinement (library accumulated at `k = 5` transfers better even without probing).

### 4.4 Tests

The MWM test pattern: a fake inner agent (a script that writes a deliverable depending on which harness text it
received) and a fake editor; assert that each task starts from `H_0`, that the library grows by one entry per
task with an experience entry, that task `t+1`'s evidence contains task `t`'s entry, that `extra["harness2"]`
reaches the Evolver, and that the per-task ledger holds `R = 4k`, `P = 3k`.

## 5. Plan C: vendor `google-research/harness2` with shims

Add `harness2 @ git+https://github.com/google-research/harness2@1cfe254` as an optional extra (`.[harness2]`,
Python 3.12 only; upstream pins `>=3.12,<3.14` while the framework supports 3.11) and write three shims:

- an MHA `BenchmarkAdapter`: `tasks()` from `TaskData`, `domain_of()` from the task field, `task_prompt()` from
  the task's prompt field, `execute()` running the task's pipeline with the harness tree deployed as an adapter
  and a run directory per `run_id`, `trajectory()` through the renderers of 6.6, `deliverable_text()` from
  `RunOutput.output`, `score()` through the judge;
- a `Substrate` whose `SurfaceSpec` is the MHA adapter layout (always-on `harness/*.md`, skills under `skills/`,
  subagents under `agents/`) and whose `materialise()` writes an adapter directory;
- a `Proposer` channel that runs upstream's sessions through the framework's sandboxed Codex or Claude Code
  launch instead of OpenCode-from-source on Vertex.

Then `harness2.core.schedule.run_domain` drives a stream and `harness2.core.judge` scores selections, with an
`Evolver` of a few lines that only stores the library.

When it is the right choice: a reproduction of the paper's numbers on JobBench, LAB or WorkBuddy-Bench, where
upstream's benchmark adapters, patches and judges are the hard part and already exist (they need Docker for
WorkBuddy, Vertex credentials, Node and bun; none of it runs on this machine today).

Why it is not the foundation: `harness2.config` loads `.env` at import and is shaped around Vertex, OpenCode
and three benchmarks; `Proposer` has exactly two channels and its concurrency is module state; the surface,
install modes and leak globs are benchmark values; the stream, namespace and resume logic duplicates what the
Trainer, Git tree and `state/` snapshots already do; and none of it can be reused by ACE or Continual Harness.
The pieces worth taking are `prompts.py`, `tree.py`'s validators, `context.py`'s tripwires and evidence layout,
`selection.py`'s parsing and `ledger.py`, which Section 6 ports.

## 6. The shared core (what every plan needs)

Ported into `hermit/algorithms/evolvers/harness2/core/` first, with upstream's Apache-2.0 header and a
`NOTICE` line, so that the branch stays additive while `feat/toolsmith-v2` is in flight. Candidates for promotion
to framework level once a second method uses them: 6.2, 6.5, 6.6 and 6.8.

### 6.1 Surface and validation (`surface.py`)

A `SurfaceSpec` for the MHA adapter (`always_on = harness/{systemprompt,guardrails,LongTermMEMORY,tool_guidance}.md`,
`skill_dir = skills`, subagent dir `agents` with one-file roles, `plugin_dir = None`, `hooks_file = None`,
stubs naming each lever) and the port of `read_tree`, `write_tree`, `validate`, `autotrigger`, `diff_summary`,
`diversity`. The template's `check.py` calls `validate` so the Trainer's repair and the Evolver's retry agree.

### 6.2 Evidence (`context.py`)

`build()` from a list of material records or `run_agent` results: the `context/` layout of Section 1.4, the
`mission()` pointer, `assert_no_leak_paths` with the task's `leak_globs` (from `task-info/harness2.yaml`), and
`assert_no_grader_verdicts` with the upstream markers plus the framework's own (`"score":`, `"feedback":`,
`check_feedback`). In label-free mode the builder copies records through a filter that drops `judge`,
`pipeline_extra.score`-like fields and the judge's feedback text.

### 6.3 Prompts (`prompts.py`)

The six stage prompts verbatim (Appendix H), the `GENERALIZE`, `ANSWER_FREE` and `EVIDENCE_BOUNDARY` blocks, a
`_SURFACE_MHA` block replacing the OpenCode and Codex tables (paths of 2.3, the react caveat when the actor is
react), and the task slots from `task-info/harness2.yaml`. Overridable by `params.prompts`.

### 6.4 Sessions (`proposer.py`)

Two channels behind one `call(mission, context_dir, label, harvest=...) -> Reply`:

- `agent`: a sandboxed `codex exec` or `claude -p` run on a copy of the evidence directory (cwd), MISSION.md
  as the prompt, `check_spec` as the only Trainer tool through a `ToolGate`, the harvested `context/harness/`
  as the proposal, usage from `parse_log`, cost from `price_usage`. This is `AgentEvolver._step` with a different
  cwd and prompt; the launch code should be shared, not copied (a small `launch(cmd, env, cwd)` helper in
  `evolver/agent.py` is the one core touch worth making; until then the Evolver builds the command as
  `MwmEvolver._command` does).
- `llm`: one metered `ctx.tools["llm"]` call with the evidence inlined under a size budget and files returned
  in `<<<FILE path ... FILE>>>` blocks (the prompt-only convention, extended to many files). For cheap models and
  for offline tests.

Caching by content fingerprint (label, mission, evidence) under `state/sessions/` so a resumed step does not
pay twice, as upstream's audit cache does.

### 6.5 Library (`library.py`)

The port of upstream's `Library`: `add()` refuses an entry without `cue`, `lesson`, `evidence`; writes the full
tree (not a summary) and `EXPERIENCE.md`; `rebuild_index()`; `retain()` for resumes. Root: `state/library/` in
Plan A, `<adapter>/library/` in Plan B. Keys: the domain, then the task id (Plan B) or `step_NNNN` (Plan A).

### 6.6 Trajectory renderers (`render.py`)

`[ACTION] name(args)` / `[OBSERVATION]` / `[OBSERVATION ERROR]` / `[ASSISTANT]` / `[THINKING]` / `[ERROR]` lines,
with per-observation clipping and an explicit truncation marker, from: the react agent's `messages`; a Codex
`exec --json` stream (port of `substrate/codex.py:render_events`); a Claude Code `stream-json` stream (new,
small). The `log` path of a material record or `run_agent` result selects the renderer by content.

### 6.7 Selection and reflection parsing (`selection.py`)

`assign_ids` (seeded shuffle), ```` ```choice ```` and `<why>` parsing with the neutral fallback (a delivered base,
else the first final), `<edits>` and `<experience>` parsing with `NO_ENTRY`, and the ledger assertion.

### 6.8 Domains and ordering (framework-level, small)

- Task field `domain` (configurable name) read by the Evolver and the agent; absent means one domain.
- For Plan B with several domains, `agent.concurrency` must be 1 today because the library of a domain must be
  threaded in order. A later Trainer option, `data.lanes: domain`, could run domains concurrently and tasks
  within a domain in order, as upstream's `--workers` does. Not needed for the first version.

## 7. Touch points outside the new package, and the merge strategy

The branch `feat/toolsmith-v2` (local to a sibling checkout, not pushed) changes `evolver/agent.py`,
`trainer/trainer.py`, `sandbox.py`, `settings_check.py`, `cli.py`, `config.py`, `defaults.yaml`, `loader.py`,
`backends.py` and adds a `toolsmith` template and Evolver plus `examples/visual_showcase`. To keep both mergeable:

- Plans A and B add files only: the Evolver package, two templates, one methods page, example configs, tests.
- The two small core edits worth making are deferred until toolsmith-v2 lands: the shared `launch()` helper in
  `evolver/agent.py` (6.4) and a `harness2` alias in `agents/base.py:builtin`.
- `README.md`: move Harness² from *Planned support* to *Supported algorithms* in the final PR, one row.
- `docs/index.md` and `docs/methods/index.md`: one line each, in the final PR.

Convergence worth noting for later: toolsmith's learnable `tools/` (programs with their own tests, admitted
after a sandboxed test, as MWM does) is the natural home for Harness²'s C7 scripts once both exist; and
Harness²'s evidence tree, tripwire and library are reusable by ACE (reflector–curator over a playbook) and
Continual Harness (windowed trajectory refinement), the other two planned integrations.

## 8. Milestones and verification

| Milestone | Deliverable | Verified by |
|---|---|---|
| M1 core | 6.1 to 6.7 as pure modules | `tests/test_harness2_core.py`, offline |
| M2 template | `harness2@1.0` with `check.py`; `deploy --host codex` and `claude-code` of a hand-written tree shows skills and roles | `tests/test_harness2_template.py`, `check_spec` on the root |
| M3 Plan A | `Harness2Evolver` `mode: exec`, llm and agent channels | the toy task end to end with a deterministic proposer and a fake codex; ledger identity; label-free assertion |
| M4 smoke | OfficeQA smoke with the react actor and a local vLLM model on this machine's L40s (the `MHA-local-llm` setup), then with a codex actor on `visual_showcase` when it merges | `steps.jsonl`, `report.md`, the library under `state/`; `eval --adapter none|root|final` |
| M5 Plan B | `Harness2Agent`, `harness2-library@1.0`, `mode: library` | MWM-style fake-agent test; a 10-task ordered stream smoke; `start_from` transfer to a second stream |
| M6 docs | `docs/methods/harness2.md`, README row, recipe page | docs build |

What a smoke run must show before any claim: the base set rendered without judge text, two candidates with
diffs, at least one `KEEP_BASE` or fallback handled without a crash, a reflection with an `<experience>` entry
in the library, and the next step's evidence listing it. Performance claims need the usual protocol: fixed
actor, repeats, `none` and `root` baselines, costs recorded.

## 9. Open questions for the team

1. Which setting matters first: a trained execution harness for the visual showcase and OfficeQA (Plan A), or
   the online stream with library transfer (Plan B)? The recommendation above assumes A then B.
2. Is a judged variant wanted at all, or should the Evolver stay strictly label-free so that comparisons with
   GEPA and SkillOpt are read as "without labels" versus "with labels"?
3. Should skills be inlined into `ADAPTER.md` for the react actor, or should the react agent gain a `skill`
   tool so the on-demand surface exists there too?
4. Should the library's Python helpers be admitted only after a sandboxed self-test, as MWM and toolsmith do,
   or is upstream's compile check enough for v1?
5. Hooks (C8): wait for an `exports.hooks` in the hosts, or leave them out of the method permanently?

## Appendix: references and licences

- Paper: Xu et al., *Harness²: Recursive Agent Harnessing for an Open World*, arXiv:2610.14224 (2026-10-09).
- Code: google-research/harness2 at `1cfe254` (Apache-2.0). Files ported in Section 6 keep the upstream
  copyright header; a `NOTICE` entry is added to the package. The release's benchmark runners under
  `third_party/` are not ported.
- Related methods the framework already has: MWM (`mwm`), which shares the "native agent session over a
  mounted evidence store" pattern; GEPA and SkillOpt, which share the `run_agent` candidate-probing pattern.
