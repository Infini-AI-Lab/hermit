<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/wordmark-dark.svg">
  <img src="assets/wordmark-light.svg" alt="MetaHarnessAdapter" width="540">
</picture>

**A Continual Learning Framework for the Deployment and Evolution of Harness Adapters**

[Quickstart](#quickstart) · [How it works](#how-it-works) · [Architectures](#architectures) · [Guide](docs/guide/index.md) · [Status](docs/guide/status.md)

[![License](https://img.shields.io/badge/license-Apache--2.0-C6743C.svg)](LICENSE) [![Python](https://img.shields.io/badge/python-3.11%2B-1B1D22.svg)](pyproject.toml)

</div>

---

MetaHarnessAdapter is an open-source continual learning framework for deploying and evolving external harness adapters. Instead of evolving the original harness, which can be impractical in many cases, we propose optimizing an external, portable harness (harness adapter) to improve agent performance in continual learning. The harness adapter enables us to enhance even complex or black-box agents, such as Codex and Claude Code. This repository provides the abstractions and interfaces that support the deployment and evolution of harness adapters.

## Quickstart

Requirements: Python 3.11+, and an installed, signed-in Codex CLI. Claude Code CLI is optional as a second host or learner. Model calls use your existing accounts.

```sh
pip install -e .
harnessadapter --help
```

**1. Deploy a starting architecture to a host.** This generates a managed workspace and opens Codex in it.

```sh
harnessadapter deploy prompt-only --host codex
```

**2. Evolve it from experience.** The Evolver reads the evidence directory and writes a candidate under `.demo/evolution/`. The source is never modified.

```sh
harnessadapter evolve --architecture prompt-only \
  --evidence examples/mwm_evidence \
  --prompt "Read the evidence and write useful, scoped instructions in ADAPTER.md." \
  --no-network
```

**3. Review and deploy the candidate.** Read the session's `control/report.md`, then deploy the path it reports.

```sh
harnessadapter deploy /path/to/session/adapter --host codex
```

The same lifecycle is available from YAML:

```sh
harnessadapter run config/evolve.yaml --dry-run   # print the equivalent CLI
harnessadapter run config/evolve.yaml
```

## How it works

```text
  Adapter or Architecture  +  Experience  +  Config  +  Training State (optional)
                                     │
                                  Evolver           (runs in an OS sandbox,
                                     │               bounded file tools only)
                    ┌────────────────┴────────────────┐
             Candidate Adapter               Training State (optional)
                    │                                 │
            review → deploy                    next iteration
```

The design borrows the vocabulary of model training, because the roles line up:

| Model training | MetaHarnessAdapter |
| --- | --- |
| Base model | The existing acting agent and its harness |
| Model architecture | **Architecture**: starting files, representation, interfaces |
| Trained adaptation module | **Adapter**: the learned prompt, knowledge, code, and roles |
| Training algorithm | **Evolver**: instructions plus an update procedure |
| Training data | **Experience**: histories, observations, feedback |
| Checkpoint / optimizer state | Adapter / optional **Training State** |
| Inference | Deploying the Adapter to a host |

Architecture and Evolver are independent choices. Adapters are text and code rather than weights, so evolution may change structure, not only content.

**Deployment.** An Adapter exports up to three things. Context is injected into the host's project instructions (`AGENTS.md` or `CLAUDE.md`) inside a marked, replaceable block. Plain Python callables are served as MCP tools by the framework; the Adapter contains no server. Role definitions are converted to native Codex or Claude Code subagents. Hosts start the MCP server themselves; no global settings are touched.

**Evolution.** A learner session (Codex or Claude Code as the model) edits the candidate through framework-owned file tools: list, read, write, move, delete, read image, validate. Every operation executes under the Codex OS sandbox with an explicit policy: the candidate is writable, experience is read-only, additional environment reads are opt-in, and network can be disabled for tools without disabling model inference. Each session records input snapshots, the tool audit, a report, and a result. `candidate_ready` means the run completed and structural checks passed, not that performance improved. Nothing is promoted automatically.

See [Components](docs/guide/components.md), [Evolution](docs/guide/evolution.md), and the [execution policy](docs/execution-policy.md).

## Architectures

Two bundled starting points share one deployment and evolution pipeline. A local directory path works anywhere a bundled name does.

| Architecture | What the host sees | What evolves |
| --- | --- | --- |
| `prompt-only` | Markdown context only; no MCP server | One learned `ADAPTER.md`, initially empty |
| `mwm` | Generated context plus a `wm` MCP tool; optional native roles | Prose memory, worked examples, per-tool bundles, roles, briefing, journal, cases |

```sh
harnessadapter deploy mwm --host claude-code
harnessadapter evolve --architecture mwm --evidence examples/mwm_evidence --no-network
```

Without `--prompt`, `mwm` selects the bundled MWM Evolver, which maintains a journal and docket alongside the memory. With `--prompt`, the generic prompt-driven Evolver runs instead. Pass `--evolver /path/to/definition` to bring your own.

## Evolver permissions

```sh
harnessadapter evolve mwm --backend codex       --evidence ./evidence --no-network
harnessadapter evolve mwm --backend claude-code --evidence ./evidence --read-all
harnessadapter evolve mwm --evidence ./evidence --read-dir ../project --model gpt-5.6-sol --reasoning-effort high
```

| Flag | Effect |
| --- | --- |
| `--evidence DIR` | Read-only experience, snapshotted into the session |
| `--validation DIR` | Optional read-only storage visible to the learner; not an automatic evaluator |
| `--read-dir DIR` / `--read-all` | Additional read-only environment access; never grants writes |
| `--no-network` | Blocks network for tools and commands, not for model inference |
| `--model` / `--reasoning-effort` | Explicit learner model; effort applies to Codex only |
| `--prepare-only` | Generate the session without calling a model |

Full reference: [Configuration](docs/guide/configuration.md).

## Example: minimal shop

[`examples/minimal_shop/`](examples/minimal_shop/README.md) is a complete Adapter in one ordinary directory: a memory store, a context prompt, three Python capabilities exposed as MCP tools, and one role converted to a native subagent. Copy it out to start your own.

```sh
harnessadapter minimal-shop --codex     # or --claude
```

Then send: `Run the adapter integration task in PROMPT.md.`

## Repository layout

```text
harnessadapter/        the package (pip install -e .)
  cli.py                 deploy | evolve | run
  adapter/               manifest loading, in-process exports, evidence bindings
  runtime/               MCP wrapper, bubblewrap execution primitive
  evolution/             sessions, Evolver definitions, learner backends, launch YAML
  policy/                the sandboxed tool broker and its file operations
  hosts/                 Codex / Claude Code workspace conversion
  evolvers/              bundled Evolver presets: basic, mwm
  architectures/         bundled starting structures: prompt_only, mwm
examples/              minimal_shop demo and mwm_evidence fixture
docs/                  guide/ plus design notes and contracts
site/                  interactive design notebook (python3 -m http.server --directory site/dist)
tests/  tools/  config/
```

## Status

This is a working local prototype, not a released package.

**Implemented.** Prompt-only and MWM architectures. Context injection, MCP registration, and native role conversion for Codex and Claude Code. Prompt-driven evolution with either host as the learner, plus the MWM method preset. OS-enforced tool boundaries with audit records. Sandboxed image reads for visual experience. CLI and YAML launch.

**Not yet.** A published package or SDK. Generic Training State persistence and resume. Automated evaluation, candidate promotion, or rollback. Merging an Adapter into an existing project's settings rather than a generated workspace. GEPA, SkillOpt, Reef, or external Evolver plugins. Both learners currently require the Codex CLI as the sandbox executor; MWM learned-program admission additionally needs Linux bubblewrap.

Details and troubleshooting: [Status and limits](docs/guide/status.md).

## Learn more

- [Guide](docs/guide/index.md): quickstart, components, architectures, deployment, evolution, configuration.
- [Adapter contract v0](docs/adapter-contract-v0.md): context, MCP tools, optional roles and helpers.
- [Core and evolution contract](docs/core-and-evolution.md) and the [MWM fidelity correction](docs/mwm-fidelity-correction.md).
- [Design discussion](docs/design-discussion-2026-09-18.md), [subagent field survey](docs/subagent-fields-2026-09-18.md), and integration findings for [Claude Code](docs/claude-demo-findings-2026-09-18.md) and the [minimal demo](docs/minimal-demo-findings-2026-09-18.md).
- Interactive design notebook: `python3 -m http.server 8873 --bind 127.0.0.1 --directory site/dist`, then open <http://127.0.0.1:8873>.

## Contributing

Issues and pull requests are welcome. Run `pytest` before submitting; tests that need the Codex sandbox skip automatically when it is absent. Repository documentation, examples, and prompts are maintained in English.

## License

[Apache License 2.0](LICENSE). Contributions are accepted under the same terms, as described in section 5 of the license.
