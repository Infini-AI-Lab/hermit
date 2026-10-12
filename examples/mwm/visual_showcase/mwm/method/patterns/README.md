# Pattern Library

A catalog of EXECUTABLE mechanism *forms* — programs and delegates — the
meta-agent may consult when designing its world model. Cards describe
shapes of solutions, never solutions.

The `demo/` folders that the cards and the rules below mention are omitted from this copy: this learner cannot run
code, so it reads the cards only. Each card's `## Demo` section still describes its demo.

## Rules

1. **Form only.** A card teaches a structure: skeleton, an out-of-domain
   worked demo, abstract applicability conditions. No card may contain the
   evaluation domain's entities, rules, product names, or numeric values.
   `python3 -m mwm.patterns_lint` greps every file against a
   banned-vocabulary list; one hit is a violation.
2. **Provenance required.** Every form comes from antecedents OUTSIDE our
   benchmarks: published practice or another domain's experience. A card
   supported only by experience from the domain under evaluation must not
   ship in that domain's library. The provenance record is kept by the
   operators (claude-doc/patterns-provenance.md), not on the cards.
3. **Demos run.** Each `demo/` is self-contained and offline:
   `python3 demo/test.py` must pass with the bundled sample data.
4. **Adoption is free.** Copying a demo's skeleton and swapping in
   domain content learned from evidence is the intended use, stated
   plainly in the contract. What remains the meta-agent's own work:
   choosing which card fits the observed failures, filling it with
   evidence-derived content, and revising it when the counters say it
   is not working.

## Card layout

```
<card-name>/
  CARD.md      front matter (name / when / uses) + five sections
  demo/
    run.py         reference implementation, neutral domain
    sample_data/   bundled inputs; no network, no external state
    test.py        asserts the mechanism's key behavior
    README.md      what the demo shows; what to change when re-domaining
```

The library holds `tools/` and `subagents/` only. Knowledge — briefing
text, claims, scope and wording — is the meta-agent's own prose, written
from its own evidence; this library is for mechanisms it must build and
test.

`patterns_catalog.py` prints a one-line-per-card catalog from the `when:`
lines. Nothing embeds it today. Cards are frozen per experiment tag;
changes bump the library version.
