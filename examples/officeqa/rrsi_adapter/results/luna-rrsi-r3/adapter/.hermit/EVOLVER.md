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
for is replaced by what the diff reads as, so an edit counts as a component only with the evidence named here. A
component the acting agent cannot carry (the task information says which) is never asked for.

| Component | What it is in an adapter | What the diff must show |
|---|---|---|
| `prompt` | the instructions' prose: `ADAPTER.md`, or text another file injects into the agent's context | anything else: prose counts as `prompt` wherever it is, a mandatory procedure written in prose too |
| `control_flow` | hooks that gate or redirect the agent (before a tool runs, when it tries to stop) | a hooks file, or a `PreToolUse`, `Stop`, `SubagentStop` or `UserPromptSubmit` entry |
| `output_plumbing` | what tool output looks like when it reaches the agent | a `PostToolUse` hook entry |
| `context_mgmt` | what the agent keeps in view over a long session | a `SessionStart`, `PreCompact` or `PostCompact` hook entry |
| `config` | constants and settings the adapter's own files carry | an UPPER_CASE constant line, or a `.toml`/`.yaml`/`.ini`/`.cfg`/`.json` settings file |
| `client_tool` | an executable helper the agent runs, with the instruction that says when | a file under `scripts/`, `tools/` or `bin/`, or a `.py`/`.sh`/`.js`/`.ts`/`.rb`/`.pl` file |
| `skill` | a procedure kept out of the always-on instructions and read on demand | a file under a `skills/` directory, or a `SKILL.md` |
| `memory` | a store of general lessons the agent reads: never task-specific facts | a file under `memory/`, `notes/` or `lessons/`, or a `MEMORY.md`, `NOTES.md` or `LESSONS.md` |
| `subagent` | a role the agent delegates a bounded piece of work to | a file under `sub_agents/`, `agents/` or `roles/` |

`client_tool`, `skill`, `memory` and `subagent` add machinery; the others change text or settings.
