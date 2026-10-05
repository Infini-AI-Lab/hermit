# Installation

Install the Python package first. The agent host, model access, and task data depend on what you want to run.
The framework itself does not require a GPU.

## Requirements

- Python **3.11 or newer**.
- Git, used by the Trainer to store adapter versions and checkpoints.
- Linux with `bubblewrap` (`bwrap`) for sandboxed agent or command Evolvers. The current sandbox backend is Linux-specific.

The package and the context-only deployment walkthrough work on macOS as well. Running a learner program with
`sandbox.enabled: true` requires `bwrap`; installation of the Python package alone does not provide it.

## Install from your checkout

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
harnessadapter --help
```

The help output should list `check`, `train`, `eval`, and `deploy`. Editable installation makes this checkout the
source of the imported code. On a new terminal, activate the same virtual environment before using the commands.

If you do not have a checkout yet:

```bash
git clone https://github.com/haizhongzheng/MetaHarnessAdapter.git
cd MetaHarnessAdapter
```

These pages describe the 0.2.0 framework. If your checkout has the earlier `evolve` / `run` interface, switch to a
revision that contains `check`, `train`, and `eval` before following this guide.

## Install only the integrations you use

| Use case | Additional requirements |
|---|---|
| Inspect an adapter or generate a host workspace | None beyond the package. `deploy` does not start an agent. |
| Run an adapter in Codex or Claude Code | The chosen host installed and authenticated. |
| Use `prompt_only` as the learner | A configured chat-completions endpoint, model, and API credentials in `run.yaml`. |
| Use the `codex` or `claude` Evolver | The corresponding agent CLI, its model access, and a supported execution environment. |
| Run the OfficeQA example | Prepared OfficeQA corpus and scoring files, plus Fireworks model access; see [OfficeQA](../recipes/officeqa.md). |
| Run Lean collaboration tools | A Lean/Mathlib environment and the per-agent environment variables described in the template's README. |

For sandboxed learner programs on Ubuntu/Debian:

```bash
sudo apt-get install bubblewrap
bwrap --version
```

The machine must also permit the namespaces bubblewrap uses. The sandbox wraps learner programs launched through
the framework; it does not isolate the user-supplied rollout pipeline or arbitrary in-process Evolver code.
Disabling it with `sandbox.enabled=false` runs those learner programs with the launching user's access. See
[Configuration](../framework/configuration.md#sandbox-network-and-warnings) for the boundary and network settings.

## Optional: build these docs

```bash
python -m pip install -r docs/requirements.txt
bash docs/build.sh
bash docs/serve.sh 8001
```

Open <http://127.0.0.1:8001>. The preview rebuilds when source files change.

Continue to [Quick Start](quickstart.md) to inspect and deploy your first adapter.
