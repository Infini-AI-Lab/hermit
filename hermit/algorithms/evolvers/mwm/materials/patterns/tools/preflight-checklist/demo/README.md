# Demo: transplant preflight

`python3 test.py` — four facts files run against the transplant
checklist (bed prepared, hardening-off done, pest check passed): one
satisfies everything and gets a GO naming what was checked; one is
missing two items and gets a CATCH listing both; one names an action
with no checklist on file and is refused; one is broken JSON and is
treated as NO-GO.

Form (keep when re-domaining): the single gate call immediately before
an irreversible action, one machine-parseable line out, refusal of
unknown action types, and every failure mode — including the gate's own
input failing to parse — resolving toward NO-GO.

Content (replace): the action names, the checklist items, and the fact
keys — derive them from the irreversible actions your evidence shows
misfiring.
