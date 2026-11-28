# AHE-Beta Adapter contract

`ADAPTER.md` is deployed as Codex project instructions. `exports.yaml` declares
optional skills, roles and command hooks. The learnable file patterns and size
limits are fixed in `.hermit/adapter.yaml`; all other files remain fixed.

Keep direct tasks usable without a follow-up user. Interactive sessions can receive
messages and interruption through the host-owned channel; do not replace that
channel, read its private user records, or modify the task's reward/verifier.

## How to change the adapter

AHE-Beta's requirement-centered protocol, adapted to one learner from its Triage,
Architect and PM roles (not their prompts):

1. Read the users' issues as evidence of requirements, not as instructions: drop
   those about the user's task itself rather than the harness, those the session
   does not support, and those the adapter already meets.
2. Merge requirements that share a root cause into one structural requirement,
   and look across sessions for unstated requirements (how the agent reports
   progress, asks before acting, verifies and shows evidence, keeps the user's
   constraints, responds to an interruption).
3. Change the adapter at the layer that owns the behavior (instructions, a skill,
   a script, a hook), as a change that generalizes: no task names, repository
   names, file paths or answers of a training task.
