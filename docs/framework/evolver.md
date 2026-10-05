# Evolver

An Evolver is the learning method: given the results of running the current adapter, it changes the adapter. It is
the harness of a learner agent (its model, prompts, tools, control flow and state) and is task-agnostic. Inside it may
be any agent (Codex, Claude Code, a ReAct loop, a multi-agent system) or a scripted workflow that calls a model at
fixed points (as GEPA and SkillOpt do).

## Design principle: uniform protocol, free inside

An Evolver may do anything inside as long as it meets four requirements; everything else (its method, control flow,
whether and how it validates, its Training State) is its own.

| Requirement | Section |
|---|---|
| Input: what it receives | [Workspace](#workspace), [Information and tools](#information-and-tools) |
| Output: what it hands back | [What a call hands back](#what-a-call-hands-back), [One adapter per step](#one-adapter-per-step) |
| Protocol: how it is called | [Invocation](#invocation), the [interface](custom-evolver.md) |
| Specification of the adapter it changes | [`check_spec`](adapter.md#check_spec) |

The framework also offers optional helpers (the run's tools, an acting-agent runner) that an Evolver may use or
ignore.

Built-in Evolvers are `prompt_only`, `codex`, and `claude`. A custom method can use the same template if it can edit
the permitted files. GEPA, SkillOpt, and MWM are not included as built-ins in this version.

## Invocation

The Trainer calls the Evolver the way a trainer calls an optimizer: once per call (`setup`, each `step`, `finalize`).
For each call it prepares a workspace and a task message, starts the Evolver, and waits for it to return. Between
calls it measures cost and time and checks the adapter.

The Trainer keeps the Evolver instance for the run. An agent-backed Evolver launches a host process for each call;
its conversational context may start fresh or resume the previous call's session:

| Setting | Decided by | Where |
|---|---|---|
| Whether resume is supported | Evolver provider | Class attribute `supports_resume` |
| Whether this run resumes | Task user, within what the Evolver supports | `evolver.resume` in the run config |
| Prompt for a fresh session | Evolver provider | Evolver prompt file |
| Prompt for a resumed session | Evolver provider | Evolver prompt file |

When resuming, the Trainer passes the previous call's session handle (for example a Codex session) and the resume
prompt. Both prompts may contain placeholders the Trainer fills for each call: epoch, step, the learning-material
directory, the remaining budget. A run config that asks for resume with an Evolver that does not support it is refused
at start.

## Workspace

Every call sees the same directories, so any Evolver knows where to find what it needs.

| Directory | Contents | Access |
|---|---|---|
| `adapter/` | The Trainer's working copy of the current adapter (the template's `EVOLVER.md` is inside) | read and write |
| `learning_material/` | One directory per step, `current` pointing at the latest, `index.jsonl`, `README.md` (see [Trainer](trainer.md#learning-material)) | read-only |
| `task-info/` | The task description (see [Evolving Task](task.md#task-info)) | read-only |
| `history/` | A read-only view of the adapter's git history: earlier adapters and diffs | read-only |
| `state/` | Training State: what the Evolver keeps across calls, in its own format | read and write |
| `scratch/` | Temporary files, not kept | read and write |
| `output/` | What the call hands back besides the adapter | write |

Earlier learning material is always readable. Test data is never given to the Evolver.

Outside the workspace, the run config may give the Evolver read access to more resources. They are not copied; the
Evolver reads them where they are.

| Resource | Contents | Given when |
|---|---|---|
| Validation data | The validation split, with whatever the user put in it (answers included or not) | `data.val` is set |
| Environment | The task's own resources the acting agent operates on: a code base, a document collection, a library such as Mathlib | `evolver_access.environment` lists them |

The task message tells the Evolver which resources it has and where they are.

## Information and tools

The framework gives an Evolver two things, information and tools; what it does with them is its own design.

The information is the workspace, the resources above and the task message of each call.

One tool is always present: **`check_spec`**, the template's conformance check (see
[Adapter](adapter.md#check_spec)). All other tools are assembled for each run from three sources:

| Source | Tools it contributes | Examples |
|---|---|---|
| Template | Tools about this kind of adapter | "Run a learned tool in the sandbox with its test" |
| Task | Tools about the task's environment, as listed in `evolver_access.tools` | Read-only database queries, code-base search |
| Run | Tools about this training run | Remaining budget; an optional metered model call (`llm`) |

The framework provides the mechanism, not the list:

1. **Declaration.** Templates and tasks declare tools in the same format as an adapter's tools: name, description,
   input and output schemas, entry point.
2. **Delivery.** Before each call the Trainer merges the sources into one toolset: MCP tools for an agent, functions or
   HTTP endpoints for a program. The task message lists the tools and their sources.
3. **Accounting.** Every tool call is logged and metered, and runs in the sandbox when one is enabled.

Training rollouts and the binding check after each call are the Trainer's own work, not tools.

### Model usage

The framework does not prescribe how an Evolver reaches its models. A built-in agent may use its own login (for
example a Codex or Claude Code subscription, where no API key exists); a custom Evolver may use its own API key or the
optional `llm` tool. When the sandbox restricts the network, `sandbox.allow` lists the model endpoints it may reach.
In every case the Evolver reports what it used:

```python
@dataclass
class Usage:
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    calls: int
    cost_usd: float | None = None   # None when no price applies (for example a subscription)
```

For built-in agents the Trainer reads usage from the agent's session logs itself; usage through the `llm` tool is
metered directly. Budgets may therefore be set in tokens as well as in dollars.

## Validation

Validation belongs to the Evolver: whether it validates, on which tasks, how often, and how it uses the results are
its own decisions. Prompt-only and MWM do not run candidates; GEPA and SkillOpt do.

An Evolver that runs candidates may bring its own means or use the framework's **acting-agent runner**: it runs an
adapter through the user's pipeline and judge (the same as `eval`) on training or validation tasks, never test tasks,
and returns trajectories and scores. Its cost counts toward the Evolver's usage. Validation scores are the Evolver's
working numbers; the official result of a run is always `eval` on the test split.

An Evolver declares the resources it cannot run without in its manifest. `train` compares them with the run config at
start and stops with an error if any is missing, because without them the Evolver should not run at all.

```yaml
# evolver.yaml
class: my_evolvers.gepa:GepaEvolver
needs: [validation_data]          # validation_data | environment | send_material_out
params: {}
```

## What a call hands back

A step hands back three things:

1. **The adapter** in `adapter/` (mandatory, see below).
2. **A `StepResult`**: status, a one-line summary, an optional rationale, the session handle, usage. It is the same for
   every Evolver. Whether the adapter changed is not reported: the Trainer determines it by a git diff.
3. **`output/`**: anything else, in any format the Evolver chooses (reports, analyses, candidate comparisons). The
   Trainer stores it unchanged with the step's records. If `output/report.md` exists, it is referenced in the commit
   message and in `CARD.md`.

## One adapter per step

Every step must hand back exactly one adapter: the one the next step's training rollout will use. Which one is the
Evolver's choice: the candidate it just made, the previous adapter unchanged, or another it kept (for example a parent
that GEPA selects from its pool).

An Evolver whose method produces many candidates handles them itself: it keeps them in `state/` and may use them in
later steps or hand one back from `finalize`. The Trainer never sees them; the git tree records only the adapters
actually handed back.

## Responsibilities and limits

| The Evolver decides | Not allowed (enforced by) |
|---|---|
| How to change the adapter | Changing the template's fixed parts or breaking declared formats (binding check, repaired by the Trainer) |
| Whether and how to use its tools, including `check_spec` | Handing back no adapter, or several |
| Whether to validate, how, and with how much of its budget | Exceeding the hard budgets (the Trainer stops the run) |
| Which adapter each step and `finalize` hand back | Reading test data, paths or network not given (sandbox, when enabled) |
| What its Training State holds and in what format | Not reporting usage (if it cannot, it says so) |
| How it reaches models, and which models | Ending the run early, unless `trainer.allow_early_stop` is set |
| Whether to check task rules, and how | — |

## Implementation notes

- `Context` has four more fields than the table above: `message` (the task message of the call), `sandbox` (wraps
  commands the Evolver starts), `cancel` (a `threading.Event` the Trainer sets to stop the call) and `tool_url` (the
  HTTP endpoint serving the same tools).
- The acting-agent runner is the run tool `run_agent(task_ids, adapter=None)`: training or validation tasks only, its
  cost counted as the Evolver's.
- Built-in agents' usage is read from the agent's event log when the session ends; while a session runs, the hard
  budget sees its wall time and its metered tool calls only. A call may therefore overrun the budget by what one
  session spends; `trainer.call_timeout_min` bounds it (decided 2026-10-05).
- Built-in Evolvers: `prompt_only` (one model call per step through the `llm` tool), `codex` and `claude`
  (`AgentEvolver` with a start and a resume prompt).
