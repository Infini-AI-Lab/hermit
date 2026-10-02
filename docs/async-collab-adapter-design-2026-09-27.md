# Async collaboration adapter — design (draft, 2026-09-27)

Status: implemented (`harnessadapter/architectures/async_collab`, 2026-09-27); first five-problem run fc_ds2h_collab. Successor of the `lemma_board` architecture.
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
- Limits: ≤ 5000 characters per message (raised from 300 on 2026-09-27: with 300, about half of the
  messages of the first five-problem run were rejected and the rest were compressed to the point of being
  hard to read); per-agent rate limit of 6 messages per 10 minutes.
- Delivery: every message (type, author, full text) goes into everyone's `[team]` delta, the last ten
  since the reader's previous call — all messages, not only those addressed to the reader, so that each
  agent keeps a global sense of what the others are doing; the whole channel is in `library/channel.jsonl`.
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

## 11. Two phases: parallel search, then division of labour (added 2026-09-27, after the first run)

Status: design under discussion; not implemented. Evidence: the five-problem run `fc_ds2h_collab` (20
trajectories read in full) and the earlier readings of §1.

### 11.1 What the first run showed

The team's gain came from two sources, and only one of them was harvested:

- **Sampling diversity** — several agents attacking the same thing, one of them getting through. This is
  where best@5 beats avg@5 by a factor of two. The run did this by accident and did it badly: in every
  problem the agents announced their routes, read each other's, declared the team "converged", and then
  all formalised the same route from scratch. Diversity was lost to convergence, and the redundancy was
  spent uniformly — five copies of the easy steps as well as of the hard ones.
- **Division of labour** — different agents proving different parts, total time ≈ the longest part. Not
  harvested at all: nobody decomposed (five sorry skeletons in four problems, one of them claimed by a
  teammate and closed by its author 44 s later); every winner proved its file end to end; the library was
  used to skip a step that a teammate happened to have done, never to take a part.

So the goal is not to choose between the two modes but to **spend redundancy where the difficulty is**:
easy parts one agent, hard parts several, and those several on different routes.

### 11.2 The two phases

The problems of this benchmark are usually hard in the reduction, not in the number of parts: until the
right reduction is found there is no map to divide. Hence the modes are separated in time.

**Phase 1 — parallel search for a reduction.** All agents work on the whole target. The harness's job is
to keep the routes different, not to make them agree:

- the channel shows each agent's declared route; the `[team]` block tells an agent when its route is
  already taken ("A3 is on the p-adic route; take another, or take a part of it");
- "converged" is treated as a warning, not a success: when three or more agents declare the same route,
  the block says so and names the routes nobody is on;
- the exit condition is a **verified reduction in the library**: a compiling skeleton whose target proof
  is complete modulo sorry helpers (the target depends on the helpers, the helpers are the open subgoals).
  Its author's `check` records it as the team's map.

**Phase 2 — division of labour on the map, redundancy by difficulty.** Once a map exists the `[team]`
block and the resume message switch to it: the open subgoals, who is on each, how long each has been open,
how many failed checks it has seen. Allocation stays soft (§7), but the ranking changes:

- an open subgoal with nobody on it ranks first; a subgoal with one agent on it is hidden from the others
  while it is fresh;
- a subgoal that stays open past a threshold (time since claim, or failed checks by its owner) is reopened
  to a second and then a third agent — with the owner's attempted approach attached, so the newcomers try a
  different one;
- a refuted subgoal takes its skeleton off the map and the team returns to Phase 1 for that part.

The merge point is the library as it is today: verified helpers enter it, and the map's author (or anyone
holding the skeleton) pulls them into the target proof. This is the one part of the current adapter that
already works.

### 11.3 What this needs beyond the current adapter

1. **Decomposition before allocation.** Agents do not write skeletons on their own. Either the first
   Problem.lean `check` of an agent must carry a skeleton (a target proof with sorry helpers), or one role
   ("mapper") writes the first skeleton while the others search.
2. **A difficulty signal.** Per subgoal: time open, number of owners so far, failed checks, refutations of
   neighbours. The harness already sees all of these through `check`.
3. **A route register.** Phase 1 needs to know which routes are taken; today this is only in free-text
   `plan` messages. A `plan` message with a route name (or a typed field) makes it countable.
4. **A phase indicator** in the `[team]` block and in updates.md, so an agent knows whether it is expected
   to search or to take a part.

### 11.4 Open questions for this section

- Who writes the map when several skeletons compete (rank by how much of the target they close? keep all?).
- The reopening thresholds (minutes open, failed checks) — to be set from the subgoal histories of the runs.
- Whether Phase 1 should cap the number of agents on one route (hard) or only warn (soft, consistent with §7).
- How to keep an agent from abandoning a part it owns when the `[team]` block shows a more attractive one.

## 12. Three design points from the activity view (user, 2026-09-28 23:29)

After watching the per-agent activity timeline of r13 (what each agent does minute by minute), the user
set three points for the coming versions. The harness is then a manager: it assigns, interrupts and
resumes; an agent is never in a state where nobody can say what task it is on.

**12.1 Every agent always has one explicit task.** An agent may not decide freely what to do. At every
moment it is either building the map (phase 1) or solving one node of the map (phase 2), and the plan
records which. Work outside the plan — helper lemmas that serve no node, exploration after the map
exists — is not a state the harness allows. (r13 evidence: after a map at 13 min, four agents of 1000-4
stayed 34–53 min away from the team; the fastest solves had every agent on a node within 14 min.)

**12.2 Lean verification is never a bottleneck.** It uses only CPU and the machine has 128 cores, so
every compile the harness runs — grades, ingest checks, final grades — runs in parallel without a pool
limit; a proof must be recognised as soon as its compile ends. (r13 800-11: a proof saved at 22:31 was
recognised at ~23:32 because grades queued behind each other and behind ingest checks. Fixed in two
steps on 2026-09-28: a separate grade pool; final grades submitted at once in their own directory. Still
to do: no pool limit at all, and grading from the agent's own `check` compile when the file hash
matches, so a proof costs no second compile.)

**12.3 Best-of-n with a signal.** Several agents may work on the same task on purpose: at the start all
of them design the map (and gain understanding of the problem); later several may attack one statement.
When one of them finishes, the others on that task are signalled within about a minute: their sessions
are stopped and resumed with the news — when it was finished, by which agent, and what the solution is
(the map, or the proof's name in the library) — so that they read it and move to the next task.
(v14 / r14 does this for parts: a part proved by one agent interrupts the others on it, with the prover
and the lemma to pull. The map-design phase — the first map interrupting everyone still searching — is
the next step.)

Sequencing agreed with the loop's one-hypothesis-per-round rule: r14 = interrupt on a proved part
(12.3, parts); r15 = interrupt on the first map (12.3, map phase) with the map in the resume message;
r16 = the hard rule of 12.1 (a `check` while on no node is refused; the plan is the only state).
12.2 is infrastructure and is fixed as found, outside the hypothesis sequence.
