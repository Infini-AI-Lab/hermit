# Async collaboration adapter — design (draft, 2026-09-27)

Status: design under discussion; nothing implemented. Successor of the `lemma_board` architecture.
Evidence base: the trajectory readings in
`~/code/lean-results/fc_screen/notes/async-collab-trajectory-findings-2026-09-27.md`
(14 shared-notes runs, 5 lemma-board runs, DeepSeek V4.1 Flash and gpt-6-sol on formal-conjectures).

## 1. Problem

Five agents prove the same theorem asynchronously, each in its own workspace, under one wall-clock cap.
With a free-form shared folder (scenario s2) the team solved 24/54 problems against 23/54 for five
independent agents (best@5) and was no faster than the fastest independent run: the channel added nothing
beyond parallel sampling. The readings explain why:

- Nothing reusable crossed the channel: workspaces were invisible, notes were prose, and in 25 solved runs no
  teammate lemma entered a winner's proof. The one channel that did transfer content — the lemma board — put
  12 teammate lemmas into a winner's target proof.
- Agents looked at the channel only at start (empty), after context compaction, and when stuck; they never
  "checked periodically"; some slept for up to 49 minutes polling.
- Nobody divided labour; three to four agents formalised the same route in parallel in most runs.
- Wrong claims were never retracted and were rediscovered by two to three readers each.
- "Everyone is stuck" propagated through the notes and became the stated reason to give up.
- Writing conflicted with the stop rule (an agent skipped its final note "to be safe").
- Board outcomes were decided by whether a verified reduction existed early, not by how many lemmas
  were proved; v0 had no claims at all, v6 had claim churn (62 claims, four agents on one lemma).

## 2. Goals and non-goals

Goals: (1) verified proof content moves between agents without their cooperation; (2) every agent has an
accurate picture of what is proved, what is open and who is on what, at the moments it decides; (3) idle
or stuck capacity is redirected instead of confirming the team's pessimism; (4) all of this as a
harness adapter — context, MCP tools, optional roles — with no change to codex or to the shell.

Non-goals for the first version: hard task assignment; private agent-to-agent messages (the channel is
public and typed, §4a); a decomposition algorithm; timers or any action not triggered by an agent's own
call or a session boundary.

## 3. Shared state: three layers by trustworthiness

| Layer | Content | Writer | Delivered |
|---|---|---|---|
| Facts | Verified lemmas (statement, proof, author, time); refutations (what, evidence, by whom) | harness only | pushed |
| Structure | Open subgoals (sorry helpers of compiling skeletons); who is on which; refuted/affected marks | harness (observed) | pushed |
| Channel | Ideas, plans, feedback, requests — short typed messages (§4a) | agents, via `say` | headline pushed, full text readable |

The facts and structure layers live in a shared directory mounted read-only in every jail
(`library/*.lean`, `library/index.md`, `library/state.json`); only the MCP server writes it. The
channel is an append-only `library/channel.jsonl` (author, time, type, text, optional reference), also
written only by the server. Each agent keeps a writable scratch subfolder (`shared/A*/`) for its own
files; it is not a communication channel and nothing in it is pushed.

## 4. Tools (MCP, declared in adapter.json)

| Tool | Input | Effect | Returns |
|---|---|---|---|
| `check` | — | compiles the caller's Problem.lean (replaces check.sh); extracts sorry-free helpers → verify → library; extracts sorry helpers → open subgoals; diffs the file against the previous check → "working on" | compiler errors and the target's axioms, then the `[team]` delta |
| `pull` | lemma name | inserts a verified lemma before the target (as board_pull) | source, then the `[team]` delta |
| `refute` | name, evidence | marks a subgoal or claim refuted; skeletons depending on it are marked affected | confirmation, then the `[team]` delta |
| `say` | type, text, optional reference | appends a message to the channel (§4a) | confirmation, then the `[team]` delta |

No `claim`, no `list`. The full state is readable as files.

### 4a. The channel

A Slack-like stream for ideas, plans, feedback and requests, designed so that what the readings showed to
be harmful (status reports, collective pessimism, dumps) has no place in it:

- Types: **idea** (a route worth trying), **plan** (what I am doing next), **feedback** (an assessment of a
  lemma, subgoal or message — must carry a reference), **request** (what I need from others). There is no
  status type and no conclusion type: "this cannot be done" can only be said as feedback on a specific
  subgoal with evidence, which is the filter that keeps refutation and drops despair.
- Limits: ≤ 300 characters per message; code is referenced (a library lemma, a file in the author's
  scratch folder), never inlined; per-agent rate limit.
- Delivery: every message's headline (type, author, first 80 characters) goes into everyone's `[team]`
  delta — all messages, not only those addressed to the reader, so that each agent keeps a global sense of
  what the others are doing; the full text is read from `library/channel.jsonl`.
- Harness use: a **plan** message updates the "working on" map immediately (earlier and more reliable than
  the diff); a **feedback** message that references a subgoal and carries evidence marks it *disputed*,
  which lowers its rank; **request** messages stay listed in the delta until answered by a message that
  references them.
- Relation to `refute`: feedback is an opinion ("I doubt X holds"), refute is a fact ("X fails, n = 7").
  Feedback goes to the channel, refutation to the facts layer.

The channel is an experimental variable: runs are compared with and without it, since there is no evidence
yet that free communication raises the solve rate.

`[team]` delta — appended to every tool return, only the changes since the caller's last call, omitted when
nothing changed:

```
[team since your last check]
new verified: window_bound (A3): 5n < m ≤ 30n → …            pull with: pull window_bound
open, nobody on it: gap_case_p3 (from A1's skeleton): …
refuted: twolevel_bound — A5, counterexample n = 7
you are on: delta_bonus — A4 is on it too
channel: [plan] A4: taking gap_case_p3 via Nat.log window · [request] A2: anyone with an explicit
  construction for m = 3? · [feedback] A5 on twolevel_bound: fails at n = 7, see shared/A5/aff2.py
full state: 12 verified, 4 open, 1 refuted, 3 messages — library/index.md
```

## 5. Harness behaviour (all triggered by tool calls or session boundaries)

| Trigger | Action |
|---|---|
| any `check` | compile; extract and verify sorry-free helpers (in the submitter's own file context, dependency-aware); register sorry helpers as subgoals; normalise statements by the grader's type hash so identical subgoals merge across agents; update "working on" from the diff and from the first compile error's declaration; mark an agent idle after N checks without change |
| state change | recompute each agent's delta from its last-seen pointer; rank open subgoals: verified share of the skeleton they belong to, number of agents on them (0 preferred), number of skeletons sharing them, refuted → excluded |
| `refute` | flip status; mark dependants |
| `say` | append to the channel; a plan updates "working on"; feedback with evidence marks the subgoal disputed; a request stays in the delta until a message references it |
| agent ends its session unproved | launcher resumes it with the `[team]` delta plus one concrete open subgoal ("pick one of these"), never the bare "continue working" |
| no compiling skeleton exists / an agent gave up | (role policy, §6) the resume message or delta carries a task: produce a compiling skeleton, or verify a named claim |
| first proof of the target | stop every agent (as today) |

Delivery is piggybacked on the agent's own tool calls. Measured on the 315 s2 agents: compile/check calls are
23 % of all commands, median gap between checks 1 command, 90th percentile 5, no agent without checks —
so a delta arrives within a command or two without any host-side hook. A shell-profile hook (100 % of
commands) is possible on the host side but outside the adapter contract; kept as a fallback for models
that rarely check.

## 6. Roles: dynamic (framework C)

Every agent is a prover by default. The harness assigns a temporary task type by state, delivered through
the delta or the resume message; the agent may decline (logged, no penalty):

- decomposer — when no compiling skeleton exists after T minutes, or all skeletons are refuted;
- verifier — when an agent has given up: check a named unverified claim or refute a subgoal by search;
- prover — otherwise, with a suggested open subgoal.

Give-up statements never enter the shared state; only facts and structure do.

## 7. Allocation is soft

The harness never assigns exclusively. It makes the map accurate ("you are on X — A4 is on it too", "open,
nobody on it: …") and ranks suggestions; adoption is logged. Success metrics for the allocator are the
duplication rate (same subgoal formalised by ≥2 agents at once; today 3–4 of 5) and coverage (open subgoals
with at least one agent), not adoption.

## 8. Adapter contract fit

Uses the three exports only: Context (AGENTS.md block: use `check`, meaning of the `[team]` lines, where the
library lives), Tools (four MCP tools), Roles (none in v1). The library directory is adapter runtime state;
mounting it into the jails and composing the resume message are transport, done by the launcher. Two items
need launcher cooperation: the resume message content, and the MCP tool timeout (a Lean compile inside
`check` can take minutes; raise the timeout to 10 minutes or make `check` asynchronous).

## 9. Evaluation

Same protocol as before, so the result is comparable: DeepSeek V4.1 Flash, 5 agents, 2 h, semantic grader.
Problems: the 7 solved 1–4 of 5 times by single agents (800-11, 1000-11, 1000-2, 1000-21, 1000-22, 1000-4,
1000-7) for solve rate, the 16 solved 5/5 for time, and a sample of the 0/5 ones. Report against best@5
(solved) and fastest-of-5 (time), plus: library lemmas used in the winner's target proof, duplication rate,
coverage, delta adoption, refutations issued, channel volume by type and how often a plan or feedback
message precedes a change in the reader's file. The channel is switched on and off as an arm.

## 10. Open questions

- Role policy thresholds (T for "no skeleton"; what counts as a give-up while the session is alive).
- Whether to rank between competing skeletons or stay neutral early.
- Whether to also scan scratch .lean files in the workspace for "working on".
- Whether a host-side active channel (codex app-server) is worth adding for the one interrupt that matters
  (target proved elsewhere → stop).
