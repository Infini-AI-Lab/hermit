---
name: lean-workflow
description: How to work in Lean so that progress is steady and checked — read it at the start.
---

# Working in Lean

Progress is what compiles. Runs that prove their theorem compile often, from early on;
runs that fail spend long stretches thinking or computing without compiling anything.

**Have a route, then write Lean early.** State the route in a sentence or two, with its
key reduction. If an informal proof or a library theorem fits, formalize that route
rather than looking for a slicker one. When the easy argument gives less than the
statement, name the exact gap and look for the known theorem that closes it.

**Keep a skeleton that compiles.** The target proved from helper lemmas stated with
`sorry`; fill the helpers one at a time. Never leave your main file broken: develop in a
scratch file with the same header, compile after each lemma, and move over only what
compiles. Edit by an anchor that occurs once, and recompile after every edit.

**Look names up by compiling them.** A few `#check` lines in a scratch file confirm a
name, its argument order and its implicit arguments faster than reading search hits.

**Read the error where it happened.** Print the numbered lines around it and read the
goal the error shows; fix that spot rather than rewriting the proof.

**Keep compiles short.** If a compile is slow or runs out of heartbeats, time its pieces
to find the slow step and restructure it; raising the limit and recompiling everything
rarely helps.

**Numbers test ideas; they are not the plan.** Test a conjecture on larger and random
cases, not only the first few, then go back to Lean. Run a long search in the
background on small sizes and keep writing Lean meanwhile.

**Only clean proofs count.** Check the axioms your target depends on; a lemma that
still has `sorry` — yours, a teammate's, or one in the file — cannot be used.

**Keep going, and keep your footing.** Do not stop to write status reports, and do not
delete scratch work. Write settled facts down (a notes file, or as compiled lemmas):
a long context gets summarized, and summaries drop or distort them.
