# Quickstart

Run these commands from the framework repository. Python 3.11+ and an installed, signed-in Codex or Claude Code CLI are required. First use installs framework Python dependencies into .venv-demo. Model calls use your existing account.

## Try context-only deployment

```sh
./harnessadapter deploy prompt-only --host codex
```

This creates a managed host workspace and opens Codex there. The initial context says that no instructions have been learned yet. No adapter MCP server or subagent is registered. Use --host claude-code for Claude Code, or --prepare-only to generate files without opening the host.

## Evolve from experience

```sh
./harnessadapter evolve --architecture prompt-only \
  --evidence fixtures/mwm_evidence \
  --prompt "Read the evidence and write useful, scoped instructions in ADAPTER.md." \
  --no-network
```

The bundled experience contains fictional shop observations for integration checks. Replace it with your own directory containing README.md and your records. This command calls a model and produces a separate candidate under .demo/evolution/. It does not overwrite the architecture or publish the result.

Read the reported control/report.md and control/result.json. Then deploy the candidate path shown in the result:

```sh
./harnessadapter deploy /absolute/path/to/session/adapter --host codex
```

## Try MWM

```sh
./harnessadapter evolve --architecture mwm \
  --evidence fixtures/mwm_evidence --no-network
```

Without a custom prompt, the mwm shortcut selects the bundled MWM Evolver. With --prompt, the generic prompt-driven Evolver is selected unless you explicitly pass --evolver mwm. Architecture and update algorithm are separate choices.

The MWM architecture begins with empty learned memories, tools, and native roles. Its framework-served wm tool provides the reference interface.

## Use YAML

```sh
./harnessadapter run config/evolve.yaml --dry-run
./harnessadapter run config/evolve.yaml
```

The first command prints the equivalent arguments. The second runs the same CLI workflow. Paths in YAML are relative to that YAML file.
