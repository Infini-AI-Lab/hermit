# AHE Adapter

AHE, Agentic Harness Engineering ([Lin et al., 2026](https://arxiv.org/abs/2604.25850),
[code](https://github.com/china-qijizhifeng/agentic-harness-engineering)), evolves a coding agent's harness in a closed
loop: k rollouts per task, an Agent Debugger that distils each task's traces into a report, and an Evolve Agent that
edits the harness and declares every change with the fixes and regressions it predicts, which the next iteration's
rollouts confirm or refute; changes judged harmful are reverted. AHE edits the whole NexAU harness, its tool
implementations and its middleware classes included. **AHE Adapter is AHE's loop confined to an adapter:** the
acting agent (Codex or Claude Code), its agent loop and its tools are fixed, and only the adapter's files change: the
system prompt, scripts the shell runs, a middleware module run as the host's hooks, skills, sub-agents and a long-term
memory, the same files on either host. Choose it when the acting agent runs hooks, each task can be run k times per
iteration, and you want the learner to change more than one text file under an attributable, falsifiable record of
what each change was for.

## Setup

```yaml
template: ahe@1.0
evolver:
  name: ahe_adapter
  params: {task_field: uid, k: 2, rollback: auto}   # the k rows of a task share task_field
  base: codex                                        # the Agent Debugger and the Evolve Agent
  model: {name: gpt-5.6-luna, login: chatgpt, effort: medium}
data:
  train: rows/train_k2.txt   # every training task k times: rows <task>#1 ... <task>#k
  step_size: 100             # one step = one AHE iteration over every trial row
trainer: {epochs: 10, allow_early_stop: true}        # the paper's ten iterations
sandbox: {enabled: true, network: restricted, allow: [chatgpt.com, auth.openai.com]}
```

Both learners are sessions of the `codex` Base Agent: the Agent Debugger one session per task and attempt, the Evolve
Agent one session per step with `check_spec` as its only tool. A task's k rollouts are k rows of the training split
that share `task_field` (a row without it is grouped by its id without the `#n`); the example's `make_rows.py` writes
them. The learners are Codex sessions whatever the acting agent is. The acting agent must deploy the adapter as
`hermit deploy` does for its host, so that the host's hooks run the middleware (Codex: `.codex/hooks.json`; Claude
Code: `hooks` in `.claude/settings.json`). Pi runs no hooks: its deployment lists the hook commands for the agent to run
by hand, and run so, without an event's payload, only `session_start` has something to do (it hands the memory over).

## The template

`ahe@1.0` (`hermit/adapter/templates/ahe/`) maps AHE's harness components onto files, the same for every host:

| AHE component | File | Learnable |
|---|---|---|
| System prompt and tool guidance | `AGENTS.md` (`ADAPTER.md`, the context export, is kept equal to it; a deployment writes it as Codex's `AGENTS.md` or Claude Code's `CLAUDE.md`) | yes |
| Tool implementations | `scripts/*.py`, run by the shell | yes |
| Middleware | `middleware.py`: functions `session_start`, `before_tool`, `after_tool`, `stop`, `before_model` | yes |
| Skills | `.agents/skills/*/SKILL.md` and their `scripts/` | yes |
| Sub-agents | `agents/*.md` | yes |
| Long-term memory | `MEMORY.md`, handed to the session by the seed middleware at `session_start` | yes |
| The hook runner | `hooks.json` (the `hooks` export), `hook_runner.py`, `hermit_hooks.py` (the middleware's interface), `hook_hosts.py` (each host's wire) | fixed |

`hooks.json` starts `hook_runner.py` from the adapter's deployed copy on every lifecycle event of the host; it
registers the events of both hosts (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure,
PostToolBatch, Stop, PreModelRequest), and each host fires those it has and skips the others (Codex says nothing;
Claude Code writes an "Unknown hook event" line to its debug log). The runner reads the host from the deployment's record
(`.hermit-deploy.json`, written by `hermit.hosts.codex` or `hermit.hosts.claude_code`), so the template names no host.
It hands each call to that host's wire in `hook_hosts.py`, which turns the host's payload into the one `HookInput`
the middleware reads and its `HookResult` into the host's output (context to add, a tool call to deny, a stop to
block). The runner calls the middleware's function for the event with the task text (the workspace's `task.md`,
else the session's first prompt), fails open on any error, and logs every call to `.hermit-hooks/hook-calls.jsonl`
in the workspace, which the Debugger reads beside the session's trajectory. The hosts differ where this table, the
one `.hermit/EVOLVER.md` gives the Evolve Agent (`hook_hosts.CAPABILITIES`), says:

<!-- hook_hosts.capability_table: begin -->
| | Codex | Claude Code |
|---|---|---|
| `session_start` | SessionStart, before the prompt (again on a resumed session: `inp.source`); `context` is read before the first model request | SessionStart, before the prompt (`inp.source`: startup, resume ...); `context` is read before the first model request |
| `before_tool` | PreToolUse (none for a `write_stdin` poll): `deny` (not run; the model reads `reason`), `allow`, `updated_input` (run instead); `context` is read with the call's result, but dropped when given with `allow` and no `updated_input` | PreToolUse (none for a call whose input the CLI refused): `deny` (not run; the model reads `reason`), `allow`, `updated_input` (run instead; without `allow` the session's permission mode decides); `context` is read with the call's result |
| `after_tool` | PostToolUse after a call that ran, a failed command too (no exit code: `failed` is None); `output_text` may be the command's last output chunk only (always for one that outlives its first wait: its polls fire nothing); none after a denied call, a failed `apply_patch` or one run through the shell; `block`: the model reads `reason` instead of the output; `context`: beside it. With a code-mode model (`gpt-5.6-luna`) a block or a deny reaches the model as an error in its script | PostToolUse, or PostToolUseFailure after a call that failed (`failed`; `exit_code` and the error as `output_text`); `output_text` is None after a Write, Edit, MultiEdit or NotebookEdit (no output text); a command moved to the background at its timeout is a PostToolUse with the output so far; none after a denied call or one whose input the CLI refused; `block`: the output stays and the model also reads `reason`; `context`: beside it |
| `stop` | Stop: `block` once per turn, the model going on with `reason`; a blocked stop's `context` and the queue go with the reason | Stop: `block` once per turn, the model going on with `reason`; a stop's `context` goes out only with a block (alone, it would make the model go on); a sub-agent's end is no stop |
| `before_model` | PreModelRequest, before every model request; its output is ignored: only its `context` is queued | no such event: the runner calls it after the prompt, after every model message's tool calls (PostToolBatch) and after a blocked stop, once per model request (none before a sub-agent's first); only its `context` is queued |
| queued context | handed over with the next prompt or tool result, or a blocked stop; a stop that goes through leaves it for the session's next prompt (a resumed session) | handed over with the agent's next prompt or tool result (a failed one too), or a blocked stop; a stop that goes through leaves it for the next prompt; a sub-agent's queue is its own |
| `stop_session` | ends the session at `session_start` only; elsewhere the session goes on (with a classic, not code-mode, model a stop at `after_tool` replaces the output with `reason`) | ends the session at `before_tool` (once the call has run) and at `after_tool` of a call that succeeded; ignored at `session_start` and at `after_tool` of a failed call |
| `system_message` | shown nowhere | shown to the person watching, not to the model |
| `inp.tool_kind` | `shell` (Bash: a code-mode script's `exec_command` too), `patch` (`apply_patch`), `mcp`, `other` | `shell` (Bash), `read` (Read), `write` (Write, Edit, MultiEdit, NotebookEdit), `mcp`, `other` (Grep, Glob, Agent ...) |
| `inp.cwd`, `inp.model`, `inp.turn_id` | the session's directory (`inp.workspace`), even for a command run in another `workdir`; the model; the turn | the shell's current directory, which follows the agent's `cd` (`inp.workspace` is the project's); no model; the prompt's id |
<!-- hook_hosts.capability_table: end -->

The rows were measured on the hosts' own CLIs, Codex 0.164.0 and Claude Code 2.1.317 (2.1.318 alike), each run on a
scripted local model with a probe hook on every event; `tests/fixtures/ahe_hosts/` keeps the payloads they sent, which
`tests/test_ahe_hosts.py` replays through the wires, and the same file runs both CLIs on a scripted model when they
are installed: the memory, a note after a call (on Claude Code a failed one too, after a `cd`), the queued
`before_model` context, a deny, a blocked stop with the queue, a context given with `allow`, a new input, a block with a
system message, and `stop_session` after a call; the other cells rest on the probe and its payloads. `.hermit/check.py`
deploys a copy of the adapter to each host as the framework does, measures the context file each reads (Codex reads
32,768 bytes), runs one session of synthetic events in each host's own shape and order through the hooks the
deployment installed, as the host runs them, holding every output to the fields the host reads (and to the one drop it
knows of: a context Codex drops when it comes with `allow`), and requires `AGENTS.md` to keep the paper's three seed
rules; `MEMORY.md` its header; `middleware.py` to define the five functions, import only the standard library or
`hermit_hooks`, keep reading `MEMORY.md` at `session_start` and hand an entry of it to the model on each host; skills
and sub-agents their front matter.

## One framework step

Step N is AHE iteration N: step 1 scores the seed, and step N's edits are scored by step N+1's rollouts.

1. Classify every task by its k rollouts (pass, fail, exception), diff against step N−1 (flipped, regressed, rollout
   moves, infrastructure buckets), and score step N−1's change manifest with upstream's rules (EFFECTIVE ...
   HARMFUL). The best-ever record keeps the commit the rollouts ran when their pass@1 is strictly higher.
2. Stop when pass@1 reaches `target_pass_rate` (with `trainer.allow_early_stop`).
3. `rollback: auto`: the files of every change judged HARMFUL are restored from before the change (the paper's
   Algorithm 1), undone again if `check_spec` then fails; `rollback: agent` only reports, as the released code.
4. The Agent Debugger: one Codex session per task over its rollouts' trajectories, hook audits and the judge's verdicts,
   with upstream's query; a failed attempt is retried, and after `retries` upstream's fallback text stands in. The
   reports and an overview go to `state/analysis/step_N/`.
5. The Evolve Agent: one Codex session reads the overview, the verdicts and the history (`START.md` and the evolution
   query on stdin), edits the adapter, and writes `state/change_manifest.json` for iteration N.
6. The manifest is validated and archived, and the iteration's scores and history are updated.

```mermaid
flowchart TD
    R["Trainer: k rollouts of every task on H_(N-1)"]
    S["1. classify, diff against N-1, score manifest N-1,<br/>best-ever (strict >, the commit the rollouts ran)"]
    E{"pass@1 >= target_pass_rate?"}
    B["3. roll back the HARMFUL changes of manifest N-1"]
    D["4. Agent Debugger: one codex session per task<br/>over its trajectories, hook audits and verdicts"]
    V["5. Evolve Agent: one codex session edits the adapter<br/>(check_spec), writes manifest N"]
    P["6. archive the manifest, scores, history"]
    F["finalize: final = best_ever (git archive of its commit) or last"]
    R --> S --> E
    E -- yes --> F
    E -- no --> B --> D --> V --> P --> R
```

`finalize` hands back the best-ever commit (`final: best_ever`) or the working copy (`final: last`). With no
validation split, best-ever is chosen on the training rollouts, so its training pass rate overstates what the same
adapter scores on held-out tasks; evaluate it on a test split.

## Parameters

```{literalinclude} ../../hermit/agent/evolver/methods/ahe_adapter/evolver.yaml
:language: yaml
```

## Records

Under `<run>/workspace/state/` (and each step's copy, `step_NNNN/state/`): `stats/`, `analysis/step_NNNN/` (the
Debugger's overview, one report per task, its jobs and usage), `queries/` (the Evolve Agent's input), `manifests/`,
`evaluations/` (the verdicts on the previous step's changes), `rollbacks/`, `best_ever.json`, `iteration_scores.md`
and `evolution_history.md`; `<run>/workspace/output/final_selection.json` names the iteration `finalize` handed back.
Each rollout's log holds its `hook-calls.jsonl` and `hook-state/` beside the session's trajectory.

## The authors' implementation against this one

The authors' code is not called: `evolve.py` (4,719 lines) drives NexAU agents in e2b sandboxes through a fork of
Harbor and needs Python 3.13 or later. Its parts are ported per function, each module naming the lines it follows:
the statistics, the diff, the attribution and the overview (`analysis.py`), the Debugger's jobs and attempts
(`debugger.py`), the Debugger's queries, fallback text and the evolution query verbatim (`queries.py`); `START.md`
and `DEBUGGER.md` adapt the Evolve Agent's prompt and the Debugger's rules. The rollback and the trajectory reader are
this framework's own code.

| Dimension | AHE (8b2a55d) | This Evolver | Why |
|---|---|---|---|
| When learning happens | Once per iteration, after k = 2 rollouts of every task; 10 iterations | The same: a step is every trial row, ten steps | Same |
| What the learner sees | The Debugger: each task's NexAU traces (with a per-turn timing breakdown for a timed-out rollout) and verifier output; the Evolve Agent: the overview, per-task reports, verdicts, history | The same, with the acting agent's event stream (Codex's or Claude Code's; whole, and one file per standard step) and the hook audit as the traces and the judge's feedback as the verifier output; no timing breakdown, since the stream has no timestamps | Framework: the host's trace format |
| What is learned | NexAU's prompt, tool descriptions and implementations, middleware classes, skills, sub-agents, memory | The adapter's files (the table above), the same on either host; the host's loop and tools are fixed | Framework: the acting agent is fixed; the reason for the name |
| The update | Edits plus a manifest of predictions, attributed at the next iteration; Algorithm 1 reverts HARMFUL changes, the released code leaves it to the agent | The same manifest and rules; `rollback: auto` (the paper) or `agent` (the code) | Decision: the paper by default |
| Final harness | The best iteration by pass@1 | The best-ever commit, the harness its rollouts ran (Algorithm 1 line 15 names the harness after the next edit, which no rollout measured) | Decision |
| Models and budgets | GPT-5.4 high for every role; the Evolve Agent 500 turns | Config; the Evolve Agent `evolve_timeout_s` 7,200 s, the Debugger 600 s × 3 attempts, 16 at a time | Decision |
| Not ported | — | The Explore Agent (one-shot skill seeding from NexAU's source and coding-agent references); Best-of-N | Decision: no counterpart for a fixed host |

## Result

On OfficeQA's GPT-5.6-Luna column, one training: 0.616 on the 172 test questions, +25.0 points [+16.9, +33.1] over
its starting adapter ([example](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/officeqa/ahe_adapter#result),
[recipe](../recipes/officeqa.md#ahe-adapter)).

## What it is not

The Evolve Agent cannot change the host's own tools or loop: a tool is a script the shell runs, and middleware acts
only through what the host's hooks allow (add context, deny a tool call, block a stop; the table above). The Explore
Agent is not ported. Claude Code as the acting agent is checked hook by hook, not run on a benchmark here. The paper
evolves and reports on the same benchmark; on OfficeQA the adapter is trained on the training split and reported on
the held-out test split.
