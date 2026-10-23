# Installation

The core framework needs **Python 3.11+ and Git**. It does not require a GPU. External tasks, model endpoints,
learner CLIs, and sandbox support depend on the workflow you choose.

## Install the checkout

If you do not have a checkout:

```bash
git clone https://github.com/haizhongzheng/hermit.git
cd hermit
```

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
hermit --help
```

Help should list `check`, `train`, `eval`, `deploy`, `convert`, and `serve`. Activate the environment again in
each new terminal. Editable installation uses this checkout's Python code.

## Choose the additional requirements

| Workflow | Additional requirements |
|---|---|
| [Offline first training](quickstart.md) | None beyond the core install |
| [First real-model training](first-model.md) | `python -m pip install -e '.[gepa]'`, a compatible chat-completions endpoint, model id, and credential; data is included |
| Generate a Codex / Claude Code / Pi workspace | No host CLI is needed until you start the host |
| Run an acting host | Installed and authenticated host, plus adapter tool dependencies |
| Prompt-only learner | The Codex CLI, Linux with bubblewrap, and a learner backend that serves the Responses API |
| GEPA | `python -m pip install -e '.[gepa]'`, learner endpoint, validation split |
| SkillOpt | `python -m pip install -e '.[skillopt]'`, learner endpoint, validation split |
| Codex / Claude learner | Selected authenticated CLI and supported program-execution environment |
| MWM | Linux learner environment, Codex, evidence store, task deployment contract, model access |
| Benchmark recipe | Its dataset, scorer, model settings, and environment; see [catalog](../recipes/index.md) |

Core installation and the deterministic tutorial work on macOS. Sandboxed learner **programs** use a
Linux-specific bubblewrap backend. For Ubuntu/Debian:

```bash
sudo apt-get install bubblewrap
bwrap --version
```

The environment must permit the required namespaces. Installing the binary alone may not satisfy a container or
cluster's namespace policy. The sandbox does not isolate your acting pipeline or arbitrary in-process Python.
See [sandbox boundaries](../framework/configuration.md#sandbox-network-and-warnings).

A recipe can require Python 3.12+ even though the core supports 3.11. Check external dependencies separately.
For exact experiment reproduction, record the framework commit and dependency revisions.

## Verify the local installation

```bash
hermit check --config docs/_examples/first_training/run.yaml
```

The probe should score the first toy task zero without execution errors. Its sandbox-disabled warning is expected
for the included in-process, offline teaching learner. Continue to [Quick Start](quickstart.md) to train and evaluate it.

## Optional: build documentation

```bash
python -m pip install -r docs/requirements.txt
bash docs/build.sh
bash docs/serve.sh 8000
```

Open <http://127.0.0.1:8000>. The preview rebuilds on source changes. The strict build treats warnings as errors.
The local preview includes design drafts under `_static/drafts/`; the default publication build excludes them.
For earlier `evolve` / `run` interfaces and current limitations, see [Compatibility](../reference/compatibility.md).
