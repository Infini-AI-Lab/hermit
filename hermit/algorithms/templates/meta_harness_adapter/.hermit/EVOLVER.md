# Meta-Harness adapter

The adapter is what a fixed acting agent is given for its work on a task, beyond the task itself. The agent, its
model and the tasks do not change; the adapter is the only thing that does.

`ADAPTER.md` is given to the agent as its instructions at the start of every task; it is the only file sure to
reach the agent. Whether and how the adapter's other files reach it depends on the agent; the task information says
how. Anything else you build into the adapter is used only if the agent knows of it, and `ADAPTER.md` is where you
tell it what is there and how to use it.

What the adapter contains, which files it has and how they are organised is yours to decide. Every file outside
`.hermit/` may change; `.hermit/` (this guide) is fixed.
