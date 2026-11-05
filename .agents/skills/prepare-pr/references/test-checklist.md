# Test checklist

Answer these items alongside the [general checklist](general-pr-checklist.md) when a pull request adds or changes
tests, with links to the tests.

- [ ] **Group and selection.** Is each test file in the group of the code it covers (`tests/core/`,
  `tests/methods/<name>/`), and does `python -m pytest --changed` run it for a change to that code? Is a new helper
  or fixture directory listed in `tests/selection.py` and in [tests/README.md](../../../../tests/README.md)?
- [ ] **Names.** Does each test's name state the behaviour it holds, as a sentence
  (`test_a_cli_that_never_started_is_an_error_not_a_wrong_answer`), rather than the function it calls or a number
  (`test_run`, `test_basic_2`)?
- [ ] **Docstrings.** Does each new file open with a docstring that names the code under test, the stand-ins it uses
  and what it covers? Where the name cannot carry it, does a test's docstring say why the behaviour matters (the
  past bug, the boundary it guards)?
- [ ] **What it asserts.** Does each regression test fail without its fix, and how was that checked? Does it assert
  what a caller sees (a status, a record, a file, a raised error), not only that nothing raised? Is an error's text
  matched only where the text is the contract?
- [ ] **Real entry points and stand-ins.** Does a behaviour's test go through the entry point users call
  (`Config.load`, the Trainer, the endpoint with the real client, a Base Agent's `run`), with stand-ins only at the
  edge (the model, the CLI)? Is a stand-in built from what the real program prints (a recorded stream, its source and
  version stated beside it), rather than from what the code under test expects?
- [ ] **Isolation.** Does the test touch only `tmp_path` and the fixtures of `tests/conftest.py`: no user home,
  sign-in, token or `~/.cache`, no network, and no installed CLI unless it is marked `integration`? Does it skip,
  with a reason, when a platform or dependency group it needs is missing?
- [ ] **Time and order.** Does the test pass on any date and in any order: times relative to now rather than fixed
  timestamps that expire, no state left for another test, and a condition polled against a deadline rather than a
  fixed sleep?
- [ ] **Cost.** Is a check that another test already makes left out, and do tests that need the same expensive setup
  (a Trainer run, a sandbox) share it? State the new tests' runtime (`--durations`).
- [ ] **Hygiene.** Are there no unused imports, and no `# noqa` added to keep them? Are shared fixtures defined in a
  `conftest.py` rather than imported from a module? Are the processes and files a test starts gone when it ends?
