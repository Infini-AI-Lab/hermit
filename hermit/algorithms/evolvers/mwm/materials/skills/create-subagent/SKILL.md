# create-subagent — when a sub-problem deserves its own agent, and how to write one

Read when the record shows hosts repeatedly working through the same
sub-problem inside their episodes. "This environment" says which of the
two situations below applies here; read that section.

## What a sub-agent is

A prompt you write, agents/<name>/SPEC.md: the first line is the
trigger a host reads in the catalog; then the shared entry header
(scope, last_confirmed_t, derived_from); then the prompt. A host hands it
a sub-task in one sentence; it works on that sub-task and hands back one
final message, which may carry an answer — submitting is the host's. It
never delegates further. How it runs depends on the host's harness.

## When

All of these, or it is not a sub-agent:
- the sub-problem needs several steps of interaction with the
  environment (otherwise the answer belongs in the briefing);
- it needs judgement along the way (otherwise it is a tool);
- it recurs across episodes in the same shape (otherwise a fixed
  procedure does not pay);
- a host can say what it wants in one sentence, and one message can
  answer it (otherwise it is the whole episode, not a part of it);
- its result is an input to the host's decision, not the decision.

The case for it is in the record: several hosts exploring, retrying or
failing on that same sub-problem. Write the addresses into
derived_from.

## Where the host's harness cannot start one (the wrapper runs it in the host's seat)

The sub-agent runs on the host's own model with the host's tools except
the one that submits, on the host's budgets. It receives everything the
host received — the package, the episode's input — plus your prompt.
Write one self-sufficient prompt: what it works on, how it proceeds,
what it hands back and in what form, and when to report that it does
not apply here. One sub-task per spec. Do not restate knowledge the
package already carries; point at it. Its turns appear in the episode
record marked by: <name>, and its full trajectory is under
/evidence/delegates/.

## Where the host's harness starts one itself

The host reads your spec's text and starts a sub-agent through its own
harness; what that sub-agent can do, how long it may run and where its
record lands are the harness's, not yours. Write the prompt for THAT
sub-agent: self-contained (the harness, not the wrapper, assembles what
it sees, so do not assume it has the package or the episode's input —
state what the host must pass along), with the same parts as above —
what it works on, how it proceeds, what it hands back and in what form,
when to report that it does not apply. The record of what it did is the
harness's own.
