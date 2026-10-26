# Contributing to Hermit

Contributions can add learning methods, adapter designs, task integrations, host exporters, tests, or documentation.
Start with the [developer guide](docs/developer/index.md) and choose the interface for your change.

## Set Up

Hermit core requires Python 3.11+ and Git. From a checkout:

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
integrations have `tests/test_optimizer_evolvers.py`:

```bash
python -m pip install -e . '.[test]' -r requirements-gepa.txt -r requirements-skillopt.txt
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

## Propose a Change

For bugs, include a small reproducer, the framework version or commit, platform, and relevant configuration.
Remove credentials and private task data from shared logs. Use [private reporting](SECURITY.md) for vulnerabilities.

For pull requests, describe the resulting behavior, affected interfaces, and validation. Keep documentation,
commands, and defaults consistent. Report external requirements and unverified platform-specific checks.

Use the existing task, adapter, and Evolver interfaces where possible. For the contracts and extension examples,
see [task integration](docs/tutorials/your-own-task.md), [adapter customization](docs/tutorials/customize-an-adapter.md),
and [custom Evolvers](docs/framework/custom-evolver.md).
