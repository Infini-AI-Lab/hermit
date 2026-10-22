---
name: kernel-check
description: How to write a large finite check that Lean's kernel can verify with `decide` (no native_decide) — read it before you write one.
---

# A finite check the kernel can do

`decide` costs one kernel step per case. Count the cases before you write the
check, and cut them with mathematics first.

**Verify, don't search.** Whatever is expensive to find, find it with a script and
write it into the statement; Lean then only checks it.

**Write it so the kernel can run it:** a `Bool` function by structural recursion,
with a lemma proved by induction that `true` means what you need; keep every
intermediate value small. Functions defined by well-founded recursion do not reduce
in the kernel.

**Measure small first.** Run the check on a tiny instance, then time it on a small
range before the full one; before you raise a heartbeat limit, time the check on a tenth
of the range. Split a big check into named lemmas. Never let one check
block you for long without knowing why it is slow.
