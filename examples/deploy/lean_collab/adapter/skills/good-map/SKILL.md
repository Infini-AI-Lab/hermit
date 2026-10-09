---
name: good-map
description: What makes a good map of a Lean proof (the target proved from helper lemmas stated with sorry) — read it before you write or judge a map.
---

# A good map

A map is what the whole team inherits: once it is registered, five agents spend
their time on its parts. A weak map costs more than a late one. A split of a part into
sub-parts is a map of that part, and the same rules apply, with the part in place
of the target.

**Choose the route by what its parts will cost.** When there are several routes, count
the unproven pieces each one needs and how hard each piece is — a route whose parts
need only crude estimates beats one that needs sharp ones — then commit; do not keep
reopening the choice. Prefer a route the library already supports.

**Every part is smaller than the target.** A part that is the target in other
words — the same claim with a definition unfolded, a constant rewritten, or a
hypothesis moved — splits nothing, even if the map has several parts. A map whose one
part holds the whole difficulty is not a map yet. Ask of each part: is it strictly
easier than the target, and why?

**Split until each part has a proof sketch.** For every part, ask: can I say in a
few sentences how it is proved? If not, it is still too coarse: prove it from
smaller `sorry` lemmas, which become its children — the map gets another level
there. If yes, stop: those sentences go in its docstring as the evidence. If the
sketch has cases, the cases are the parts. Splitting lowers the difficulty; a part
split into pieces each as hard as the part lowers nothing.

**The hard part is named, not hidden.** Know where the difficulty of the proof
lies and give it its own part (or parts). A map made of one small simplification
plus "everything else" is not a decomposition. Whoever writes the map starts on its
hardest part at once, in its simplest statement.

**Every part is believed true, with evidence.** For each part, have a reason it
holds: a numerical check over a range, a known theorem, a short argument. Put the
reason in the part's docstring. Check the edge cases (zero, small values, the
boundary of a range) — a false part sends the team down a dead end. The evidence
is a numerical check, a known theorem or a short argument — not a Lean proof.

**Statements are exact.** The hypotheses each part needs are stated; types and
casts are the ones the proof will use; the part says precisely what the glue
uses. Settle the definitions before you hand out the parts: a definition that changes
later invalidates the work done on it.

**The parts fit together simply.** The target's proof from the parts (the glue)
should be short and obviously right. If the glue itself needs a long argument,
that argument is a missing part.

**Each part is a unit one agent can own.** Not chopped into dozens of trivial
steps. Machinery several parts share (a general lemma, a definition with its basic
facts) is its own part, stated in general form, with the specific object replaced by
a variable.

**It uses what the team has.** Library lemmas that state a step close it at once;
use them as parts where they fit. Build on the findings on the message board, and
say whose idea the map follows.
