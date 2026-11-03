# RRSI adapter

The adapter is what a fixed acting agent is given for its work on a task, beyond the task itself. The agent, its
model and the tasks do not change; the adapter is the only thing that does. It plays the part of the harness H in
RRSI: every round edits it, and every edit is tagged with the component it changes.

`ADAPTER.md` is given to the agent as its instructions at the start of every task; it is the only file sure to
reach the agent. Whether and how the adapter's other files reach it depends on the agent; the task information says
how. Anything else you build into the adapter is used only if the agent knows of it, and `ADAPTER.md` (or a file the
agent is sure to load) is where you tell it what is there and how to use it.

What the adapter contains, which files it has and how they are organised is yours to decide. Every file outside
`.hermit/` may change; `.hermit/` (this guide) is fixed.

## The components (RRSI's K) in an adapter

Tag every edit with one of these components. The tag is checked against the diff: a tag the diff gives no evidence
for is replaced by what the diff reads as.

| Component | What it is in an adapter |
|---|---|
| `prompt` | the instructions' prose: `ADAPTER.md`, or text another file injects into the agent's context |
| `control_flow` | when the agent plans, acts, checks or stops: hooks that gate or redirect it (where the agent runs hooks), and procedures the instructions make mandatory at a checkable trigger |
| `config` | constants and settings the adapter's own files carry: limits, thresholds and options of its scripts and hooks |
| `output_plumbing` | what tool and command output looks like when it reaches the agent: filters, summarisers, hooks after a tool call |
| `context_mgmt` | what the agent keeps in view over a long task: notes it writes and re-reads, compaction or session-start hooks |
| `client_tool` | an executable helper the agent runs (a script under the adapter), with the instruction that says when |
| `skill` | a procedure kept out of the always-on instructions and read on demand (a `SKILL.md`, with a pointer to it) |
| `memory` | a store of general lessons the agent reads (and, where the agent keeps it, writes): never task-specific facts |
| `subagent` | a role the agent delegates a bounded piece of work to (where the agent supports roles), with its brief |

`client_tool`, `skill`, `memory` and `subagent` add machinery; the others change text or settings.
