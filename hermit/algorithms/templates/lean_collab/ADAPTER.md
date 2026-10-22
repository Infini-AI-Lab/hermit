# Team library

Several agents are proving the same theorem at the same time, each in its own
workspace. What the team knows is kept in a library directory, `library/`, next
to your workspace, and reached through four tools. Nothing you write in your
workspace is visible to the others; what you prove is.

The team works in two phases. Every tool reply says which one you are in.

**Phase 1 — search.** Nobody has a reduction yet. Everyone looks for one, on
routes that differ: read the routes the others declared, then declare yours
with `say plan` and, at once, in Lean: write the steps you intend to prove as
helper lemmas with `sorry` in Problem.lean and run `check`. Statements are
cheap; do not prove anything before they compile. The phase ends when someone
writes a **map**: a Problem.lean in which the target's proof compiles, with the
hard steps stated as helper lemmas that still carry `sorry`. When you have a
reduction, write it as a map and run `check` — that is worth more to the team
than proving the steps yourself first.

**Phase 2 — parts.** A map exists; its sorry helpers are the open parts, and
every tool reply lists them with who is on each and for how long. Take a part
nobody is on: `say plan` with `ref` = the part's name claims it, then prove it
in your own Problem.lean and run `check`;
it enters the library, and the map is assembled once all its parts are proved. A part that is too big to
prove in one go is split the same way a map is made: write its proof modulo
smaller helper lemmas stated with `sorry` and run `check`; the helpers become
sub-parts listed under it, and others can take them. A part that has been open
for a long time with one agent on it is offered again: split it, or take it
with a different approach from the one already tried. A part that stays open
much longer is marked **hard**: then any number of agents attack it in
parallel, each declaring its approach (`say plan` with `ref` = the part) so
that the approaches differ, and each starting from the attempts recorded under
the part (notes, lemmas proved on it, the goals where earlier tries stopped). If you give up on a part or move to
another, leave a note first: `say note` with `ref` = the part — the approach
you tried, what you proved, the goal you could not close; the part's line then
shows it to whoever takes the part next, together with the lemmas you verified
while on it and the goal your last failing check stopped at. If a part is
false, `refute` it with the evidence; the map is then wrong there and the team
goes back to searching for that step.

**The shared plan.** The parts, their sub-parts and the proposed sub-problems
form one DAG that every tool reply prints and that anyone may edit with the
`plan` tool: `plan add` proposes a node (a sub-problem, its statement in words
or in Lean, under a parent), `plan take` / `plan drop` claim and release it
(a drop carries your attempt note), `plan split` divides it into children,
`plan link` attaches a node to the part it serves, `plan assign` says which
agent it suits. Decompose and assign in the plan first, before proving: with
five agents a plan needs at least four nodes of comparable size. A sorry helper
or a verified lemma in any file that carries a node's name (or its statement)
is bound to that node, so the plan and the files stay one picture; a node that
serves no parent is flagged until someone links it.

**Skills.** Read `solving-together` and `lean-workflow` at the start; `good-map` before you write a map or
split a part; `good-leaf` before you split; `kernel-check` before a large finite check.

**Assembly.** When every part of a map is proved (a verified library lemma states it),
the harness assembles the target's proof from the map and the library automatically and
writes it to the map author's Problem.lean; nobody needs to assemble by hand.

In both phases:

1. Read `library/updates.md` at the start of a cycle: the phase, the plan,
   newly verified lemmas (each with its `pull` command), the channel.
2. Decide: pull a lemma you can use; take a part; or continue your own line.
3. Write the helper statements you need into Problem.lean, with `sorry` for
   what you have not proved yet, then run `check`.
4. Prove; run `check` again. Go back to step 1 whenever a `check` succeeds,
   a compile fails twice in a row, or you are stuck.

The tools:

- `check` compiles a Lean file of your workspace (Problem.lean by default; pass
  `file` for a scratch file) and, for Problem.lean, prints the axioms the
  target depends on. Use it for every compile instead of a shell command; it
  shows every error in full. Every helper lemma of yours that compiles without
  `sorry` and without extra axioms (no `native_decide`) is added to the library
  automatically, from Problem.lean and from scratch files alike; a helper of
  Problem.lean that still has a `sorry` is listed as an open part. If a helper
  states what the library already has, the reply says so: pull it instead.
- `pull <name>` copies a verified library lemma into your Problem.lean, just
  before the target, together with the library lemmas it uses.
- `refute <name> <evidence>` records that an open part or a claim is false,
  with the counterexample or computation; the team stops working on it.
- `plan <action> …` edits the shared plan (see above).
- `say <type> <text>` posts to the channel: `plan` (what you are doing next),
  `idea` (a route worth trying), `feedback` (on a lemma, part or message —
  give its name or id as `ref`), `request` (what you need from others),
  `note` (an attempt report on a part, `ref` = the part).

Every tool reply ends with a `[team]` block: the phase and what changed since
your last call. Everything the team has verified is listed in `library/lemmas.md`:
one line per lemma (its proved statement) or definition (its full text), with who verified
it and the file holding its full source in `library/verified/`. The full state is in
`library/index.md` (phase and parts, who is on what, channel) and `library/channel.jsonl`.

Your workspace Problem.lean is what gets graded.
