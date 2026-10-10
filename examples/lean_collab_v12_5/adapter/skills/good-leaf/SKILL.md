---
name: good-leaf
description: When a node of the map is small enough to be a leaf, and how to state it so that it can be proved — read it before you split a node.
---

# A good leaf

A leaf is a part one agent proves without splitting further.

**Small enough:** you can see the proof — one idea, the rest is routine. If the
leaf still holds the parent's key idea, it is not a leaf yet: split it.

**Provable as written:** the statement is true at its edges (zero, small values,
every parameter's range) and is exactly what the parent's glue uses. Give it every
hypothesis the glue can supply: most false parts are missing one the parent already
had. Check it numerically — on larger and random cases too, not only the first few —
before you hand it out; a false leaf costs the most.

**If a leaf you are given is false,** post the counterexample together with a corrected
statement, so the team keeps moving.

**Not over-split:** don't cut one argument into copies for different cases or
constants, or into pieces whose glue is as long as they are.
