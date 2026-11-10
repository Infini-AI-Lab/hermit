---
name: solve-issue
description: Take a GitHub issue to a merged fix - verify the issue against the code, fix it in a fresh worktree with tests that fail without the fix, open a PR that follows the repository's PR rules, run /dual-review on it, push the review fixes once the final tests pass, then squash-merge after CI and the user's go-ahead and clean up. Invoke with /solve-issue <issue number | URL> [repo].
user-invocable: true
---

# solve-issue

This skill takes an issue from a report to a merged pull request. It is the order of work that has held up in
practice: check the report, fix it, open the PR, get an independent review, then merge. It produces a PR that is
ready for merge, not a long one: fix what the issue says, record the rest.

## Inputs

ARGUMENTS: an issue number or URL; optionally the repository (`OWNER/NAME`). Without a repository, use the one the
current checkout's `gh repo view` names.

## Step 1 — read the issue, then check it against the code

1. Read the issue and its comments (`gh issue view <n> --comments`).
2. Verify every claim in it against the current code on the default branch, by reading the code it names, its callers,
   and anything that shares it (grep for the function across the repository, docs and tests). An issue is a starting
   point, not a specification.
   - Check who is affected; it is often fewer callers than the issue lists, because some already work around it.
   - Look for a pattern the codebase already uses for the same problem (a neighbouring function doing the same job for
     another kind of file, a record that already holds what is needed). Following it beats inventing a new one.
3. Where it can be reproduced cheaply, reproduce it (a short script in the scratchpad against the real code).
4. If the issue is wrong or the fix needs a design choice that is the user's to make, say so and ask before writing
   code. Otherwise state the plan in two or three lines and go on.

## Step 2 — fix it in a fresh worktree

1. `git fetch origin`, then `W=$(mktemp -d /tmp/<short-name>.XXXX)` and `git worktree add -b <branch> "$W" origin/<default>`.
   Never work in the user's main checkout, and never switch its branch.
2. Read the repository's own rules first: AGENTS.md / CLAUDE.md, CONTRIBUTING.md, and any PR-preparation skill or
   checklist they name (for hermit: `.agents/skills/prepare-pr`, the general and test checklists).
3. Make the smallest change that fixes the cause, in the style of the surrounding code.
4. Write a regression test for each behaviour the fix adds, in the test group of the code it covers. It goes through
   the entry point users call, with stand-ins only at the edge.
5. Show each test fails without the fix. Copy the test file into a throwaway worktree of `origin/<default>` and run
   it there, then remove that worktree. Do not `git stash` in a worktree where a test run is going.
6. Update every document the change makes false (user docs, docstrings, the changelog under Unreleased).
7. Check the real entry point once (for a CLI change, the CLI itself on a scratch workspace), not only the unit tests.

## Step 3 — check the whole change

- Run the related tests, then the repository's change-selected suite once, with `run_in_background: true` (for
  hermit: `python -m pytest --changed --base origin/<default>`, which runs every group when shared code changed). Do
  not start a long run with `&` inside a foreground command: the harness does not track it.
- Compare the collected count with what was selected; name the groups that were skipped for a missing dependency.
- Build the docs if any doc changed (with warnings as errors where the repository does), and lint the changed files.
  Report only problems on lines this change touched; name existing ones as existing.
- Re-run the affected tests after any later edit. A run that started before the edit does not cover it.

## Step 4 — open the PR

1. Commit with a message that says what was wrong and what the code does now; end it with the attribution lines the
   session gives.
2. Push the branch and open the PR against the default branch with `gh pr create --body-file`. The body follows the
   repository's template and checklists:
   - the problem and the behaviour now;
   - the interfaces and files touched, and what is deliberately unchanged;
   - the commands actually run, with their results;
   - what was not verified;
   - "Fixes #<n>".
   Mark the independent review as pending.
3. Read the body back against the final diff: every path, name, number and claim.

Invoking this skill authorizes the branch push and the PR. It does not authorize a merge.

## Step 5 — /dual-review the PR

Run `/dual-review <pr>` and follow it as written, including its rules. Its fixes stay in its worktree until the
user says to push them. Once the user has said so (for example "push once the tests pass"):

1. Wait for its final test run to pass.
2. Commit the fixes as one commit, show its message, and push to the PR branch. Use a normal push, never a force
   push; to bring in a newer default branch, merge it in, since the PR will be squash-merged.
3. Update the PR body:
   - the final test result;
   - each new test, and the fact that it fails without its fix;
   - the independent review's rounds, what it found, what was fixed, and what was left and why.

## Step 6 — merge, after CI and the user's go-ahead

1. Wait for CI with a background until-loop on `gh pr checks <pr>`, not by polling in the chat. If CI fails, do not
   merge: report the failing job and its cause.
2. Merge only when the user says so:
   `gh pr merge <pr> --squash --match-head-commit <tested head sha>`. Pinning the head means a commit pushed after
   the tests does not get merged untested. Use `--admin` only when the one thing blocking is a required review and
   the user has asked for the merge.
3. Confirm the PR is merged and the issue closed (it closes on "Fixes #<n>"; close it by hand otherwise).

## Step 7 — clean up, when the user says so

For each worktree this work created (the fix's, /dual-review's, any throwaway one):

1. Check `git -C <w> status --short` is clean.
2. Remove it with `git worktree remove`.
3. Delete its local branch.

Delete the PR's remote branch, then `git worktree prune`. Never delete a directory you did not create; look at it
first.

## Reporting

Report in the chat language, short and plain. At each pause say:
- where things stand;
- what is running, and how long it should take;
- what the user has to decide.

When the user asks for it simpler, give fewer lines, not the same lines in other words. At the end, give a table:
the PR and merge commit, the fixes, what was left and why, the tests, and the cleanup done.

## Rules

- Merging, pushing review fixes and deleting things each wait for the user's word. That word covers the step it
  names, not the next one.
- Never print or copy secrets.
- Temporary files go only in the scratchpad, `/tmp` or `/dev/shm`; no `rm -rf`.
- Do not switch `gh` accounts without asking.
- Long waits run in the background, with a one-line status in the chat.
