# ASPIRE

ASPIRE ([Agentic Skills Discovery for Robotics, arXiv:2607.00272](https://arxiv.org/abs/2607.00272),
[code](https://github.com/NVlabs/ASPIRE)) learns robot skills as text. A coordinator agent sends one coding agent (a
fix-loop subagent, the actor) to each robot task. The actor writes a Code-as-Policy program, runs it in a robot
execution engine that records every primitive call with RGB keyframes, debugs it on development seeds, and reports
what it learned. The coordinator turns those reports into a shared skill library: it repairs entries the actors graded
badly and admits a new entry only after a fresh verifier re-solved the task from the library and the entry alone. It
also chooses which task runs next (its curriculum). In ASPIRE all three roles are Claude Code sessions; here they are
Codex sessions (or Claude Code or Pi), run by the framework's coding-agent agents with ASPIRE's own prompts. The framework
learns only the library: the actor's harness and prompts are fixed.

## Setup

```yaml
template: aspire                              # the skill library: skills/README.md and skills/<topic>.md
data: {train: all.txt, val: verify.txt, test: rerun.txt, step_size: 1, shuffle: false}
agent:
  base: codex                                 # the actor: ASPIRE's fix-loop subagent prompt, verbatim
  params: {prompt: ../utils/agent/task.py:prompt, prepare: ../utils/agent/task.py:prepare,
           answer: ../utils/agent/task.py:answer, collect: ../utils/agent/task.py:collect}
  codex: {codex_sandbox: danger-full-access, shell_env: all, web_search: false}   # the task sandbox is the boundary
environment:                                  # the task sandbox: its code, ASPIRE's prompts and docs, the service
  read: [../utils/agent, $ASPIRE_DOCS, ...]
  env: [ASPIRE_SIM_URL]
judge: {path: ../utils/judge.py}              # on the host: Stage 2
sandbox: {enabled: true, enforce: required}
evolver:
  name: aspire                                # the coordinator: a Codex session with ASPIRE's guide
  base: codex
  model: {ref: fireworks/accounts/fireworks/models/glm-5p3-flash, effort: medium}
```

The template is ASPIRE's library layout: `skills/README.md` (the index) and topic files `skills/<topic>.md` of
entries `## <title>`, each with **Failure signature.**, **When to apply.**, a repair (prose and, when useful, one
snippet) and **Provenance.** The acting agent receives `ADAPTER.md` and below it the whole library, rendered by the
template's context tool (files as context: the paper has no retrieval ranking); the files are also deployed into its
workspace. The template's check parses every snippet under the program sandbox's rules, refuses entries that name
privileged access (simulator internals, success predicates, reflection), and refuses a staged `skills/proposed.md`.
No existing template fits: `prompt-only` is one file; `continual-harness` carries a policy, sub-agents and memory
ASPIRE does not have; `reharness` retrieves per instance from an embedded bank.

The example's task functions (`examples/aspire_robosuite/utils/agent/task.py`) give the actor its workspace in the
task sandbox: `bin/sim`; ASPIRE's API and artifact docs and the benchmark's page (Robosuite's, or LIBERO's); the scene-snapshot program; and a page that maps
the template's paths and commands onto the workspace. The sandbox shows nothing else of ASPIRE's checkout: not its
shipped library, worked solutions or examples. It has no GPU, so the simulator runs on the host as a service
(`utils/agent/sim_service.py`), which `bin/sim` reaches on a forwarded loopback port; the session registers there
before the agent starts (its workspace and task id: one session per rollout), and the service runs only the seeds of
the task row it reads on the host, the unseen check once and a verifier's two programs. The judge (`utils/judge.py`,
on the host) runs the selected program once on the held-out seeds: ASPIRE's Stage 2, the coordinator's in ASPIRE. The
verifier is the same actor on the task's verifier row (`verify-<task>`, `data.val`): ASPIRE's verifier prompt, five
seeds, two attempts, scored on its own seeds with its own verdict.

## The step

One step is one finished task.

```{mermaid}
flowchart TD
    A[actor in the task sandbox: Codex with ASPIRE's fix-loop prompt; bin/sim to the host's simulator service] --> S2[judge on the host: the selected program once on the held-out seeds]
    S2 --> R[the step's record: findings, skill report, the selected program, Stage 2 score]
    R --> C[coordinator: Codex with ASPIRE's guide, sections 6, 7 and Curriculum]
    L[the library] --> C
    C -->|repairs on graded entries| L2[the library in the adapter]
    C -->|proposals in a copy's skills/proposed.md| V[verifier: run_agent on verify-&lt;task&gt;]
    V -->|score >= 0.8| P[promote into the topic files]
    P --> L2
    C -->|next_tasks.json| N[StepResult.next_tasks: the next step's task]
    L2 --> K{template check}
    K -->|fails| F[the Trainer repairs the failing files]
```

1. The Trainer runs the actor on the step's task, with the current library. The actor follows ASPIRE's fix-loop
   prompt: it explores the scene once, writes `v00`, runs every version on all development seeds through `bin/sim`,
   groups failures by mode and repairs the largest, selects `fix_code.py`, runs it once on the unseen-check seeds, and
   writes `findings.md` and `skill_report.json`. When `fix_code.py` passes fewer than 90% of the development seeds, the
   same session goes on with ASPIRE's evolutionary-search template (§2.3: eight candidates per round from the top
   three, until 80% or five rounds; its best program replaces `fix_code.py` only if it beats it). The judge then runs
   the selected program once on the held-out seeds, on the host; that rate is the task's score.
2. The coordinator, a Codex session (`hermit/agent/evolver/methods/aspire/COORDINATOR.md`: a page that maps the guide
   onto the harness, then ASPIRE's own sections 6, 7 and Curriculum verbatim), reads the record, acts on the grades
   (repairs `wrong` and `misleading` entries, adds evidence to `useful` ones), stages the proposals in a copy of the
   library and calls `run_agent` on the task's verifier row, promotes what a verified run supports, and calls
   `check_spec`.
3. It records its curriculum round in `state/curriculum.md` and names the next task in `output/next_tasks.json`; the
   Evolver declares `chooses_tasks` and returns it as `StepResult.next_tasks`
   ([Choosing the next tasks](../framework/evolver.md#choosing-the-next-tasks)).
4. The Trainer checks the library with the template's check and repairs what fails.

The final adapter is the library after the last task. ASPIRE's Phase 3, one rerun of the tasks with the final library,
is the evaluation on the test split (`rerun-<task>` rows, the same seeds), compared with each task's first pass.

## Parameters

```{literalinclude} ../../hermit/agent/evolver/methods/aspire/evolver.yaml
:language: yaml
```

The Codex actor's settings (`agent.params`) are `aspire_root`, `aspire_docs` (the docs as the launcher stages them) and `max_steps`; its seeds are the task rows'
(`utils/prepare.py`, which the launcher runs: `ASPIRE_EVAL_SEEDS` for the held-out ones), and the simulator's
environment and workers are the simulator service's (`ASPIRE_SIM_ENV`). The direct comparison actor
(`run_direct.yaml`) reads `debug_seeds`, `workers`, `aspire_root` and `sim_env` from `agent.params`, a task row's own
seeds winning; the held-out seeds are the task rows' for both actors (the judge reads them there). The verifier's rows
carry their own seeds.

## The authors' implementation against this one

| Dimension | ASPIRE (NVlabs/ASPIRE) | This port | Why |
|---|---|---|---|
| Roles | coordinator, fix-loop subagents and verifiers, all Claude Code | the same three roles, Codex (or Claude Code or Pi) sessions with ASPIRE's prompts | — |
| When learning happens | after each subagent returns | after each task (one per step) | the Trainer's step |
| Curriculum | the coordinator chooses each round's tasks and logs `curriculum.md` | the same, one task per step, through `StepResult.next_tasks` | the Trainer dispatches what the coordinator names |
| Parallelism | one task per GPU | one task at a time | a decision: two GPUs |
| Stage 2 (held-out) | the coordinator runs it after the subagent returns | the judge runs it on the host, on the selected program | the score is the judge's; no session can run held-out seeds |
| What the actor sees | the repository: library, docs, scripts, its own runs (and, against the rules, the shipped skills and worked examples) | the task sandbox: its workspace, the library, ASPIRE's prompts, docs and snapshot program; not the shipped library, worked solutions, examples or other workspaces | the reading rules enforced by the sandbox |
| Running episodes | `cap-harness run-batch` on the GPU, in the repository | `bin/sim`, to the simulator service on the host, which holds the session's seeds, the unseen check once and a verifier's two programs | the task sandbox has no GPU; the seed rules held by the trusted side |
| The update | promotion after a verified reproduction; repairs on grades | the same, by the coordinator session | — |
| Verification | 5 seeds (the template's 51-55), 2 attempts, `verified` at ≥ 4/5; `void` never promoted | the same budget on 101-105 for Robosuite and on the template's 51-55 for LIBERO (two programs, held by `bin/sim`); the score counts only with the verifier's own `verified` and its selected program at ≥ 4/5 on rerun | the template's seeds are LIBERO's; ASPIRE says to change them for Robosuite |
| Lint | `lint_skills.py` after every promotion | `check_spec` with the template's check (sandbox validity and privileged access in ```python snippets, the library's size) | the upstream linter imports ASPIRE |
| Scene exploration | `skills/task-exploration.md` in the shipped library | the same file, given as `docs/task-exploration.md` (the procedure, not learned skills) | the empty library has no such file |
| Stage 2 evidence | the coordinator runs it and keeps the failures out of development | the judge runs it on the host; the coordinator, in its own sandbox, sees only the score | ASPIRE's rule: evaluation failures are not development evidence |
| Library history | `<file>-changelog.md` beside each file | `state/changelog.md` | the adapter holds only the library |
| Phase 3 | rerun of tasks below 80% whose library changed | the test split reruns every task | the evaluation measures it per task |
| Models | Claude Opus 4.6 (and others) | GLM 5.3 Flash in every role | the first-evaluation row of the framework |
| Perception | SAM3 | SAM3, or any service with its wire format (`utils/seg_service/`: Grounding-DINO + SAM) | SAM3's weights are gated |
| Evolutionary search (§2.3) | a separate evosearch subagent for tasks the fix loop leaves below 90%: K = 8 candidates per round from the top 3, stop at 80% or 5 rounds, Stage 2 on its best | the same template, verbatim, as Part 2 of the actor's session; the judge runs Stage 2 on `evosearch_best_code.py` | a decision: one session keeps the task's rollout one call |
| Not ported | — | BEHAVIOR-1K, the real robot | scope |

`examples/aspire_robosuite/aspire/run_direct.yaml` replaces the Codex actor with a structured loop of `direct` calls
(`utils/agent/agent.py`), which sees a digest of each run instead of the files: a comparison, not the method.

## What it is not

A verified entry transmits (a fresh actor followed it to the fix on the task it came from); it is not shown to
generalize. The actor's simulator runs outside its sandbox, on the host: the service starts only ASPIRE's harness, on
the session's own programs and seeds. The reruns score the held-out seeds the first pass was scored on (as ASPIRE's
Phase 3 does).

## Example

[`examples/aspire_robosuite/`](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/aspire_robosuite): the
seven Robosuite tasks of ASPIRE's harness.
[`examples/aspire_libero/`](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/aspire_libero): LIBERO's
five suites on the same engine.
[`examples/aspire_libero_pro/`](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/aspire_libero_pro): the
eighty LIBERO-Pro tasks ASPIRE's harness requires.
