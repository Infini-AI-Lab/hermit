# Evolution framework design

Date: 2026-10-05. Discussion record. **Superseded by the pages under `docs/framework/`**, which include every decision
here plus later ones (no `decision` field, no `rejected/` refs, `start_from`, sandbox warnings, `--adapter none`).

This document defines the parts of a general framework for evolving harness adapters. It is written one part at a time;
each section is added after it is agreed.

| Part | Status |
|---|---|
| 1. Adapter template and adapter | Agreed (2026-10-05) |
| 2. Evolver | Agreed (2026-10-05) |
| 3. Trainer | Agreed (2026-10-05) |
| 4. Evolving task | Agreed (2026-10-05) |

## Roles

Three kinds of users work with the framework, and the framework itself provides one component.

| Role | Provides |
|---|---|
| Template provider | An adapter template |
| Evolver provider | An Evolver: the learning algorithm (for example prompt-only, GEPA, SkillOpt, MWM) |
| Task user | An evolving task, and a choice of one template and one Evolver |
| Framework | The Trainer (the outer training loop) and evaluation (`harnessadapter eval`) |

As in verl, the Trainer handles everything around the learning algorithm (data, rollouts, scoring, checks,
checkpoints, records, budgets), and the Evolver, in the role of an optimizer, decides how the adapter changes. An
Evolver is essentially the harness of a learner agent: its model, prompts, tools, control flow and state.

## 1. Adapter template and adapter

### 1.1 Two different things, arranged as a tree

An **adapter template** and an **adapter** are different things. The template is a structure and its specification;
an adapter is a concrete, deployable artifact built on that structure.

They form a tree:

```text
template (root)
 ├── adapter A1      evolved with Evolver P on task X
 │    ├── A1.1       continued from A1 on task X
 │    └── A1.2       started from A1, evolved on task Y
 ├── adapter B1      evolved with Evolver Q on task X
 └── adapter C1      evolved with Evolver R on task Z
```

| Tree element | Meaning |
|---|---|
| Root | The template. It fixes the structure of every node in its tree: what is learnable and what is fixed. |
| Node | An adapter: every checkpoint and every final result. Each node is complete, downloadable and deployable. |
| Edge | One training run from a parent node: the Evolver, the evolving task, the config, and the run records. |

Any node can be the starting point of a new training run. The starting point of a run is either a template (the root)
or an adapter.

Templates do not derive from one another. Each template is the root of its own tree; a new structure is a new template.

The tree is a git repository. One repository holds one tree; the Trainer maintains it.

| Tree element | In git |
|---|---|
| Root (template) | The first commit; its content is the template. |
| Node (adapter) | A commit. Downloading an adapter is cloning the repository and checking out that commit. |
| Node id | The commit hash. |
| Parent | The commit's parent. |
| Edge (one step of a training run) | A commit whose message records the Evolver, the evolving task, the epoch and step, the decision and its grounds, and where the run records are. |
| Branch (one training run) | A git branch, for example `gepa/lean-x`. |
| Checkpoint, final result | A git tag, for example `gepa/lean-x/epoch-3`, `gepa/lean-x/final`. |

Only the Trainer runs git. It creates the repository from the template, commits each accepted candidate on the run's
branch, and tags checkpoints and final results. Rejected candidates are kept for analysis under a separate
`rejected/` ref namespace and never enter a run's branch. The Evolver never runs git: it edits its working copy of the
adapter, and reads earlier versions and diffs through a read-only history view.

### 1.2 Adapter template

A template is an initial adapter together with a specification. It is written by the template provider and contains
no task content and no learning method.

```text
my_template/
  adapter.yaml   manifest: identity, exports, structure (the specification)
  ADAPTER.md     context for the acting agent (learnable or fixed, as the specification says)
  EVOLVER.md     guide for the Evolver
  check.py       validity check (any entry point declared in the manifest)
  README.md      for people
  ...            fixed code and files; initial content of the learnable parts
```

| Content | Required | Purpose |
|---|---|---|
| `adapter.yaml` → `template` | yes | Template id, version, contract version. |
| `adapter.yaml` → `exports` | yes | What the adapter becomes in the acting harness after deployment: context, MCP tools, agent roles, reactions, resources it needs. |
| `adapter.yaml` → `structure` | yes | The **specification**. Learnable parts: the paths that may change, and the format each must follow (for example `memory/*.md` with a scope field, `tools/*/` with a `test.py`). Fixed parts: the paths that must not change. |
| Fixed files | as needed | Code and files shared by every adapter in the tree and never learned (for example a dispatcher, a context renderer, a collaboration service). |
| Initial learnable content | yes | The learnable content of the root; usually empty, may hold defaults. |
| `EVOLVER.md` | yes | For the Evolver: what each learnable part is, its format, what a valid change looks like. Structure only: no learning method, no task content. |
| Check function | yes | Decides whether an adapter conforms to the template; declared in `adapter.yaml` (for example `check: check.py:check`). The framework provides the generic checks as a library and a default implementation that runs only them, so a template writes only its own extra checks (see 2.4). |
| `README.md` | yes | For people: what the template is for and how to use it. |

The specification is binding. An adapter that changes a fixed part, or whose learnable parts do not follow their
declared formats, is rejected. A template provider who wants a piece of code to evolve declares it as a learnable part
and states its format and the checks it must pass.

### 1.3 Adapter

An adapter is self-contained: a complete copy of its template's fixed parts plus learned content, so it can be
downloaded and deployed directly.

| Content | Required | Written by |
|---|---|---|
| All fixed parts of the template, unchanged (including the `template`, `exports` and `structure` sections of `adapter.yaml`) | yes | Trainer (copied) |
| Current content of the learnable parts | yes | Evolver |
| `adapter.yaml` → `origin` | yes | Trainer |
| `CARD.md` | yes | Trainer (generated); the user may add to it |

The adapter's id, parent and history are its git commit, the commit's parent and the git log; they are not repeated in
files. `origin` only names the root template (id and version), so an adapter checked out on its own still says which
specification it follows. Whether the fixed parts are unchanged is checked with git: the diff between the root commit
and the adapter, restricted to the fixed paths, must be empty.

`CARD.md` is the adapter's card, like a model card: written for whoever downloads the adapter. It states the template
and parent, how the adapter was evolved (Evolver, task, model, number of epochs and steps), its `eval` results and
cost against the baselines used, a summary of what was learned, and where it has and has not been tested. The Trainer
generates it from the run records at the end of a training run. It does not affect how the adapter runs.

Not part of an adapter:

| Item | Where it lives |
|---|---|
| Training State of the Evolver (for example a GEPA candidate pool, an MWM journal and docket) | The Trainer's run directory for that training run |
| Data produced while the adapter runs (for example a shared lemma library and channel) | The run directory of each rollout or `eval` |
| Trajectories and scores as run records | Run directories; the commit message points to them |
| Task content (task-info, data) | The evolving task |

Trajectories may still be part of an adapter as learned content. That is between the Evolver and the template: if the
template's `structure` declares a learnable data path (for example `evidence/`), the Evolver may copy any material it
can read into it, up to every complete trajectory (as MWM does, letting the acting agent read them at deploy time).
The Trainer does not treat this specially; it is ordinary learnable content, checked and committed like any other.

### 1.4 Differences from the current prototype

| Current prototype | This design |
|---|---|
| `architectures/` hold starting adapters; the manifest has no learnable/fixed specification | Templates declare `structure`; the Trainer enforces it |
| Protected paths are declared by the Evolver definition (`evolver.toml` `protected`) | Fixed parts are declared by the template |
| Manifests and configs in JSON (`adapter.json`) or TOML (`evolver.toml`) | All in YAML: `adapter.yaml`, `evolver.yaml`, `run.yaml`; data and records stay JSON / JSONL |
| `mwm` keeps its journal and docket inside the adapter | Training State lives outside the adapter |
| `mwm`'s EVOLVER.md also describes how to use the MWM method | EVOLVER.md describes structure only; the method belongs to the Evolver |
| Candidates record content fingerprints; the learner sees a framework-maintained git history | Every adapter is a commit in the tree's git repository; checkpoints and finals are tags; each adapter carries `CARD.md` |

## 2. Evolver

**The contract in one sentence.** An Evolver may do anything inside as long as it meets the input requirements (2.2,
2.4), the output requirements (2.6, 2.7), the protocol (2.1, 2.3) and the specification of the adapter it changes
(1.2, `check_spec`). The framework fixes these; it also offers optional helpers (the tools of 2.4, the acting-agent
runner of 2.5) that an Evolver may use or ignore. Everything else (its method, control flow, whether and how it
validates, its Training State) is its own.

An Evolver is the learning method: given the results of running the current adapter, it changes the adapter. It is
essentially the harness of a learner agent (its model, prompts, tools, control flow and state) and is task-agnostic.
Inside, it may be any agent (Codex, Claude Code, a custom agent loop) or a scripted workflow that calls a model at fixed
points (as GEPA and SkillOpt do). This section defines the protocol between the Trainer and an Evolver; the Evolver's
internals are up to its provider.

### 2.1 Invocation

The Trainer calls the Evolver the way a trainer calls an optimizer: once per call (initialisation, each training step,
finalisation). For each call the Trainer prepares a workspace (2.2) and a task message, starts the Evolver, and waits
for it to exit; the results are in the workspace. Between calls the Trainer measures cost and time and checks the
candidate.

The process always starts fresh for each call. The agent's conversational context may either start fresh or resume:

| Setting | Decided by | Where |
|---|---|---|
| Whether resume is supported, and the default | Evolver provider | Evolver manifest |
| Whether this run resumes | User, within what the Evolver supports | Run config |
| Prompt for a fresh session | Evolver provider | Evolver prompt file |
| Prompt for a resumed session | Evolver provider | Evolver prompt file |

When resuming, the Trainer hands the Evolver the previous call's session handle (for example a Codex session) and the
resume prompt. Both prompts may contain placeholders the Trainer fills for each call, such as the epoch, the step, the
learning-material directory, the previous decision and the remaining budget. A run config that asks for resume with an
Evolver that does not support it is rejected.

### 2.2 Workspace

Each call sees the same directories, so any Evolver knows where to find what it needs. What goes into `task-info/` is
defined in part 4.

| Directory | Contents | Access |
|---|---|---|
| `adapter/` | The candidate: the Trainer's working copy of the current adapter (the template's `EVOLVER.md` is inside it) | read and write |
| `learning_material/` | One directory per step (`step_0001/`, `step_0002/`, ...), `current` pointing at the latest, an `index.jsonl` and a `README.md` (see 3.3) | read-only |
| `task-info/` | The task description | read-only |
| `history/` | A read-only view of the adapter's git history: earlier adapters and diffs | read-only |
| `state/` | Training State: what the Evolver keeps across calls, in its own format | read and write |
| `scratch/` | Temporary files, not delivered | read and write |
| `output/` | What the call hands back | write |

Earlier learning material is always readable: it comes from the training and validation data the Evolver has already
seen. Test data is never readable by the Evolver.

Outside the workspace, the run config may grant two more read-only accesses. They are not copied into the workspace;
the Evolver reads them where they are, and the Trainer's sandbox allows exactly those paths.

| Access | Contents | When |
|---|---|---|
| Environment | The task environment: for example a code base, a database or tools the Evolver may inspect, or run inside its sandbox | Only if the run config grants it |
| Validation data | The validation set itself: task inputs, and expected answers only if the config says so | Only if the run config grants it; seeing answers makes overfitting to the validation set easier, so including them is the user's choice |

The task message tells the Evolver which of these accesses it has and where they are.

### 2.3 The Evolver interface

An Evolver is a class with a uniform interface; the Trainer calls only this interface. What happens inside (one agent,
a ReAct loop, a multi-agent system, a scripted workflow such as GEPA or SkillOpt) is up to the implementation.

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol


# ---------- What the Trainer gives the Evolver ----------

@dataclass(frozen=True)
class Workspace:
    adapter: Path             # candidate adapter (writable working copy)
    learning_material: Path   # current/ and previous/ (read-only)
    task_info: Path           # task description (read-only)
    history: Path             # read-only view of the adapter's git history
    state: Path               # Training State (read/write, Evolver's own format)
    scratch: Path             # temporary files (read/write, not delivered)
    output: Path              # reports the Evolver hands back (writable)


@dataclass(frozen=True)
class Access:
    environment: Path | None = None   # granted by the run config, read-only
    validation: Path | None = None    # granted by the run config, read-only


@dataclass(frozen=True)
class Progress:
    epoch: int
    step: int
    is_epoch_end: bool


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict
    output_schema: dict
    source: Literal["template", "task", "run"]   # who contributed it (see 2.4)

    def __call__(self, **arguments: Any) -> dict: ...


@dataclass(frozen=True)
class Context:
    workspace: Workspace
    access: Access
    tools: dict[str, Tool]                  # assembled for this run (see 2.4); every call is sandboxed, metered and logged
    params: dict[str, Any]                 # the Evolver's parameters from the run config


@dataclass(frozen=True)
class StepContext(Context):
    progress: Progress
    session: str | None = None             # previous call's session handle, set only when resuming


# ---------- What the Evolver hands back ----------

@dataclass
class StepResult:
    status: Literal["done", "failed", "out_of_budget", "stop"]
    decision: Literal["accept", "reject"]  # keep the candidate as the new adapter, or not
    rationale: str                         # grounds for the decision, e.g. validation scores
    summary: str                           # one line: what changed
    session: str | None = None             # handle for resuming next time, if any
    usage: list["Usage"] = field(default_factory=list)  # model usage of this call (see 2.4)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class FinalResult:
    adapter: Path                          # the adapter to hand back as the final result
    summary: str
    extra: dict[str, Any] = field(default_factory=dict)


# ---------- The interface ----------

class Evolver(ABC):
    """A learning method. The Trainer calls only these methods; what happens inside is up to the implementation."""

    supports_resume: bool = False

    def setup(self, ctx: Context) -> None:
        """Optional: before the first step (e.g. initialise Training State)."""

    @abstractmethod
    def step(self, ctx: StepContext) -> StepResult:
        """Read the learning material and change the candidate in ctx.workspace.adapter."""

    def finalize(self, ctx: Context) -> FinalResult:
        """Optional: after training. Default: hand back the current adapter."""
        return FinalResult(adapter=ctx.workspace.adapter, summary="current adapter")
```

Only `step` must be implemented. `CheckResult` and `Usage` are defined in 2.4. `extra` holds whatever an Evolver wants recorded; the Trainer writes it to the run records unchanged. A run
config that asks for resume with an Evolver whose `supports_resume` is false is rejected.

The framework ships two ready implementations, so a user who does not want to write code only supplies prompts or a
command:

| Implementation | What it does inside | The user supplies |
|---|---|---|
| `AgentEvolver` | Starts a built-in agent (Codex or Claude Code) on the workspace with the prompt | A start prompt, optionally a resume prompt, and a model |
| `CommandEvolver` | Starts any external program and passes it the call's information and the tool endpoints | A command |

Anything else (a ReAct loop, a multi-agent system, GEPA, SkillOpt) is a subclass of `Evolver`. Whatever the inside
looks like, everything the Evolver starts runs in the Trainer's sandbox, which allows only the workspace, the granted
accesses, its tools and the model endpoints the run config allows. How an Evolver reaches its models is
its own choice (2.4); it reports what it used.

An Evolver is declared by a small manifest that names its class and its default parameters:

```yaml
# evolver.yaml
class: my_evolvers.gepa:GepaEvolver     # or harnessadapter:AgentEvolver
params: {}                              # defaults, overridable in the run config (evolver.params)
```

A user-written class runs inside the Trainer process, so its code is trusted; the agents and commands it starts are
sandboxed. Running third-party Evolvers would require running the class itself in a sandboxed subprocess.

An Evolver may also be a complete black box: a third-party service or a closed binary, wrapped by a thin subclass that
translates the call to and from the black box. The Trainer's guarantees do not depend on what is inside: test data
never enters the workspace, the binding check runs after every call, and wall-clock and reported usage are recorded.
What a black box cannot offer is stated, not assumed: its usage may be unknown (then only wall-clock time and any price
the provider reports are recorded), it may not support resume, and the learning material it receives leaves the
Trainer's control, so the run config must allow it explicitly.

### 2.4 Information and tools

An Evolver is an independent harness system. The framework gives it two things, information and tools, and what it
does with them is its own design: whether, when and why it uses any tool is outside the framework's scope.

The information is the workspace (2.2), the granted accesses, and the task message of each call.

One tool is always present: `check_spec` (below). It is the same for every run, and it exists even when the template
declares no check of its own: then the framework default runs. A template may make everything learnable and declare no
formats; `check_spec` then only confirms the adapter still loads and deploys.

All other tools are not a fixed list. They are assembled for each run from three sources, because which tools make
sense depends on the template and the task:

| Source | Tools it contributes | Examples |
|---|---|---|
| Template | Tools about this kind of adapter | A template with learned programs may offer "run a learned tool in the sandbox with its test" |
| Task | Tools about the task's environment | Read-only database queries, code-base search, a domain API |
| Run | Tools about this training run | Remaining budget; an optional metered model call (`llm`) |

The framework provides the mechanism, not the list:

1. **Declaration.** Templates and tasks declare their tools in the same format as an adapter's `tools`: name,
   description, input and output schemas, entry point.
2. **Assembly and delivery.** Before each call the Trainer merges the three sources into one toolset and hands it to the
   Evolver: as MCP tools to an agent, as functions or HTTP endpoints to a program. The task message lists the tools and
   their sources.
3. **Constraints and accounting.** Every tool runs in the sandbox and every call is logged and metered. Permissions come
   from the run config: a task tool such as database access is in the toolset only when the config grants it.

Rollouts and the binding conformance check after each call are the Trainer's own work, not tools.

**`check_spec`, the standard tool,** answers one question: does this adapter conform to its template's specification and deploy? It does
not judge quality, run tasks, call models, or check task rules.

| # | Check | Rule from | Code by |
|---|---|---|---|
| 1 | Fixed parts unchanged (git diff against the root commit on the fixed paths is empty) | template `fixed` paths | framework library |
| 2 | Every added or changed file lies in a learnable path | template `learnable` paths | framework library |
| 3 | `adapter.yaml` parses; its `template`, `exports` and `structure` sections equal the root's | template manifest | framework library |
| 4 | Learnable parts follow their declared formats (required fields, names, size limits) | template `structure` | framework library |
| 5 | Deploys: context renders, tool entry points import, roles convert (no acting agent started) | template `exports` | framework library |
| 6 | Template-specific requirements a declaration cannot express (for example: every new tool has a passing test) | template provider | template provider |

The rules belong to the template: the template may declare its own check function in its manifest, which may call the
framework's library for checks 1–5. A template that declares none gets the framework default, which runs checks 1–5;
either way `check_spec` is always available. The function
receives the candidate's path, runs in a sandbox without network and with a time limit, and returns
`CheckResult(passed, issues)`, each issue naming the path, the check and the problem. Because the function travels with
the template, a downloaded adapter can be checked on its own.

Both sides call the same function, for different purposes:

| Caller | When | Purpose |
|---|---|---|
| Trainer | Once before training, on the root | The template itself must conform. |
| Evolver | Any time during a call (optional) | Feedback, so it can fix problems itself. |
| Trainer | After every call (binding) | Nothing that fails may flow into the next round. |

What the Trainer does when the binding check fails: see 3.4.

Task rules are not part of `check_spec`. Checking an adapter against task rules, if at all, is the Evolver's own
business, built into its pipeline (4.3).

**Model usage.** The framework does not prescribe how an Evolver reaches its models. A built-in agent may use its own
login (for example Codex or Claude Code with a subscription, where no API key exists); a custom Evolver may use its own
API key, or the optional `llm` tool. The run config lists the model endpoints the sandbox allows. In every case
the Evolver reports what it used, so the Trainer can account and enforce the budget:

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

For built-in agents the Trainer fills `usage` itself from the agent's session logs; usage through the `llm` tool is
metered by the Trainer directly. Budgets may therefore be set in tokens as well as in dollars.

### 2.5 Validation

Validation belongs to the Evolver. Whether it uses the validation set, which tasks, how many, when, and how it uses the
results are all its own decisions; the Trainer takes no part.

Whether an Evolver runs candidates at all (deploy a candidate, run tasks, score them) is part of its own definition:
prompt-only and MWM do not; GEPA and SkillOpt do. The framework does not require it. It offers an optional helper that
runs an adapter on the acting agent with the same deployment and scoring as `eval` and returns trajectories and scores;
an Evolver may use it or bring its own means. The helper accepts only data the run config grants (validation or
training, never test), and its cost counts toward the Evolver's usage. Using it makes the Evolver's working scores
comparable with the official evaluation.

Running candidates needs access to things the sandbox would otherwise block (validation data, the task environment, an
acting agent and its model, the scoring program). An Evolver declares what it needs in its manifest, and the Trainer
compares that with the run config before starting; if a needed access is not granted, the run is refused with a message
naming it, instead of failing midway:

```yaml
# evolver.yaml
class: my_evolvers.gepa:GepaEvolver
needs: [validation_data, environment, acting_agent]
```
 What the framework provides is access, when the run config grants it: the
validation data (2.2), the environment (2.2), and the tools of the task (2.4). Everything the Evolver runs stays in its
sandbox and is part of the usage it reports. Its validation scores are its own working numbers; the official result of
a training run is always the Trainer's evaluation on the test split.

### 2.6 What a call hands back

A call hands back two things. The structured result is the `StepResult` returned by `step` (2.3): status, decision,
grounds, summary, usage; it is the same for every Evolver. Everything else goes in `output/`, whose content each
Evolver defines for itself: reports, analyses, candidate comparisons, in any format.

The Trainer does not interpret `output/`: after each call it stores the whole directory unchanged in that step's run
records. One optional convention: if `output/report.md` exists, the Trainer treats it as the step's explanation and
refers to it in the commit message and in `CARD.md`.

### 2.7 One adapter per step

Every step must hand back exactly one adapter, in `adapter/`: the adapter the next step's training rollout will use.
This is mandatory. Which adapter that is, is the Evolver's choice: the candidate it just made, the previous adapter
unchanged, or another candidate it kept (for example a parent GEPA selects from its pool).

An Evolver whose method produces many candidates handles them itself: it keeps them in `state/` and may use them in
later steps or hand one back from `finalize`. The Trainer never sees them; the git tree records only the adapters
actually handed back. The decision in `StepResult` therefore means whether the adapter for the next step changed: if
it changed and passes the binding check, the Trainer commits it as a new node; if not, the next step continues from
the same node.

### 2.8 Responsibilities and limits

| The Evolver decides | Not allowed (and how it is enforced) |
|---|---|
| How to change the adapter | Changing the template's fixed parts (binding check) |
| Whether and how to use its tools, including `check_spec` | Reading test data (never in its reach) |
| Whether to validate, how, and with how much of its budget | Paths or network the run config does not grant (sandbox) |
| Which single adapter each step hands back | Handing back no adapter, or several (2.7) |
| What its Training State holds and in what format | Writing outside the workspace and granted accesses (sandbox) |
| Which adapter to hand back at the end (`finalize`) | — |
| How it reaches models, and which models | Not reporting usage (if it cannot, it says so) |

## 3. Trainer

The Trainer is the framework's outer loop. It handles everything around the Evolver: data, training rollouts, the
workspace, the binding checks, git, records, budgets.

### 3.1 One step

```text
1. Data          take this step's tasks from the training split, as the run config says
2. Rollout       run the current adapter on those tasks and score them
3. Material      write the results as learning_material/step_NNNN/ (3.3) and point `current` at it
4. Workspace     copy the current adapter into adapter/, mount task-info/, history/, state/, assemble the toolset
5. Evolver       call step(ctx) and receive a StepResult
6. Check         run check_spec on the adapter handed back
7. Commit        if the adapter changed and passes, commit it to git as a new node; otherwise keep the same node
8. Record        store usage, time, decision, check results and output/ in the run records
```

`setup` is called once before the first step and `finalize` once after the last; the final adapter may then be
evaluated with `eval`.

### 3.2 Data and batching

```yaml
data:
  tasks: tasks.jsonl
  train: splits/train.txt
  val: splits/val.txt
  test: splits/test.txt
  step_size: 12            # tasks per step (verl: train_batch_size)
  shuffle_seed: 0
trainer:
  epochs: 3
```

| Concept | Meaning |
|---|---|
| Split | Train, validation and test are given by the user in the run config; they must not overlap (checked before training). |
| Epoch | One full pass over the training split. |
| Step | Each step takes the next `step_size` training tasks; the order is reshuffled with `shuffle_seed` at the start of every epoch. |
| Last step of an epoch | Takes whatever remains when the split is not a multiple of `step_size`. |
| Validation split | Not used by the Trainer; granted to the Evolver only if the run config says so (2.5). |
| Test split | Used only by `eval`. |

### 3.3 Learning material

```text
learning_material/
  README.md        the format
  index.jsonl      one line per step: step, epoch, number of tasks, mean score, cost, whether the adapter changed
  step_0001/
  step_0002/
  ...
  current -> step_0007
```

One directory per step, so an Evolver sees at a glance how many steps there have been; `index.jsonl` gives the trend,
each step directory the detail. Every step directory has the same structure.

Different Evolvers want different views of the same results (GEPA short per-task records, SkillOpt full trajectories,
MWM citable evidence). The Trainer produces one standard format only, as complete as possible: for each task its input,
the full trajectory (the host's original log and a normalised message list), the final output, the score and the
judge's feedback, cost, time and API speed. Conversion to other views is the Evolver's own business, done inside its
`step`; the framework offers a library of converters for common views (short text records, full trajectories, a
citable evidence store), which an Evolver may use or replace.


### 3.4 When the binding check fails

The rule: **the adapter that goes into the next step is always valid.** The Trainer repairs, tells the Evolver what it
repaired, and leaves every judgement about the repaired adapter to the Evolver.

| Step | What the Trainer does |
|---|---|
| 1. Roll back | Only the affected files: a changed fixed file returns to the root template's version; a learnable file in a wrong format returns to the parent's version (a new file is removed); a file outside the learnable paths is removed; a part that fails to deploy (e.g. one tool) returns to the parent's version. |
| 2. Check again | Repeat check and rollback until the adapter passes (files depend on each other, e.g. `ADAPTER.md` naming a rolled-back tool). The worst case is the parent unchanged, i.e. the step made no change. |
| 3. Commit | Commit the repaired adapter as usual; the commit message lists the rolled-back files and the reasons. |
| 4. Feed back | Write `check_feedback.json` into the next step's `learning_material/step_NNNN/`: each rolled-back file and why. |

Whether the repaired adapter is still good, and what to do about it, is the Evolver's business: it sees
`check_feedback.json` in the next step and decides for itself. The Trainer does not re-validate.

### 3.5 Checkpoints and the final adapter

```yaml
trainer:
  save_freq: {steps: 5, epochs: 1}
```

| Item | Rule |
|---|---|
| Checkpoints | Set by the user in the run config (`trainer.save_freq`: every N steps and/or every N epochs); the Trainer tags those commits `ckpt/...`. No framework default policy beyond what the config says. |
| Final adapter | The Trainer always calls `finalize`. If the Evolver does not override it, the default returns the current adapter, i.e. the last step's. If it does, the Evolver may pick another one (e.g. GEPA choosing the best candidate in its pool), and that one is final. |
| Final tag | The final adapter passes the binding check (3.4), is committed if new, tagged `final`, and gets its `CARD.md`. |

### 3.6 Run records

Records live in `runs/<run_id>/`, never in the adapter (1.3).

| File | Contents |
|---|---|
| `config.yaml` | The full run config as given, plus the template, Evolver and task versions. |
| `steps.jsonl` | One line per step: epoch and step, task list, training-rollout scores, cost, time, API speed, the Evolver's `Usage`, decision, check and rollback results, commit hash. |
| `step_NNNN/` | The Evolver's `output/` for that step, stored unchanged, plus its `StepResult` and a snapshot of `state/` (3.11). |
| `learning_material/` | All the material of 3.3 (the training trajectories live here). |
| `summary.json` | Written at the end: total cost and time (split into rollouts and Evolver), the final adapter, the checkpoints. |

### 3.7 Budgets

| Kind | Config | Enforcement |
|---|---|---|
| Hard budgets | `trainer.budget: {usd: ..., tokens: ..., hours: ...}` | Enforced by the Trainer; the run stops when one is reached. |
| Iteration budget | `trainer.epochs` (and `data.step_size`, 3.2) | The run ends after the last step. |

Hard budgets are checked as often as the Trainer can observe spending, not only between steps:

| Check point | What is observed |
|---|---|
| After each training-rollout task | Rollout cost and time |
| During an Evolver call | Wall time continuously; usage through metered tools and the optional `llm` helper as it happens; for built-in agents, usage read from the session log while it runs |
| After each call returns | The `Usage` reported in `StepResult` (the only source for a black-box Evolver besides wall time) |

When a hard budget is reached the Trainer stops at once: the step in progress is discarded, the adapter of the last
completed step stays current, `finalize` is called, and `summary.json` records the reason. A per-call timeout is the
same mechanism applied to wall time.

How long a run iterates is the user's decision, not the Evolver's. By default an Evolver cannot end a run early; one
that believes it has converged returns the adapter unchanged (`decision = "reject"`) for the remaining steps. The user
may allow early stopping with `trainer.allow_early_stop: true` (default `false`); then a `StepResult` with
`status = "stop"` makes the Trainer start no further step and call `finalize`. Without that permission `"stop"` is
recorded and treated as `"done"`.

### 3.8 Checking the settings

Checking the settings is a separate command, outside the run: `harnessadapter check --config run.yaml`. The user runs
it when they want to and starts training once they are satisfied; training does not run it for them. One exception:
`train` compares the Evolver's `needs` with the run config's `grant` at start and stops with an error if anything is
missing, because the Evolver's provider specified those needs and without them the Evolver should not run at all. As with model
training, a wrong setting can waste a lot of money, and making sure the settings are right is the user's
responsibility.

| What `check` looks at | Example of a problem it finds |
|---|---|
| Template | The root adapter fails `check_spec` or does not deploy |
| Evolver | The class does not load; an access in its `needs` is not granted; resume is requested but not supported |
| Task | `task-info/README.md` missing; train, validation and test overlap; the judge cannot score a task |
| Budgets | No hard budget or no iteration budget |
| API speed | One short probe; the measured speed is reported |

Disclaimer, printed by `check` and stated in its documentation: it checks only the basic items above. Passing `check`
does not guarantee that the settings are free of problems or that the run will succeed.

### 3.9 Evaluation

```bash
harnessadapter eval --config run.yaml --adapter final      # or a tag, a commit, a local path; `root` = the template
```

| Item | Rule |
|---|---|
| Data | The test split only |
| Deployment and scoring | The same as the training rollouts: the same acting agent, the same judge |
| Repeats | `eval.repeats` in the config; reports the mean and the share solved at least once |
| Records | Per-task score, cost, time and API speed under `runs/<run_id>/eval/<adapter>/` |
| `CARD.md` | When the evaluated adapter is `final`, its results are written into its card |

`eval` evaluates exactly the adapter it is given; it has no built-in baseline. A baseline is one more `eval` of the
template (`--adapter root`), run by the user when and as they choose.

### 3.10 Commands

| Command | Purpose |
|---|---|
| `harnessadapter check --config run.yaml` | Check the settings (3.8) |
| `harnessadapter train --config run.yaml` | Run the Trainer (3.1–3.7) |
| `harnessadapter eval --config run.yaml --adapter ...` | Evaluate one adapter on the test split (3.9) |

### 3.11 Resuming after a crash

A crashed run resumes from its last completed step, not only from a checkpoint: every step already leaves what a
resume needs.

| Needed to resume | Where it comes from |
|---|---|
| The adapter | That step's git commit (3.1) |
| The Evolver's `state/` | A snapshot the Trainer stores after every step, next to the commit |
| Which tasks come next | Recomputed from the seed, the epoch and the step number (3.2) |
| Learning material and budget spent so far | `learning_material/` and `steps.jsonl` (3.3, 3.6) |

Procedure: find the last step fully recorded in `steps.jsonl`; restore the adapter and `state/` to that step; discard
the step that was in progress and start the next one; keep counting the budget from where it was. If the run config
asks for session resume but the Evolver's session cannot be found, the Evolver gets its fresh-start prompt and the
record says so. Training rollouts already finished in the crashed step are not reused.

```yaml
trainer:
  resume_mode: auto      # auto: continue an unfinished run; disable: start over; or a given step / checkpoint
```

## 4. Evolving task

What the task user provides, once a template and an Evolver have been chosen.

Design for the worst case: the user knows only that an adapter template and an Evolver exist and that someone
recommended them; they know nothing about how either works. They care about their task and the host agent they want
to improve. So everything in part 4 is about the task and the host-agent pipeline; nothing in it may require
knowledge of the template's or the Evolver's internals. Choosing the template and the Evolver is naming them in the
run config; their parameters have defaults.

### 4.1 Parts

| Part | What it is | Read by |
|---|---|---|
| `task-info/` | The task in words: what the tasks are, the goal, how answers are scored, the rules | Evolver |
| Data | `tasks.jsonl` (one task per line) and the train / validation / test split | Trainer |
| Judge | The scoring program: a task and the agent's output in, a score and feedback out | Trainer (rollouts and `eval`) |
| Environment | Where the adapter is deployed: the acting agent's host and model, the environment's tools, isolation | Trainer |
| Run config `run.yaml` | Connects the above with the template and the Evolver; trainer settings, budgets, checkpoints, permissions (4.6) | `check`, `train`, `eval` |

Two files once proposed for `task-info/` are superseded: `learning_material.md` (the material's format is described by
the Trainer in `learning_material/README.md`, 3.3) and `evaluation.md` (validation is the Evolver's own job, 2.5).

### 4.2 `task-info/`

What goes into `task-info/` is task-specific. The framework ships a basic template with the files it considers
necessary; the user fills it in and may add any other files that give the Evolver more information. The only fixed
point is `README.md`, the entry that the Evolver reads first and that lists the other files.

| File in the basic template | Content | Lean example |
|---|---|---|
| `README.md` | Entry: the task in one paragraph, the list of files | — |
| `task.md` | What a task looks like; the acting agent's input and output | A Lean theorem; a proof without `sorry` |
| `goal.md` | What to optimise; priorities when there are several aims | Solved > time > cost |
| `judge.md` | How answers are scored and what the feedback looks like, in words (no answers) | Correct only if it compiles and the target statement is unchanged |
| `rules.md` (optional) | Constraints the user wants stated, if any | No facts about individual problems in the adapter |
| `environment.md` (optional) | The acting agent's environment: tools, limits | Five agents in parallel, 3-hour cap, a Lean compiler |

### 4.3 Task rules

Part 4 is what a user must do to run an existing template with an existing Evolver; it does not include anything
that helps an Evolver produce a better adapter. Rule checking is therefore not a task part: an Evolver that wants
rule checks (a program, an LLM review, anything else) builds them into its own pipeline. The task user at most states
constraints in words (`rules.md`, optional). The Trainer does not enforce task rules; the only binding check is
`check_spec` (3.4).

### 4.4 Judge

The judge belongs to the task and its environment, and the user defines it: a task and the host agent's output in, a
score and feedback out. The Trainer passes everything the judge returns into the learning material unchanged (3.3);
there is no separate setting for how much feedback the Evolver sees. The richer the feedback, the more the Evolver can
learn from it. Whether the feedback reveals the reference answer is the user's decision when writing the judge, made
from the task's point of view alone.

### 4.5 Acting-agent pipeline

The user designs the complete acting-agent pipeline: how one task is run (host, model, number of agents and how they
work together, launcher, time cap, isolation). The framework is acting-agent agnostic: it never modifies this
pipeline, and neither the template nor the Evolver is written for a particular one.

To the Trainer the pipeline is a black box with one interface:

```python
def run(task: dict, adapter: Path) -> RunOutput:   # output for the judge + the raw log (+ cost, time, API speed)
    ...
```

The adapter enters the pipeline only through its standard exports (context, MCP tools, roles, reactions), which the
pipeline deploys into its host. The same `run` serves training rollouts, `eval`, and the optional acting-agent helper
an Evolver may be granted (2.5). The Evolver learns about the pipeline only from what the user chooses to write in
`task-info/environment.md` and from the trajectories in the learning material.

### 4.6 Run config

The run config is the counterpart of verl's training config: one YAML file describing one training run, used by
`check`, `train` and `eval`. YAML is used for comments and layered overrides.

The file follows verl's layout: one section per component, and a `trainer` section for the outer loop. The framework
ships a default config; the user's file and command-line overrides change only what they name:

```bash
harnessadapter train --config run.yaml trainer.epochs=5 evolver.name=gepa
```

```yaml
# run.yaml: only what differs from the framework defaults needs to be written

data:                        # verl: data
  tasks: tasks.jsonl
  train: splits/train.txt
  val: splits/val.txt
  test: splits/test.txt
  step_size: 12              # tasks per step (verl: train_batch_size)
  shuffle_seed: 0

task_info: task-info/        # read by the Evolver (4.2)

pipeline:                    # the user's acting-agent pipeline (4.5); verl: actor_rollout_ref
  run: pipeline.py:run
  concurrency: 8
  params: {}                 # passed through to the pipeline unchanged

judge:                       # 4.4; verl: custom_reward_function
  path: judge.py
  name: score

template:                    # the starting point, like initial weights (part 1)
  name: lean-collab
  version: "1.0"

evolver:                     # verl: algorithm (part 2)
  name: gepa
  resume: false              # continue the Evolver's session between steps (2.1)
  params: {}                 # empty = the Evolver's defaults

grant:                       # which of the user's resources the Evolver may use (2.5)
  validation_data: true
  validation_answers: false
  environment: false
  acting_agent: true

trainer:                     # verl: trainer (part 3)
  run_name: lean-gepa-01
  output_dir: runs/
  epochs: 3
  allow_early_stop: false
  resume_mode: auto          # 3.11
  save_freq: {steps: null, epochs: 1}
  budget: {usd: 200, tokens: null, hours: 24}

eval:                        # 3.9
  repeats: 5
```

| Section | verl counterpart | Who needs to understand it |
|---|---|---|
| `data`, `task_info`, `pipeline`, `judge` | `data`, `actor_rollout_ref`, `custom_reward_function` | The task user: all of it is their own |
| `template`, `evolver` | initial model, `algorithm` | Nobody beyond naming them; parameters default |
| `grant` | — | The task user: which of their resources may be used |
| `trainer`, `eval` | `trainer` (incl. `resume_mode`) | The task user: how long, how much money, how many repeats |
