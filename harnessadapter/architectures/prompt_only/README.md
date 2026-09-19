# Prompt-only Architecture

An initial adapter structure with no learned task knowledge. The acting export
is ADAPTER.md; tools and native roles are absent. The framework does not register
or start an adapter MCP server for this architecture.

From the framework root, evolve a separate candidate:

```sh
harnessadapter evolve --architecture prompt-only \
  --evidence examples/mwm_evidence \
  --prompt "Read the evidence and write useful, scoped instructions in ADAPTER.md."
```

The result reports a candidate directory. Deploy that directory:

```sh
harnessadapter deploy /path/from/result/adapter --host codex
```

For an empty-context integration check, deploy the built-in architecture directly:

```sh
harnessadapter deploy prompt-only --host claude-code
```

The built-in source is never overwritten by evolution. No optimizer-specific
algorithm or validation procedure is implied by the architecture.
