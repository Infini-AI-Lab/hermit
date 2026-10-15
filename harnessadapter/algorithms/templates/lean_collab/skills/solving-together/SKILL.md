---
name: solving-together
description: How to work with the other agents on the same theorem — read it at the start.
---

# Solving together

Several of you work on the same problem; the channel (`say`) is how you think together.
What the team shares best is checked work: verified lemmas others can pull, not prose.

**Pull, don't re-prove.** You do not need to build the whole proof yourself: pull your
teammates' verified lemmas instead of proving them again, and spend your effort on a
piece nobody has.

**Post a finding the moment it checks.** A key fact or construction that survives a
numerical check goes to the channel at once, before you formalize it; its definitions
go into the library as compiled lemmas others can pull.

**Look before you build.** Read the new posts and the library before you begin a step
or define anything: if the concept or the lemma is already there, build on it. When a
teammate's verified lemma or construction covers your plan, adopt it, name what it
still lacks, build that piece, and drop your own copy. Before you write a long block,
search the library for it.

**Read the whole library.** `library/lemmas.md` lists everything the team has verified,
one line each. It is long, keeps growing, and a long output gets cut: read all of it, in
several pieces until you reach the end. The lemma you are about to write may already be
there, under a name you would not guess; searching for a name finds only the names you
guess, so read the list itself.

**Say what you are about to do.** Before a long computation or a big piece of Lean,
post it in one line — what, and roughly how long. If it is already being done, do
something else.

**Join the plan.** Once a map is registered, work on its parts, or split the largest
held part, rather than on a whole proof of your own (a part marked hard welcomes a
parallel attempt with a different approach).

**Take one part, then state it.** Hold one part at a time. When you take it, state it as
a `sorry` lemma in your Problem.lean at once — the team sees who is on what from the
files — and develop its proof in a scratch file; move the proof over once it compiles.

**Keep in touch while you work.** During a long computation or a long stretch of
thought, look at what changed in the team now and then (any team tool reply shows it);
after your context is summarized, reread the posts addressed to you.

**Share what you learn, especially failures.** A route that fails, and why, saves the
others the same hour. One agent re-checks a teammate's construction; the others build
on it.

**Close the last gap together.** When one step remains, name it exactly and leave it
to the agent holding the closest lemmas; the others pull the result or keep working on
open parts rather than repeating the same step.

**Hand over what you finish.** When a lemma lands, post its name and what it gives; pull
others' lemmas instead of proving them again.
