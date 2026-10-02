# Async collab — phase 1 as a shared effort: build the map together (draft, 2026-10-01)

Status: design draft for review. Phase 2 (after the map) is out of scope here and is discussed separately.

## 1. Why

Tree mode (v31–v45) reached its behavioural goals (root split in 3–14 min, dispatch, deep trees) without a clear
gain in solve time over the shared-notes baseline (s2) or the map version v20, and the harness grew complex
(interplay bugs, assembler bugs). The user stopped that direction (10-01 09:55).

Two readings of the recorded runs (10-01) set the problem for phase 1:

- Ideas are not missing. The decomposition the map used existed 13–32 min before the map was registered, and in
  6 of 8 runs it had been posted as prose. What delayed the map: proving before stating (4 runs), glue compile
  errors (4), waiting for someone else to assemble (2).
- Agents duplicate rather than converge. In every map-version run 3–4 agents followed nearly the same route
  independently and proved the same lemmas 2–4 times; agents said "we're duplicating" and kept going.
- Shared ideas do change course when they are posted (≈16 observed course changes across s2, r20, r43; e.g. r20
  1000-7: A3 at 23.3 "A4's particle route is the way", which became the proof's core). Dead ends are rarely
  posted but are valuable when they are (r43 800-11: the `native_decide` warning).
- Messages are few: 3–11 per problem over two hours, mostly route declarations (`say plan`). Delivery is not the
  bottleneck — a posted message reaches the others in their next tool replies.
- Lemmas verified before the map are used heavily: 15–21 of them in the final proofs of 5 runs.

## 2. Decisions (user, 10-01)

1. Phase 1 is a common effort: the five agents build one map together. They converge — build on the best idea —
   rather than search on deliberately different routes (v20–v30 asked for "routes that differ"). Best-of-n
   belongs to phase 2 (several agents may prove the same part).
2. Agents are encouraged to post what they find (ideas, constructions, numerical patterns, dead ends), not
   what they intend to do. One message kind, no types.
3. The common goal is stated at the top of the first task message (TEAM.md), not only in ADAPTER.md.
4. No pushing: no assigned splitter, no reminder, no hand-off, no judge, no idle interrupt in phase 1. As before,
   phase 1 ends when the first map compiles.
5. Every round records: messages per problem and their kind (finding / plan / other), adoptions of another agent's
   idea, time to the first map, duplicate lemmas.

## 3. Mechanism (phase 1)

Unchanged infrastructure: the standalone adapter service (v30, kept permanently), the lemma library (`check`
verifies and registers lemmas, `pull` brings them into a file), the automatic assembler (v44/v45 fixes).

| element | phase 1 behaviour |
|---|---|
| message board | `say(text)` — one kind of message, at most 5000 characters, no type, no rate limit beyond a loose flood guard (20 per 10 min per agent). Stored in `library/board.md` (readable by all) and `state.json`. New messages appear in every agent's next tool reply, in full (as now). |
| lemma library | as now: any lemma that compiles without `sorry` and with clean axioms enters at the author's next `check`; replies announce new ones; `pull <name>` brings it in with its dependencies. |
| the map | a Problem.lean (any agent's) in which the target compiles from two or more helper lemmas still carrying `sorry`. The first `check` of such a file registers the map: its sorry helpers become the parts. A one-part "map" is not registered (it is the target restated, not a decomposition); the reply says so. |
| end of phase 1 | the map's registration. Everyone is interrupted once with the map (author, parts, statements) — the one interrupt of phase 1. |
| nodes during phase 1 | none. No `plan take`, no splitter, no assigned roles; `check` works for everyone at any time. |
| removed from tree mode | root node from minute 0, assigned splitter (A1), A2–A5 on the root, take-first gate, splitter reminder and hand-off, crowd judge, idle interrupt in phase 1, message types and their side effects (`plan` registering routes, `note`, `request`). |

## 4. What the agents read (drafts — for review)

### 4a. TEAM.md — the team paragraph of the first task message

> Team: you are agent {me}, one of five agents ({others} are the others) proving this theorem together.
>
> First, the five of you build one **map** of the proof together: a Problem.lean in which the target follows
> from two or more helper lemmas still stated with `sorry`. Work on it as a team. Post what you find on the
> message board (`say`) — a key idea, a construction, a numerical pattern, a route that fails and why — and read
> what the others post; when someone's idea is better than yours, build on it instead of starting your own.
> Lemmas you prove along the way go into the shared library (`check`), where everyone can use them.
>
> When anyone's map compiles, it is registered for the team and you are all told; the map's parts are then
> proved. The tools are described in the project instructions (AGENTS.md); the library directory is {lib}. As
> soon as any workspace file proves the theorem, every agent is stopped.
> No proof of this theorem exists anywhere on this machine; do not search for one.

### 4b. ADAPTER.md — phase 1 part (the full file is rewritten around it)

> # Team proof
>
> Five agents prove the same theorem, each in its own workspace. Nothing in your workspace is visible to the
> others; what you post and what you verify is.
>
> ## Building the map together
>
> The team's first goal is one map: a Problem.lean in which the target compiles from two or more helper lemmas
> stated with `sorry`. Nobody is assigned to write it; whoever has it first submits it with `check`.
>
> Share as you go. The message board is how the five of you think together:
> - post what you find, in a few lines: an idea that works, a construction, a numerical pattern, a step that
>   fails and why, a Mathlib lemma that fits;
> - read the new posts in each tool reply (the whole board is `library/board.md`);
> - when another agent's idea is better than yours, build on it and say so; when you see two of you doing the same
>   thing, say so and split the work.
>
> Statements are cheap: when you see the steps, write them as helper lemmas with `sorry` and `check`. Any
> lemma you prove along the way enters the library; others `pull` it instead of proving it again.
>
> When a map compiles, it is registered and everyone is told; phase 1 is over.

### 4c. `say` tool description

> Post on the team's message board. Everyone sees new posts in their next tool reply; the whole board is
> `library/board.md`. Post what you found — an idea, a construction, a numerical pattern, a step that fails and
> why — in a few lines. At most 5000 characters.

### 4d. Interrupt when the map is registered

> {author} has a map: the target now follows from {n} parts — {name}: {statement}; … Phase 1 is over.
> {phase-2 instruction — to be decided with phase 2}

### 4e. Reply to a one-part file

> not registered as a map: the target follows from one part ({name}), which only restates it. A map has two
> or more parts.

## 5. Measurement (every round)

Per problem: messages posted (and how many are findings vs plans vs other, classified by a reading), adoptions
(an agent's reasoning or post explicitly builds on another agent's post), time to the first map, lemmas proved
twice or more under different names, and the usual solve time / cost / tokens on the four reported problems.

## 6. Open points

- Phase 2 (after the map): to be designed separately (user: best-of-n allowed on a part).
- Whether the board needs anything beyond pushing new posts in tool replies (e.g. delivery to an agent that has
  made no tool call for a long time) — measure first.
- Risk: agents may wait for someone else to write the map once one agent announces it (observed twice in the
  readings); measure, do not pre-empt.
- **Pending for the phase-2 discussion (user, 10-01 ~11:45):** "own tasks" (v21–v30: an agent registers its own work item, `plan add kind=own`, mostly prose, mostly unattached to the map; r35 had 31, 28 retired unused) — whether phase 2 should have them at all, and how such per-agent work is shown on the activity page (as nodes, as notes on the agent's lane, or as notes on a part). Routes are already shown as approaches on the target rather than as boxes (page v45); approaches recorded on nodes appear in the node's tooltip.
- **Pending for phase 2 (user, 10-01 ~11:55): if own tasks stay, every own task must name the parent node it contributes to** — which node it helps prove is the important part. (Display now: an own task with no parent is drawn as a separate tree beside the target's — the page shows a forest.)
- **Pending for phase 2 (user, 10-01 ~12:10): graph edits.** The graph holds only the root conclusion and the conclusions of Lean-checked maps/splits. A statement proposed for a node (formerly "plan step", now called a **proposed lemma**) is a note on that node — an unverified proposal to change the graph; the graph changes only through a checked split or revision. Whether and how agents edit the graph in phase 2 (e.g. revising any node's split) is to be decided.
- **Pending for phase 2 (user, 10-01 ~12:20): encourage work on leaf nodes.** Agents do not always work on leaves: in the map versions a large share of agent-time sat on nodes that already had children (r20: 33%; r35: map parts with children 8%, own tasks with children 16%, whole-target own tasks 8%); in tree mode, after forced moves and dispatch, 1–3%. The user wants agents encouraged to work on leaves so that the collaboration is meaningful (direct work on a parent duplicates the children's work); how to encourage it without hard rules is to be designed with phase 2.
- **Decided (user, 10-01 ~13:50): phase 1 still ends at the first compiled map.** Improving or replacing a weak first map happens in phase 2 through graph edits (see the graph-edit item above), not by delaying the end of phase 1.

## 7. Phase 2 — graph edits (agreed in discussion, user 10-01 ~15:05; not built)

Principle: the graph holds only Lean-checked conclusions; no statement is edited in place and no node is deleted.

Agent operations (separate tool calls, because each is handled differently):
1. `split X` — X has no split yet; a file in which X compiles from two or more sorry lemmas registers them as X's children.
   Agents proving X directly are told in their next tool reply (no interrupt).
2. `alt_split X` — X already has a split; a different set of children is added as a parallel split (OR: X is proved when
   any one split is fully proved). Children with a statement identical to an existing node are that node (progress
   carries over). Correcting a child = `alt_split` of its parent with the corrected statement; dropping an unnecessary
   child = `alt_split` without it. Agents on the old split are told in their next reply, with both splits' progress.
3. `refute X` — a file in which `¬X` compiles without sorry. X is marked false; every split containing X is void and
   its subtree retired. Agents in that subtree are interrupted at once, with the counterexample and the open leaves.

Automatic changes:
4. A node proved (check, no sorry) is closed; a split whose children are all proved closes its parent (assembly).
5. A library lemma with a node's exact statement closes the node.
6. Retirement: a branch is retired when its parent is proved by another split, or when it is dominated — a new split's
   open children are a subset of an old split's open children (the old split needs at least as much work). Nodes shared
   with a live split stay live. Agents on retired nodes are interrupted at once with the reason and the open leaves.

Common rules: retired nodes stay on the activity page (grey); proved lemmas stay in the library; every graph change is
posted on the board automatically; `check` only compiles and fills the library — it no longer registers splits.

Open: (a) phase 1's map as `split root` (one tool for both phases; would change phase 1 and need a re-run);
(b) a manual `abandon` (leaning no); (c) `split` and `alt_split` as one tool or two (written as two).

## 8. Phase 2 — who works on what (texts approved by the user 10-01 ~15:30; not built)

Evidence (LOG.md): inference without claims → everyone on one part (early map versions); free choice with claims →
crowding (r13–r19); "free parts first" hard refusal (v20, H11) → 3 of 7 refusals turned into a free take; best-of-n
second attempts (H14/H15) → arrived 17–20 min late, mostly restated the leader, 1–2 of 6 contributed; assignment and
forced moves (tree mode) → leaf share up, no time gain.

Decisions: explicit `take`; hard refusal of a held node while any leaf is free; second attempts only when every leaf is
held; no assignment, no forced moves; work on leaves is encouraged, and taking a node that has children needs a reason
(posted on the board); the 10-min idle push stays.

Approved texts:
- ADAPTER.md (phase 2): "The map is now a graph: the target at the root; a node that has been split is proved by its
  children. / Before you work on a node, `take` it. While any leaf has nobody on it, a node someone already holds
  cannot be taken; the reply lists the free leaves. When every leaf has someone, you may take a held node as a second
  attempt. / Work on leaves. A node with children is proved once its children are. If a leaf is still too big, `split`
  it instead of proving it whole. To take a node that has children, give the reason."
- `take` description: "Take a node of the map: you are working on it. The reply shows who is on each node and which
  leaves are free. While any leaf is free, a node someone holds is refused. A node that has children needs `reason`:
  why you work on it rather than on its children."
- Refusal (held node): "not taken: {X} already has {holders} on it, and these leaves have nobody: {free leaves}. Take
  one of those."
- Non-leaf without reason: "not taken: {X} has children ({children}); it is proved once they are. To take {X} itself,
  give `reason`. Free children: {free children}."
- Non-leaf with reason: "taken: {X}. Your reason is posted on the board."

## 9. Phase 2 — other actions (user 10-01 ~15:40; not built)

1. Board: unchanged — every post goes to everyone (no node-attached posts; no flooding seen: 1–6 posts per problem in
   r48 phase 1, 3–11 per two-hour run in tree mode). Revisit only if phase 2 shows flooding.
2. Delegation: no separate action — changing the graph (a split's free leaves) is the delegation.
3. Graph view: ordinary replies carry only the changes relevant to the agent (its node, that node's parent and
   children, the free-leaf list); the whole graph is shown when the agent takes a new task (`take` reply); the full
   graph is also in `library/graph.md`.
4c. `split` and `alt_split` stay two tools.
4a. Phase 1 keeps the automatic map registration on `check` (no `split root`) — user 10-01 ~15:45.
4b. `abandon X` is added (user 10-01 ~15:55): reason required and posted on the board; no restriction when others hold
    X — agents on X and its subtree are interrupted with the reason. Evidence: map versions r21–r35 had manual
    retirement; 30 uses, almost all "already covered by a verified lemma" (subsumed / duplicate), none "dead end";
    3 had other claimers, all justified by a verified lemma — no wrong kill seen (reasons read, trajectories not checked).
    Approved texts (10-01 ~16:00): description "Retire a node of the map that the proof no longer needs — for example,
    its content is already covered by a verified lemma. Give `reason`; it is posted on the board, and everyone working
    on the node or below it is told at once."; ADAPTER.md "If a node is no longer needed, `abandon` it with the
    reason."; interrupt "{by} abandoned {X}: \"{reason}\". You were on {node}; it is retired. Free leaves: {free
    leaves}."; refusal "not abandoned: give `reason`."

## 10. Phase 2 — transition and assembly (user 10-01 ~16:20; not built)

- Phase 1 → 2: at the map's registration every agent is interrupted with the whole graph and the explicit start of
  phase 2 (user). Drafted texts (phase-2 start interrupt, ADAPTER.md phase 2, split / alt_split / refute texts, node
  proved / parent proved / idle notices, good-map extension, TASK.md last line) — approved as drafted (user 10-01 ~16:25: "按你写的来").
- good-map applies to every split in phase 2 (user: yes).
- Assembly: the harness assembles automatically (v31–v45 `assemble`); with parallel splits it assembles whichever split
  completes first. On an assembly failure the split's author is put on the node to fix it, nobody else is told
  (user: option A — the one exception to "no assignment"; v45 evidence: five agents fixing one assembly cost 21% of a
  run). Failure text: draft ("The assembly of {X} … failed: {why}. You wrote this split, so you are on {X} to fix
  it. If a piece is missing, verify it as a named lemma (the assembly is retried whenever the library grows); otherwise
  prove {X} directly in your file from the library lemmas — the `check` that compiles it closes {X}.") — user left the
  wording to Claude; kept as drafted.
- Evaluation: phase 1 + phase 2 total time (user).
- Activity page: Claude decides (parallel splits, grey retired nodes, refuted marks).

## 11. Phase 2 — the `good-leaf` skill (text approved by the user 10-01 ~16:05; not built)

Evidence: leaf analysis r20–r44 (subagent, 652 leaf-rounds): close rate 81%; ≥150-character statements ≈55%; all 18
refuted leaves were edge-case or quantifier-strength errors; the slowest leaves still held the parent's key idea
(e.g. one direction of the target, 143 agent-min unproved); over-splitting rare (1.4% one-line leaves), assembly
failures were interface errors. User: the first draft was too detailed — three principles only.

Approved text (deployed with phase 2, read before `split`):

    # A good leaf
    A leaf is a part one agent proves without splitting further.

    **Small enough:** you can see the proof — one idea, the rest is routine. If the leaf still holds the parent's key
    idea, it is not a leaf yet: split it.

    **Provable as written:** the statement is true at its edges (zero, small values, every parameter's range) and is
    exactly what the parent's glue uses. Check it numerically before you hand it out; a false leaf costs the most.

    **Not over-split:** don't cut one argument into copies for different cases or constants, or into pieces whose
    glue is as long as they are.
