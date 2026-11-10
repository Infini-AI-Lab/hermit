# Installation

The core framework needs **Python 3.12+ and Git**. It does not require a GPU. External tasks, model endpoints,
learner CLIs, and sandbox support depend on the workflow you choose.

## Install the checkout

**Recommended for examples, benchmark recipes, and development.**

If you do not have a checkout:

```bash
git clone https://github.com/Infini-AI-Lab/hermit.git
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

## Install from PyPI

Install the core framework in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install hermit-harness
hermit --help
```

The package includes the CLI, adapter templates, and built-in Evolvers. GEPA, SkillOpt, and Harness² also need
their method-specific dependencies. The example data, benchmark recipes, and method dependency groups live in
the repository; use a checkout for those workflows.

## Install method dependency groups

Method dependencies are declared once in `pyproject.toml`, separately from the core package dependencies.
Run these commands from the checkout root in the environment that runs Hermit. Group installation requires
**pip 25.1+** and Git:

```bash
python -m pip install --upgrade "pip>=25.1"
python -m pip install -e . --group gepa
# Other methods:
python -m pip install -e . --group skillopt
python -m pip install -e . --group harness2
python -m pip install -e . --group ace
python -m pip install -e . --group rrsi
# Or all method dependencies:
python -m pip install -e . --group all
```

To combine methods, repeat the option, for example `--group skillopt --group gepa` for OfficeQA.
uv reads the same groups: `uv pip install -e . --group gepa`.
Each group but `ace` (`sentence-transformers` alone) retains its tested upstream Git commit. `all` includes the five
method groups; it does not
install host CLIs, datasets, or system tools. Groups are not published as package extras, so
`pip install hermit-harness` still installs only the core; `hermit-harness[gepa]` is not supported.
Without a checkout, install a method's pinned requirements from its group in `pyproject.toml` directly; the
Evolver's missing-dependency error prints them. Documentation build dependencies are in `docs/requirements.txt`.

The upstream Harness² package supports Python 3.12 and 3.13 only, so `--group harness2` and `--group all` fail on
Python 3.14; install `gepa` and `skillopt` separately there.

## Install the sandbox's packages

The bubblewrap sandboxes (the acting agent's, the learner's, the adapter's) show the environment Hermit runs in:
what is installed there is what their code can import. The `sandbox` extra installs a base, numpy, pandas, scipy
and torch:

```bash
python -m pip install -e ".[sandbox]"      # a checkout
python -m pip install "hermit-harness[sandbox]"
```

PyPI's torch for Linux is the CUDA build; for the CPU one, install torch first from
`https://download.pytorch.org/whl/cpu`. Install Hermit in an environment of its own: a package installed beside it
is shown to the sandboxes too (`sandbox.hide` covers a benchmark's scoring module; an editable install's checkout is
never shown).

## Install mini-swe-agent

The `mini-swe-agent` acting agent and `hermit deploy --host mini-swe-agent` need mini-swe-agent (2.4.6, which brings
litellm), published as an extra:

```bash
python -m pip install -e ".[mini-swe-agent]"      # a checkout
python -m pip install "hermit-harness[mini-swe-agent]"
```

## Choose the additional requirements

| Workflow | Additional requirements |
|---|---|
| [Offline first training](quickstart.md) | None beyond the core install |
| [First real-model training](first-model.md) | `python -m pip install -e . --group gepa`, a compatible chat-completions endpoint, model id, and credential; data is included |
| Generate a Codex / Claude Code / Pi / OpenCode / DeepSeek Harness workspace | No host CLI is needed until you start the host |
| Act with, or deploy to, mini-swe-agent | The `mini-swe-agent` extra and a chat-completions model ([tutorial](../tutorials/mini-swe-agent.md)) |
| Run an acting host | Installed and authenticated host, plus adapter tool dependencies |
| Prompt-only learner | The Codex CLI, Linux with bubblewrap, and a learner backend that serves the Responses API |
| GEPA | `python -m pip install -e . --group gepa`, learner endpoint, validation split |
| SkillOpt | `python -m pip install -e . --group skillopt`, learner endpoint, validation split |
| Harness² | Python 3.12 or 3.13, `python -m pip install -e . --group harness2`, Claude Code, and the execution environment described in [Harness²](../methods/harness2.md) |
| RRSI Adapter | `python -m pip install -e . --group rrsi`, a CLI for the proposer and its analyst, digesters and critic (Codex or Claude Code), a training split evaluated k times per task |
| Codex / Claude learner | Selected authenticated CLI and supported program-execution environment |
| MWM | Linux learner environment, Codex or Claude Code, evidence store, task deployment contract, model access |
| Acting and learning on a Claude subscription | Claude Code 2.1.248 or later, `claude setup-token`, Linux with bubblewrap for its sandboxes; see [Claude subscription](../guides/claude-subscription.md) |
| Pi acting agent or learner | The `pi` release binary on `PATH` (GitHub release `pi-linux-x64.tar.gz`, checked against its `SHA256SUMS`) and a model reference |
| OpenCode acting agent or learner | The `opencode` 2.2 release binary on `PATH` (GitHub release `opencode-linux-x64.tar.gz` of `anomalyco/opencode`, checked against the release's sha256 digest) and a model reference |
| DeepSeek Harness acting agent or learner | The `dsh` command on `PATH` (Node.js 24.9 or later, `npm install -g @deepseek-ai/dsh@0.2.2-rc.1`) and a model reference |
| Benchmark recipe | Its dataset, scorer, model settings, and environment; see [catalog](../recipes/index.md) |

Core installation and the deterministic tutorial work on macOS. The sandboxes (the acting agent's, the judge's, the
learner's) use a Linux-specific bubblewrap backend; macOS has no evolver sandbox. For Ubuntu/Debian:

```bash
sudo apt-get install bubblewrap
bwrap --version
```

The environment must permit the required namespaces. Installing the binary alone may not satisfy a container or
cluster's namespace policy (on Ubuntu 24.04, unprivileged user namespaces need an AppArmor profile for bwrap or the
`kernel.apparmor_restrict_unprivileged_userns=0` setting). The sandbox does not isolate arbitrary in-process Python.
See [sandbox boundaries](../framework/configuration.md#sandboxes).

A recipe or method can support fewer Python versions than the core. Check external dependencies separately.
For exact experiment reproduction, record the framework commit and dependency revisions.

## Verify the local installation

From the source checkout:

```bash
hermit check --config docs/_examples/first_training/run.yaml
```

The probe should score the first toy task zero without execution errors, its agent in the task sandbox. The
`best_effort` warning is expected: the example runs where bubblewrap is missing too, unconfined and recorded. Continue to [Quick Start](quickstart.md) to train and evaluate it.

## Optional: build documentation

```bash
python -m pip install -r docs/requirements.txt
bash docs/build.sh
bash docs/serve.sh 8000
```

Open <http://127.0.0.1:8000>. The preview rebuilds on source changes. The strict build treats warnings as errors.
The local preview includes design drafts under `_static/drafts/`; the default publication build excludes them.
For earlier `evolve` / `run` interfaces and current limitations, see [Compatibility](../reference/compatibility.md).
