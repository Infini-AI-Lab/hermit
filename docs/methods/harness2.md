# Harness²

Harness² ([Xu et al., 2026](https://arxiv.org/abs/2610.14224), [code](https://github.com/google-research/harness2))
refines an agent's harness without labels, one task at a time: for each task a contrastive Propose–Probe–Compose
round edits a fresh copy of the base harness, the composed harness's execution is the task's deliverable, and a
per-domain library of adopted harnesses with their lessons is the only thing carried to the next task. No judge
score, judge feedback or reference answer reaches any session, so the method needs no training or validation split:
the test tasks are processed as one stream.

## Setup

```yaml
template: harness2-codex@1.1
data: {train: stream.txt, step_size: 1, shuffle: false}   # one task per step, in the stream's order
trainer: {epochs: 1}
agent: {name: codex}                                             # Codex with the adapter deployed in its workspace
evolver:
  name: harness2_upstream
  params: {model: <backend>/<model>, effort: medium, k: 1, slots: slots.yaml}
```

From the checkout root with pip 25.1+ (see [Installation](../get-started/installation.md)), install the upstream package, pinned, with `python -m pip install -e . --group harness2`; it supports Python 3.12 and 3.13 only. `slots.yaml` gives the
task-shaped text upstream's stage prompts interpolate: `work`, `domain_word`, `deliverable_word`, `inv_levers`,
`dom_levers`, `yardstick`, `domain_guidance`, `selection_checks`. They describe the work, never its measurement:
upstream refuses the words benchmark, evaluation, grader, rubric and score there. Spell out the deliverable's exact
shape in `work`: the stage prompts keep a deliverable's shape fixed, but only the shape the work names.

## The paper's protocol, by the upstream code

`harness2_upstream` runs Harness² as the paper does, with the paper's own code: the upstream package drives every
task through its benchmark seam. Each step hands the step's task to `harness2.core.schedule.run_task`:

1. the reference execution is the Trainer's rollout of the starting adapter H0 on this task;
2. an INV editor session (how the agent works: tool use, procedure, completion) and a DOM editor session (what a
   practitioner knows) each edit a copy of H0; each candidate is executed on the same task through `run_agent`, which
   runs no training row that is a step of a multi-step Harbor task: given one, the step fails, saying why, before any
   editor session runs, and a stream of them trains with a method that learns from the Trainer's own rollouts
   ([Known limitations](../reference/compatibility.md#known-limitations));
3. a composer session reads the reference and both probes and writes one harness; its execution on the same task
   is the task's deliverable (at `k > 1`, k compositions and a blind selection among their deliverables);
4. a reflection writes a verdict per changed component and at most one transferable lesson; only a task with a
   lesson enters the domain's library, with its full harness and an `EXPERIENCE.md`.

The adapter is never written, so every task starts from H0; the library lives in the Training State. The editor
sessions are upstream's `claude_cli` channel (Claude Code on the bound model, which must serve the Messages API, or
on your subscription with `evolver.params.login: claude` and a model name; with `evolver.base: pi`, Pi on the bound
model), each in a sandbox that shows only its evidence and reaches only the model. A task's score is the judge's score of
its deliverable, in `step_NNNN/step_result.json` (`extra.harness2`), never seen by a session. A deliverable whose
execution was not the agent's result (status `infra`: the machine, the provider) has no grade: its task is then a row
the stream leaves unscored (`Evolver.step_scores` maps it to `None`), not H0's score. At `k = 1` a task costs four
executions and four sessions (two editors, the composer, the reflector).

## Codex as the acting agent

The `harness2-codex` template is the paper's Codex harness as an adapter, in the layout Codex reads from its
workspace root: `AGENTS.md` (the one always-on file, at most 8,000 characters), `.agents/skills/<name>/SKILL.md`
with the helpers under its `scripts/`, `scripts/<name>.py`, `sub_agents/<name>/AGENT.md` and `.codex/hooks.json`.
`ADAPTER.md` is kept equal to `AGENTS.md`; the template's `.hermit/check.py` restates upstream's validator (a trigger line in
`AGENTS.md` for every skill, scripts that compile, a helper a skill invokes travels with it, the hook schema, the
grader-verdict tripwire). The editor sessions see upstream's Codex surface text, so skills on demand, scripts,
subagents and hooks are all levers.

An acting agent deploys the tree as `hermit deploy` does: the template's `exports.yaml` declares the skills, the
subagents and the hooks, so they are connected to Codex's own places (`.codex/skills/`, `.codex/agents/`,
`.codex/hooks.json`, each hook's command run from the workspace's copy of the tree under `hermit/harness2-codex/`),
`AGENTS.md` holds `ADAPTER.md` (the tree's `AGENTS.md`) and names the rest of the copy, `scripts/` included, and the
session runs with `--dangerously-bypass-hook-trust` so the hooks fire (a user of `hermit deploy` trusts them before
they run). Upstream's `codex_root` install put the tree at the workspace root instead; the paths in `AGENTS.md` are
now relative to `hermit/harness2-codex/`, which it says. The OfficeQA agent does this in a Docker
container per session ([example](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa/harness2_stream)); the trajectory is the `codex exec
--json` event stream, rendered by upstream's own Codex renderer. On OfficeQA's 172 test questions as one stream, with
GLM 5.3 Flash as executor and editors, the deliverable scores 0.651 against 0.576 for the initial skill it starts
from, +7.6 [+1.7, +13.4] paired ([the example's result](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa/harness2_stream#result), [the
recipe's Flash row](../recipes/officeqa.md#the-flash-row)). With Codex and GPT-5.6-Luna as executor and editors on
a ChatGPT login ([the Flash row with GPT-5.6-Luna](../recipes/officeqa.md#the-flash-row-with-gpt-56-luna)) the
deliverable scores 0.611 against 0.390, +22.1 [+15.7, +29.1], almost all of it the answer's form, which the editors
are told.

## Claude Code as the acting agent

With `agent.base: claude` the executions run the same `harness2-codex` tree on Claude Code; upstream has no Claude Code
substrate, and the tree, H0 and every stage prompt stay as they are. Claude Code's deployment connects the tree's parts
to its own places: `ADAPTER.md` (the tree's `AGENTS.md`) becomes the managed block of the workspace's `CLAUDE.md`, the
skills `.claude/skills/<name>/`, the sub-agents `.claude/agents/<name>.md` and the hooks the `hooks` of
`.claude/settings.json` (`.codex/hooks.json` already has Claude Code's hook schema), the rest copied under
`hermit/harness2-codex/` as for Codex. The editors read a Claude Code execution's `stream-json` events in upstream's
grammar (`render.py`: every tool's call and result, its errors, what a hook printed (the session runs with
`--include-hook-events`), a sub-agent's turns and its hand-back, upstream's clip marker), with the login's token, which
a command can read from its parent process, replaced by its name. An execution whose session failed scores 0, as in the
Trainer's records, and the step's records (`harness2.json`'s runs, `scores.jsonl`) give its error. What the editors are
told of the surface is still upstream's Codex text; the task's slots name the agent (OfficeQA: `slots_claude.yaml`). On
OfficeQA each execution runs in a container of its own (`environment.image`, as in the Codex columns) and the editors
are Claude Code sessions in the evolver sandbox, all on a Claude subscription; the two questions of `haiku-h2-s0` ran
before the containers ([the Flash row with Claude Haiku
5.5](../recipes/officeqa.md#the-flash-row-with-claude-haiku-55)).

## Continuing a stream

`library_from: <run directory>` continues a finished stream on other tasks: the run starts from that stream's
library, copied into its Training State before the first task, and is the same stream with the new tasks at its end.
It refuses a run without a summary (still going, or interrupted), editor settings that differ from that run's (model,
effort, tools, slots, sources, the Claude Code binary), a library carrying grader wording, and a task that stream took.
An interrupted run is instead resumed in place, as any run ([runs guide](../guides/runs.md)).

## Parameters and defaults

```{literalinclude} ../../hermit/agent/evolver/methods/harness2_upstream/evolver.yaml
:language: yaml
```

## Example

[`examples/officeqa/harness2_stream/`](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa/harness2_stream) runs the 172 OfficeQA test
questions as one stream, with GLM 5.3 Flash as both the executor (in Codex) and the editors' model.
