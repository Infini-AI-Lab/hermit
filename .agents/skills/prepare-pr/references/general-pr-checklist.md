# General PR checklist

Answer each item in the PR with links to supporting files or records.

- [ ] **Core changes.** Does the pull request change shared or core code? If so,
  what changes, and why are they needed?
- [ ] **Implementation correctness and failures.** Trace affected execution paths,
  including configuration propagation, errors, timeouts and cancellation where
  relevant. Verify that failed or incomplete execution is reported accurately
  rather than as success.
- [ ] **Real execution paths and tests.** For behavior changes, exercise the actual
  entry point and check that its outputs can be consumed downstream. Do the tests
  cover this behavior, or do mocks and hand-built fixtures hide interface mismatches?
- [ ] **Documentation.** Which documents were added or changed to match the contribution?
- [ ] **This description.** Have you read the PR description end to end, and does
  every path, name, number and claim match the code and the run records it links to?
- [ ] **Independent review (post-PR audit).** After creating the PR or updating its
  code, has an agent in a separate session from the author reviewed the actual PR
  against the general and applicable task-specific checklists? Record its findings,
  fixes and remaining concerns. Follow [post-pr-audit](../../post-pr-audit/SKILL.md).
