# Async-collab "tree mode": the team builds the proof tree together from minute 0 — design

Date: 2026-09-30. Status: draft for review; nothing implemented.
Context: [async-collab-adapter-service-design-2026-09-30.md](async-collab-adapter-service-design-2026-09-30.md) (the adapter as a service),
`~/code/lean-results/fc_collab_auto/LOG.md` (rounds r3–r35).

## 1. Problem

The current protocol has two phases. In phase 1 every agent searches alone until one of them writes a *map* (a
Problem.lean whose target compiles from sorry helpers); the map is delivered and phase 2 splits the work. Over
r26–r34 (four problems, 36 runs) the first map came at a median of 24 min (15–71), and the run spent a median **30%
of its time** before it; 5 of 36 runs never had a map. Cost is proportional to run time (cost ≈ $2.78 per hour of a
five-agent run, r = 0.83), so roughly 30% of the spend goes to a phase where five agents search separately and often
duplicate each other. The map rules make it slower: a map is written in three passes with evidence for every part,
"do not run check on a map to be first", and a map of fewer than three parts is asked to split.

## 2. Decision (user, 2026-09-30)

- No phases. From minute 0 the plan is a tree whose root is the target, and **every agent may keep updating it**.
- On a node an agent does one of two things: **split** it or **prove** it.
- **One splitter at a time per node, the root included** (user, 2026-09-30, revised): while an agent is splitting X,
  nobody else may start splitting X; once its decomposition is registered (or it drops), the next agent may split X
  again (an alternative).
- **Nobody waits for the split.** An agent with no node to split works on the problem directly (`plan take <root>
  mode=prove`): it attempts the proof and states the facts it needs as helper lemmas; every helper it verifies enters
  the team library at its next `check` (as today). When the split arrives, a child whose statement is already in the
  library closes at once, and the others pull what fits — the early effort is not lost. The splitter is told to look
  at the library first and to reuse its lemmas as children where they fit.
- **Splits are coarse first.** Agents are told to split the root coarsely (two or three big steps) and to register
  it as soon as it compiles. **No time figure appears in any agent-facing text** (user, 2026-09-30; as the
  time-free Lean prompts since 2026-09-24).
- **Several decompositions of one node may coexist** (an OR): each has its own children, worked in parallel; the
  first decomposition whose children are all proved proves the node, the others are retired.

## 3. The tree

| Object | Content |
|---|---|
| node | a Lean statement; key = type hash (as today). Status: `open`, `proved`, `refuted`, `retired`. |
| decomposition (alternative) of node X | author; children (nodes); **glue** = the Lean text of X's declaration whose proof uses the children and compiles with the children as sorry; status: `active`, `proved`, `failed`. |
| root | the target theorem; exists from minute 0. |

A node is *proved* when (a) a verified library lemma states it (as today), or (b) one of its decompositions is
proved (§6). A node may have zero, one or several decompositions.

## 4. What an agent does

1. Reads the tree (every tool reply carries it, as the plan tree today) and registers on one node:
   `plan take X mode=split` or `plan take X mode=prove` (`mode` defaults to `prove`). One task at a time, as today.
   **These are the only two task states** (user, 2026-09-30): own tasks (`plan add … kind=own`) are removed — shared
   machinery is written as lemmas while proving some node (often the root) and reaches the team through the library.
   An agent on no node is *idle*: its `check` is refused and the idle rule reminds it.
2. **Split X**: in its Problem.lean (or a scratch file) it states the children as helper lemmas with `sorry` and
   writes X's proof from them, then runs `check`. If the file compiles and X's declaration (or, for the root, the
   target) depends on exactly these sorry helpers, the adapter registers a decomposition of X: children become
   nodes under X (existing nodes are reused when the statement matches), the glue text is stored. The splitter's
   registration on X ends; the reply suggests a child to take.
3. **Prove X**: proves X's statement; the verified lemma closes X (as today).
4. Takes the next node. The tree grows continuously; there is no "waiting for the map".

## 5. Rules

| Rule | Detail |
|---|---|
| splitter exclusivity | `plan take X mode=split` is refused while another agent holds X in split mode (the root included); the refusal names the holder and says: work on the problem directly meanwhile (`plan take <root> mode=prove`) — verified helpers enter the library and count once the split arrives. |
| split slot timeout | a split hold with no registered decomposition for a while (internal setting, not stated to agents) is released; the holder is told without a time figure and may take it again. |
| alternatives | a second decomposition of X is accepted when it differs (a different child set); the reply lists the existing decompositions of X with their progress. At most 3 active decompositions per node. |
| provers | `mode=prove` follows today's rules: free nodes first; joining a node that already has provers is the joiner's call (v30), with an approach ending in `first lemma: …`. |
| free list | open leaf nodes (nobody on them, no active decomposition) first, ordered by depth (shallow first) and age. |
| check gate | an agent registered on no node gets no compile (from minute 0). The reply says how to register: `plan take <root> mode=prove` is always possible. |
| evidence | not required to split. The prover of a child checks it first (small cases); a false child is refuted (§7). |

## 5b. Contributing in lemmas (user, 2026-09-30)

Only a stated, verified lemma reaches the team; a fact proved inside a long proof (`have …`) is invisible to it.
Three measures, none of them an interrupt:

| Measure | Detail |
|---|---|
| the instructions | ADAPTER.md: work in lemmas — every fact you need is its own named lemma; a verified lemma enters the library at your next `check`, others pull it, and a child of a split with the same statement closes at once; a `have` inside a long proof helps nobody else. |
| a hint in the `check` reply | when the checked file proves something through a long internal `have` block, or carries the whole argument inside one declaration, the reply ends with one line naming the step: "state <step> as its own lemma; once verified it enters the library". Threshold internal (lines per `have`); worded as a suggestion. |
| contributions made visible | the library summary in every reply credits lemmas to authors with their use: pulled by whom, which children they closed ("A3: 3 lemmas, 2 children closed, pulled by A1/A5"). Also the measure of whether early effort was used. |

## 6. Closing nodes: automatic assembly

When every child of an active decomposition D of X is proved, the adapter assembles X without any agent:
1. builds a file: the problem's original header and context, the library lemmas that proved the children (with their
   dependencies, as `pull` does), an alias `theorem <child name> : <statement> := <library lemma>` for each child
   whose library lemma has another name, then D's glue;
2. compiles it in the Lean jail (the same jail as `check`);
3. on success X is proved (lemma stored in the library under X's name, author = D's author, `assembled` = true),
   D is `proved`, X's other decompositions are retired (cascade, as today), and the step repeats at X's parent;
4. on failure X stays open, D is marked `failed` with the compile error, and D's author is told (next-reply line).

**Root.** When the root is assembled, the adapter writes the assembled proof into the Problem.lean of the root
decomposition's author (the same kind of write as `pull`); the judge grades files as today, so the run ends at that
file's save time. The judge is unchanged.

This replaces the assembly node, the assembly owner and the assembly rules.

## 7. Failures

| Event | Effect |
|---|---|
| a child of D is refuted | D fails; its other children are retired (cascade with holder notices, v30). If X has no other active decomposition, X is open again and appears at the top of the free list. |
| X refuted | as today: X and its subtree end; the decomposition containing X fails. |
| assembly compile fails | D failed (§6.4); its children stay proved (the lemmas remain in the library). |
| a node held long | as today: the parts view shows "stuck" after 20 min; anyone may split it (exclusivity permitting) or start an alternative. |

## 8. Notices (revised 2026-09-30: splits are frequent, so the default is the next reply)

In tree mode splits, closes and retirements happen many times per run; an interrupt per event would keep agents
restarting. Rule: **every notice goes first into the holder's next tool reply** (a pending line at the top, as v30);
an interrupt is sent only when the agent has made no team call for a long time (internal setting, no figure in any
agent-facing text) or has no task.

| Event | Holder / recipient | Delivery |
|---|---|---|
| **first decomposition of the root** | everyone except its author | **urgent interrupt** (user, 2026-09-30: forced) — as the map delivery today: no waiting for a quiet moment or the 5-min spacing; a running command is still let finish (at most 8 min) |
| any other new decomposition or alternative | everyone | the tree in the next reply |
| the node you hold is proved elsewhere (incl. closed by a library lemma) | its other holders | first line of the next reply; interrupt only if silent long |
| the node you hold is retired (its decomposition lost or failed) or refuted | its holders | first line of the next reply; interrupt only if silent long |
| your split hold was released | the splitter | next reply |
| your decomposition failed to assemble (with the error) | its author | next reply |
| your lemma was pulled / closed a child | its author | next reply (the contribution credit) |
| no task registered for a while | the agent | interrupt (with the first root split, the only interrupts the protocol itself sends) |
| the root is proved | everyone | the judge ends the run |

Delivery of the few interrupts uses the v30 gate unchanged (staleness, spacing, quiet moment, running command).
Per round we measure interrupts per run and how long a notice waited before the holder saw it.

## 8b. Scenarios

| # | Scenario | Rule | Effect on the others |
|---|---|---|---|
| 1 | X already has a decomposition D1 being worked on; B wants to split X differently | once X's split slot is free (it is, after D1 registered), B adds D2 as an alternative; the reply first shows D1's progress (children proved, holders) and B decides | none: D1's holders go on; the first decomposition fully proved wins, the other is retired |
| 2 | B thinks D1 is wrong | B refutes the false child with evidence (no "replace"); D1 fails; if X has no other active decomposition, X reopens at the top of the free list | D1's holders are told why (next reply / interrupt if silent long) |
| 3 | a child of D1 is too big | split that child (one splitter at a time) | none |
| 4 | D1 and D2 share a child (same statement) | one node with two parents; proving it serves both; it is retired only when no active decomposition needs it | none |
| 5 | X already has 3 active decompositions | a fourth is refused; the reply lists the three with their progress | — |
| 6 | two decompositions of X complete close together | the first successful assembly proves X; the other is retired; its proved children stay in the library | the losers' holders are told |
| 7 | someone proves X directly while decompositions of X are in progress | X is proved; all its decompositions retire (cascade) | their holders are told |
| 8 | a lemma proved by someone working directly matches a child | the child closes at once | its holders are told |
| 9 | a child's statement is close to a library lemma but not identical | no automatic close; the parts view says "a library lemma <name> has the same name / a close statement — pull it if it fits" (as today for same names) | — |
| 10 | a decomposition restates its parent (a child with the parent's statement, or a single child equivalent to it) or creates a cycle | refused, as a map restating the target is today; ancestor check as today | — |
| 11 | an agent holds X in prove mode and wants to split it | one task at a time: `plan drop X reason=switch`, then `plan take X mode=split` | — |
| 12 | two agents prove the same leaf | best-of-n as today; the first verified closes it | the other is told |
| 13 | the splitter's session ends or it goes silent while holding a split | the hold is released after a while (internal); a resume continues if the hold is still there | the next splitter may take X |
| 14 | the root is proved while others are mid-work | the judge ends the run as today | — |
| 15 | a refutation was wrong | no un-refute in this design (open question 5) | — |
| 16 | assembly fails at level k of a deep chain | D(k) fails; X(k) stays open; levels below keep their proofs | D(k)'s author is told; the free list shows X(k) |

## 9. What is removed

Phases 1/2, the map as a special object and its delivery rules (two parts, restated target, `not_before`), route
nodes (`say plan` no longer creates `route_Ax`; a plan message stays a message), own tasks (`kind=own`), the phase-1 push, the assembly node
and owner, the "lump map" split request. ADAPTER.md is rewritten around the tree; target length ≤ 6k characters
(today 12.9k), which is also the C1 cost hypothesis.

## 10. Measurement

Per round, four problems (800-11, 1000-22, 1000-7, 1000-12): solves x/4, time sum, cost, output tokens, API speed;
plus time to the first root decomposition, lemmas verified before it and how many of them were later used (pulled or closing a child), share of the run before it (today median 30%), decompositions per node,
alternatives that won/failed, automatic assemblies (success/failure), interrupts per run. Compared in an adjacent
pair with the better of r35 (v30) / r36 (v20s), and with shared notes s2 ($13.29 / 6.39M / 16470 s).

## 11. Implementation plan

| Step | Content |
|---|---|
| 1 | Data model: decompositions per node (children, glue, author, status); split holders; migrate `maps` readers (viz, round_summary, early_lean, team_contact) to "first root decomposition". |
| 2 | `check`: register a decomposition when a node's declaration (or the target) compiles from sorry helpers; reuse today's split detection and map detection code. |
| 3 | `plan take … mode=split|prove`, split exclusivity and timeout; free list by depth. |
| 4 | Automatic assembly (§6) in the service's reactions (it needs a compile; it runs in the reaction thread pool like the host-side ingest), root write-back. |
| 5 | Failure handling (§7) on top of the v30 cascade. |
| 6 | New ADAPTER.md (≤ 6k chars) and tool descriptions; every agent-facing text recorded in LOG.md. |
| 7 | Tests: synthetic tree scenarios (split, alternative, exclusivity, auto-assembly success/failure, refute → reopen); replay of r33/r34 files (their maps become root decompositions); a 2-agent pilot. |

## 12. Open questions

1. Root write-back: writing the assembled proof into an agent's Problem.lean while it may be editing it. Alternative:
   the judge also grades a team file `library/Assembled.lean` (changes the judge; not chosen by default).
2. Alias names: a child proved under a different name and a slightly different but definitionally equal statement —
   assembly then fails and the child must be reproved under the node's statement; frequency unknown.
3. Depth: no limit proposed; deep trees cost assembly compiles (one per closed node).
4. Base version: built on whichever of v30 / v20s wins the r35/r36 pair.
5. Disputed refutations: today a refutation stands; a `dispute` action (evidence against the refutation) may be needed if wrong refutations show up.
