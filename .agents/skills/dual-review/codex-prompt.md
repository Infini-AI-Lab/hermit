You are reviewing a pull request in this checkout. Read-only: do not modify, create or delete any file.

Repository and PR: {{REPO}} #{{PR}} (head {{HEAD}}, base {{BASE}}).

Scope: `{{DIFF_COMMAND}}`. Read every changed file in full, and the code the changes call or are called by, before
judging anything.

What the change intends (the PR author's goal, as the orchestrating reviewer understands it):
{{INTENT}}

Already settled in earlier rounds (do not report these again unless the current code shows the fix is wrong or
incomplete):
{{SETTLED}}

The goal is to decide whether this PR is ready for merge. Report every problem that should block the merge; there
is no limit on the number, and a short list is only right if the code really has few problems. Do not stop after
the first few findings: work through every changed file, hunk by hunk, before writing the output. Anything you
leave out now costs another full review round later.

Minor points are not worth your effort: something only a contrived input reaches (a secret in a file name, non-UTF-8
names, IPv6-literal hosts, a malformed value no real producer emits), something that only changes a log line, a
status label or a test's wording, or a nit with no effect on results, cost or safety. Do not probe them; list them
at the end, one line each. A regression on a path that worked before, a plausible configuration that breaks, a
secret leaking in normal use, money or results counted wrong, and docs made false are never minor.

Look for:
- correctness bugs, regressions to existing behaviour, races and ordering problems, edge cases (empty, None, odd
  types, malformed input, timeouts, cancellation);
- docs, docstrings and comments that the change made false;
- places elsewhere in the repository that still assume the old behaviour (search docs/, examples/, tests/);
- tests that do not exercise what they claim, or that would pass without the fix they protect.
Do not report style preferences or naming taste.

Verify before reporting: where a finding can be shown with a small in-memory probe or by reading a specific line,
do that, and say which. A finding you believe but could not verify cheaply is still reported, marked
"unverified", with what would settle it; never drop a finding only because verifying it would take long.

Output, in this order:
1. Findings that block the merge, most severe first, numbered, each with a severity (P1 breaks normal use, P2 goes
   wrong under a specific configuration or event, P3 a rare combination or a docs error): file:line, what is wrong,
   a concrete input or scenario that shows it, and the minimal fix.
2. Minor points: one line each, file:line and what it is.
3. Coverage: every changed file, one line each, with what you checked in it. A changed file you did not review
   is listed as "not reviewed" with the reason.
4. "Checked, no issue": what you examined and found correct, one line each.
If there are no findings, say so plainly.
