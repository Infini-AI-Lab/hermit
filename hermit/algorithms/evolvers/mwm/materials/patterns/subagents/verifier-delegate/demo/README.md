# Demo: verifying a bench assignment

`python3 test.py` — before a seedling tray is committed to a bench, a
delegate re-derives the assignment from the attached seedling record and
zone rules only. Three cases: a sound proposal is confirmed; a tropical
tray proposed into a temperate-zone bench draws two agreeing `differ`
verdicts that outrank the proposer (CATCH, with the derived bench); a
record missing the deciding field makes the stub ramble, and the parser's
defined fallback treats it as cannot-tell — which never confirms, so the
step stays unverified on the cautious branch.

The delegate is a deterministic stub (`fake_delegate`) — the demo runs
fully offline. The form is the protocol around the call, not the model.

Form (keep when re-domaining): raw-materials-only request assembly, the
one-line verdict grammar, the parser whose fallback for anything
unparseable is cannot-tell, and the two-vote rule that lets independent
agreement overrule a single proposer.

Content (replace): the records, the rules text, the proposal shape, and
what your domain's cautious branch does with an unverified step.
