# Contributing to Hermit

Contributions can add learning methods, adapter designs, task integrations, host exporters, tests, or documentation.
Start with the [developer guide](docs/developer/index.md) and choose the interface for your change.

## Set Up

Hermit core requires Python 3.12+ and Git. From a checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
```

Benchmark data, agent CLIs, and model credentials depend on the integration. The
[installation guide](docs/get-started/installation.md) lists the requirements.

## Verify a Change

Run the core checks and the offline training walkthrough:

```bash
python -m pytest tests/test_training.py tests/test_spec.py tests/test_deploy.py
hermit check --config docs/_examples/first_training/run.yaml
hermit train --config docs/_examples/first_training/run.yaml
hermit eval --config docs/_examples/first_training/run.yaml --adapter final
```

Select additional tests for the affected interface. Host exporters have `tests/test_convert.py`; algorithm
integrations have `tests/test_optimizer_evolvers.py`, which needs the method dependency groups (pip 25.1+):

```bash
python -m pip install -e '.[test]' --group all
python -m pytest tests/test_optimizer_evolvers.py
```

The optimizer tests use scripted responses and need no model credentials. The complete suite is:

```bash
python -m pytest tests
```

Sandbox and MWM integration tests require Linux, bubblewrap, and usable namespaces; some tests do not yet skip
automatically when those prerequisites are absent. On macOS, run the applicable core tests and identify the
Linux checks that remain unverified. See [sandbox configuration](docs/framework/configuration.md) for the
execution boundaries. CI and platform-test organization are tracked separately for follow-up work.

For documentation changes:

```bash
python -m pip install -r docs/requirements.txt
bash docs/build.sh
```

The build fails on warnings and excludes local design drafts. `bash docs/serve.sh` starts a local preview
that includes those drafts.

Optional file checks:

```bash
python -m pip install pre-commit
pre-commit run --all-files
```

The hooks check TOML, YAML, merge-conflict markers, and newly added large files. They do not reformat code.

Do not commit generated training or evaluation data, per-task results, trajectories, logs, checkpoints, or per-run adapter snapshots. Keep these artifacts in ignored local directories. Include reproducible recipes, reusable configurations, and aggregate results in the documentation and PR description instead.

## Suggested review workflow

Contributors are encouraged to review their changes iteratively, both before opening a PR and after updating it.
Existing validation and review requirements still apply.

1. **Review the complete change.** Use the general and all applicable task-specific checklists. Inspect the
   implementation, relevant callers, tests and documentation.
2. **Verify findings before fixing them.** Confirm suspected problems against the current code. Fix actionable
   issues, and explain concerns that are rejected or accepted as design tradeoffs.
3. **Validate the fixes.** Run the affected checks and update documentation, experiment records and the PR
   description where needed.
4. **Review again when useful.** Repeat the review–fix–verify cycle while it continues to uncover actionable
   issues. A fresh agent session can provide an independent perspective.
5. **Continue after opening the PR.** Run the post-PR audit and repeat the cycle after substantive fixes or new
   review feedback. Make clear which version each review covers.

Use judgment to decide when the change is ready. A useful stopping point is when the latest review finds no new
actionable issues, earlier findings have been addressed or explained, and remaining limitations are disclosed.
There is no prescribed number of review rounds.

## Propose a Change

For bugs, include a small reproducer, the framework version or commit, platform, and relevant configuration.
Remove credentials and private task data from shared logs. Use [private reporting](SECURITY.md) for vulnerabilities.

For pull requests, describe the resulting behavior, affected interfaces, and validation. Keep documentation,
commands, and defaults consistent. Report external requirements and unverified platform-specific checks.

**Documentation-only pull requests must be discussed with the core team first.** Open an issue, agree on the
change there, and link it in the pull request. Pull requests without that discussion will be closed.

A pull request that reproduces a published learning method follows
[Reproduce a published method](docs/developer/reproducing-a-method.md) and completes the general and
method-specific checklists linked from its PR section.

Use the existing task, adapter, and Evolver interfaces where possible. For the contracts and extension examples,
see [task integration](docs/tutorials/your-own-task.md), [adapter customization](docs/tutorials/customize-an-adapter.md),
and [custom Evolvers](docs/framework/custom-evolver.md).
