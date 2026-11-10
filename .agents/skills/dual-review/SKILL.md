---
name: dual-review
description: Review a PR with two independent reviewers (this session and a fresh Codex gpt-6.1-sol at high effort), merge and verify their findings, fix, and repeat until the PR is ready for merge; minor points are recorded, not fixed. Ends with the merge verdict, the changes made and every unresolved finding with its reason. Invoke with /dual-review <PR number | URL | branch> [repo].
user-invocable: true
---

# dual-review

Two reviewers that do not see each other's work catch more than either alone. This skill runs that loop on a PR:
each round, this session reviews the change while a fresh Codex session reviews it independently; this session
merges the two lists, verifies every finding against the code, fixes what is real, and starts the next round on the
result. The goal is a PR that is ready for merge, not a PR with no findings left: minor points are recorded and
left alone, since fixing them costs rounds and tends to break real paths. It stops when a round turns up nothing new
that blocks the merge.

Run it in a Claude Opus 5.5 session at high effort (`/model`, `/effort`); say so at the start if the session is on
something else. The independent reviewer is always Codex with `gpt-6.1-sol` at `model_reasoning_effort="high"`.

## Inputs

ARGUMENTS: a PR number or URL, or a branch; optionally the repository (`OWNER/NAME`). Without a repository, use the
one the current checkout's `gh repo view` names; if that is not the PR's repository, ask.

## Step 0 — set up

1. Fetch the PR head into a fresh worktree: `W=$(mktemp -d /tmp/dualrev.XXXX)` and `git worktree add` the PR head
   there on a local branch `<pr>-dualrev`. Never reuse or `rm` an existing directory; never work in the user's main
   checkout.
2. Record the base (`gh pr view --json baseRefName,headRefOid`) and the diff scope: `git diff <merge-base>...HEAD`.
3. Create the ledger in the session scratchpad, `dual-review-<pr>.md`: the PR, the head commit, the base, and an empty
   findings table (columns: id, round, source (self / codex / both), file:line, finding, verdict, action, status).
   The ledger is the state of the loop; every finding goes in it, whatever happens to it.
4. Find the tests that cover the changed files and run them once on the untouched head, so that a later failure can
   be told from one that was already there. Note any failure that predates the review in the ledger.

## Step 1 — one round

Start both reviews in the same turn so they run in parallel.

**The Codex review (background).** Write the prompt to `scratchpad/dual-review-<pr>-r<N>-prompt.md` from
`codex-prompt.md` in this skill's directory, filled in with: the repository and PR, the diff scope (the PR's diff
on round 1; on later rounds the PR's diff plus the fixes so far, `git diff <merge-base>`), what the PR intends (from
its description, in your own words), and on rounds after the first the findings already settled (id, one line,
status) so it does not re-report them. Then run, with `run_in_background: true` (`S` is the session scratchpad,
`W` the worktree):

```bash
codex exec --skip-git-repo-check -C "$W" -s read-only -m gpt-6.1-sol -c model_reasoning_effort="high" \
  -o "$S/dual-review-<pr>-r<N>-codex.md" - < "$S/dual-review-<pr>-r<N>-prompt.md" \
  > "$S/dual-review-<pr>-r<N>-codex.log" 2>&1
```

Every round starts a new `codex exec`: no `resume`, no shared session, so each review is independent of the last.
Codex is read-only: it reviews, it never edits.

**The self review (this session, at the same time).** Review the same scope without reading the Codex output:
read every changed file in full and the code it calls, then look for correctness bugs, regressions, races and edge
cases, docs and docstrings that the change made false, places elsewhere in the repo that still assume the old
behaviour (grep `docs/`, `examples/`, `tests/`), and tests that do not test what they claim. Write the findings to
the ledger before opening the Codex result.

**Merge and verify.** When the Codex result arrives, add its findings to the ledger, mark findings both reviewers
made as `both`, and verify each one yourself before acting: read the code it points at, and reproduce it when it can
be reproduced (a short script or a test). A finding is never fixed, or reported as real, on the reviewer's word
alone. If its Coverage section marks a changed file "not reviewed", review that file yourself this round. Give each
a verdict:

| verdict | meaning | action |
|---|---|---|
| confirmed | reproduced or plainly true in the code | fix |
| not a problem | the code is right; say why, with the line that shows it | none |
| disputed | real but the fix is a design choice the PR author or the user should make | record, ask at the end |
| uncertain | cannot be settled from the code or a reproduction (needs a host, a model, data) | record what would settle it |
| minor | real, but normal use does not reach it (see below) | record, do not fix |
| not worth it | real but the fix costs more than the problem (say both) | record |
| out of scope | real but predates the PR and is not made worse by it | record |

Minor means one of: it needs a contrived input (a secret inside a file name or link target, non-UTF-8 or over-long
names, IPv6-literal hosts, modules imported from zip archives, a caller who passes a symlink where the code makes
the path itself, rename collisions, a malformed value no real producer emits); it only changes a log line, a status
label or a test's wording; or it is a P3 nit with no effect on results, cost or safety. Not minor, whatever the
reviewer's severity: a regression on a path that worked before the PR, a plausible configuration that breaks (a CLI
in `~/.local`, an editable install, an option the docs offer), a secret reaching somewhere it should not in normal
use, money or results counted wrong, and docs the change made false.

Give each finding a severity of your own after verifying it (P1 breaks normal use, P2 goes wrong under a specific
configuration or event, P3 a rare combination, a dropped setting or a docs error), and note in the ledger where it
differs from the reviewer's and why. Report your severities, not the reviewer's.

**Fix.** For each confirmed finding: the smallest change that fixes the cause, in the style of the surrounding code;
a regression test that fails without the fix whenever one can be written; the affected tests run. Then self-audit
the round's fixes (read the diff again, line lengths, lint, stale docs). Record each fix in the ledger as one line:
before -> after -> what it solved.

Text that an agent reads at run time (prompts, learner-facing docs, templates' instructions) is changed only with
its exact new wording shown to the user before it lands; list such changes for the user's review at the end of the
round.

## Step 2 — the next round, and when to stop

Start the next round on the fixed code (Step 1 again, a new Codex session). Stop when a round's merged findings
contain no new confirmed finding (minor ones do not count), or after 10 rounds (say which). A finding re-reported
after it was settled is not new; a finding about a fix made in an earlier round is. Do not start a round only to
chase minor points.

## Step 3 — the report

Run the tests the change needs once on the final tree (in the background): `python -m pytest --changed --base
<base ref>` where the repository has it (hermit does, `tests/selection.py`; it runs every group when the change
touches shared code), the full suite where it does not. Use an environment with the dependency groups the selected
tests need, and compare the collected count with the selection so that skipped groups are not read as a pass.
Check whether the branch still merges into its base cleanly (`git merge-tree --write-tree <base> HEAD`; name
conflicting files, do not resolve them without the user), and report, in the chat language, with tables:

0. Merge verdict: ready for merge or not, and if not, each blocker in one line.
1. Rounds: per round, the findings from each reviewer, how many were confirmed, fixed, or left.
2. Changes: one line per fix, before -> after -> what it solved, with the test that covers it.
3. Unresolved findings: each disputed / uncertain / not-worth-it / out-of-scope one, with its reason and what would
   change the verdict; minor ones as a short list, one line each.
4. Tests: the command, the groups it selected, the result, and failures that predate the review named as such.
5. Text changes that need the user's review, quoted in full.

## Rules

- Nothing leaves the worktree on the skill's own initiative: no commit, push, PR comment, review or merge unless the
  user says so in this conversation. Show the commit message before committing.
- Never print or copy secrets (tokens, `auth.json`, API keys).
- Long waits (Codex, test suites) run in the background; the chat gets a short status, not a blocking wait.
- Codex is a reviewer, not an authority: its findings are verified like the session's own, and its `Checked, no
  issue` list is not a substitute for reading the code.
- No temporary file outside the scratchpad, `/tmp` or `/dev/shm`; no `rm -rf`.
