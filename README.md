# metaHarnessAdapter

A prototype framework for core-only, evolvable Harness Adapters. Adapters contain knowledge, prompts, ordinary Python capabilities, roles, and declarations. The framework owns MCP wrapping, host integration, and candidate evolution outside the adapter.

The interactive design notebook is in [`site/`](site/README.md). Start it locally:

```sh
python3 -m http.server 8873 --bind 127.0.0.1 --directory site/dist
```

Visit <http://127.0.0.1:8873>. The `harnessadapter` package and CLI are implemented; a published release and framework integrations remain planned.

## Install

```sh
pip install -e .          # Python 3.11+; installs the harnessadapter command
harnessadapter --help
```

Evolution sessions need an installed, signed-in Codex CLI (its sandbox is the tool broker for both learner backends) and, for Claude Code as the learner, that CLI too. Learned-program admission needs Linux bubblewrap.

## Start with an architecture

Two initial architectures share the same deployment and evolution pipeline:

| Architecture | Acting exports | Core |
| --- | --- | --- |
| `prompt-only` | Markdown context only; no adapter MCP server | One initially empty learned prompt |
| `mwm` | Generated context + `wm` MCP tool; optional roles later | MWM memory, examples, programs, roles, briefing, journal, cases |

```sh
harnessadapter evolve --architecture prompt-only --evidence examples/mwm_evidence \
  --prompt "Read the evidence and improve ADAPTER.md." --no-network
harnessadapter deploy prompt-only --host codex
harnessadapter run config/evolve.yaml --dry-run
```

Evolution returns a separate candidate directory; deploy that path to use the learned version. Architecture and Evolver are separate choices. Prompt-only does not implement GEPA or SkillOpt. `minimal-shop` remains a pre-populated integration demo.

[Read the guide](docs/guide/index.md), or visit the [local Docs site](http://127.0.0.1:8873/docs/). Optional `--validation DIR` binds read-only, learner-visible storage, not an automatic evaluator.

## Repository layout

```text
harnessadapter/          # the package (pip install -e .)
  cli.py                   harnessadapter deploy | evolve | run
  adapter/                 manifest loading, in-process exports, evidence bindings
  runtime/                 MCP wrapper, bubblewrap execution primitive
  evolution/               evolution sessions, Evolver definitions, learner clients, launch YAML
  policy/                  the sandboxed tool broker and its file operations
  hosts/                   Codex / Claude Code workspace conversion
  evolvers/                bundled Evolver presets: basic, mwm
  architectures/           bundled starting structures: prompt_only, mwm
examples/                # minimal_shop (runnable demo) and mwm_evidence (its synthetic evidence)
tools/                   # developer scripts (demo runs, verification, MCP smoke)
docs/  tests/  config/  site/
```

Bundled evolvers and architectures ship with the package; a local path works wherever a bundled name does. Evolved Adapters are independent outputs, not additions to the bundled architectures: by default candidates go under .demo/evolution/ in the checkout (`--output` names another directory); deploy the candidate path reported by the run.

## Core and framework ownership

- `examples/minimal_shop/`: shop knowledge and plain Python capabilities. No MCP server inside.
- `harnessadapter/architectures/mwm/`: The original MWM layout: prose memory, worked examples, per-tool bundles, SPEC.md roles, briefing, journal, and persistent cases. All core files may evolve.
- `harnessadapter/adapter/`, `runtime/`, `evolution/`, `policy/`: the shared loader, MCP wrapper, experience binding, tool broker, and candidate evolution facilities.
- `harnessadapter/evolvers/mwm/`: the MWM method materials, prompt renderer, and journal/docket maintenance.
- `harnessadapter/hosts/`: Codex / Claude Code workspace conversion; `tools/`: developer verification scripts.

[Read the core and evolution contract](docs/core-and-evolution.md), or the [MWM fidelity correction](docs/mwm-fidelity-correction.md).

## Optional Training State

The agreed evolution protocol returns two outputs: an updated Adapter and optional Training State, analogous to a model checkpoint and optimizer state. Stateless Evolvers return no state. Stateful Evolvers define their own state format separately from their reusable logic; acting agents only consume the Adapter. Generic Training State persistence and resume are planned, not current CLI features. See [the component contract](docs/guide/components.md).

## Evolver permissions

The candidate is writable, experience is always readable and read-only, and additional environment reads are user-defined. Network is on by default and can be disabled for agent tools and commands without disabling model inference.

```sh
harnessadapter evolve mwm --backend codex \
  --evidence examples/mwm_evidence --no-network

harnessadapter evolve mwm --backend claude-code \
  --evidence examples/mwm_evidence --read-all
```

Use `--read-dir` for specific read-only environment directories or `--config config/evolution.toml`. Both clients use framework-controlled tools and real sandbox preflight. The current shared executor requires an installed Codex CLI even for the Claude learner. See [the execution policy](docs/execution-policy.md) for enforcement, runtime exceptions, and network semantics.

## MWM reference implementation

```sh
harnessadapter mwm --codex
harnessadapter mwm --claude

# Invokes the default Codex learner; writes a separate candidate, not this architecture
harnessadapter evolve mwm --evidence examples/mwm_evidence
```

The experience is a synthetic integration fixture, not benchmark data. The learner edits through bounded file tools. Learned programs require sandboxed admission; this Mac lacks the Linux backend, so no unsandboxed execution is substituted. Checks establish interface validity, not performance improvement. See the contract for omitted research backends and sandbox limitations.

## Minimal runnable demo

[`examples/minimal_shop/`](examples/minimal_shop/README.md) contains a small memory store, a Context prompt, three ordinary Python capabilities exposed by the framework as MCP tools, and one role converted into native Codex or Claude Code subagent definitions. It is an ordinary directory, with no submodule or nested `.git`. Users can copy it out and initialize an independent Git repository.

From this repository, prepare and open either host:

```sh
# Codex
harnessadapter minimal-shop --codex

# Claude Code
harnessadapter minimal-shop --claude
```

Then send: `Run the adapter integration task in PROMPT.md.`

Python 3.11+, `pip install -e .`, and an installed, signed-in host CLI are required. The host starts the stdio MCP server, so no separate server terminal is needed. Accept the host's normal workspace trust and MCP approval prompts when shown.

To prepare without launching:

```sh
harnessadapter minimal-shop --host codex
codex -C .demo/codex

harnessadapter minimal-shop --host claude-code
cd .demo/claude && claude
```

The dedicated workspaces contain the adapter context, MCP settings, and converted native roles. The launcher preserves normal host settings and does not edit global configuration. Both hosts use a visibly labeled, replaceable adapter block in their project instructions (AGENTS.md or CLAUDE.md), preserving text outside that block. The helper still targets a generated demo workspace, not arbitrary existing-project attachment.

This is a source checkout, not a released package or adapter registry. See the [developer instructions](examples/minimal_shop/README.md#developer-checks) for automated trials and verification. The Claude Code trial verified context loading, native child memory calls, and the parent calculation using actual event and server logs.

## Design documents

- [Design discussion, 2026-09-18](docs/design-discussion-2026-09-18.md): decisions, rationale, and open questions.
- [Adapter contract v0](docs/adapter-contract-v0.md): Context, MCP tools, optional role definitions, and optional integration helpers.
- [Subagent field survey](docs/subagent-fields-2026-09-18.md): candidate extensions and configuration scopes across Codex, Claude Code, Pi, and Hermes.
- [Claude Code integration findings](docs/claude-demo-findings-2026-09-18.md): native loading, role-name conversion, linked tool calls, and test scope.
- [Minimal demo findings](docs/minimal-demo-findings-2026-09-18.md): actual MCP and native Codex subagent calls, plus integration issues found during implementation.

Repository documentation, code comments, examples, prompts, and website content are maintained in English. Historical execution records retain their original content.
