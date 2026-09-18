# Core-only adapters and framework-owned evolution

Status: executable Python-callable prototype, updated 2026-09-18. This document supersedes the earlier idea that an adapter ships its own MCP server.

## Ownership

A Harness Adapter is the entire evolvable core repo. Its prompts, knowledge, code, role definitions, context renderer, capability declarations, and internal organization may all change. A candidate is acceptable because it satisfies the external contract, not because particular internal files stayed unchanged.

The framework owns loading, MCP wrapping, invocation logging, resource binding, host-specific configuration, and candidate lifecycle. These files live outside the adapter. Users and Evolvers do not implement another server or connector for each method.

| Component | Location in this prototype | Evolver access |
|---|---|---|
| Adapter core | `examples/minimal_adapter/` or `examples/mwm_adapter/` | Read/write in an independent candidate copy |
| Method algorithm | `evolvers/mwm/` | Supplies the method prompt and memory index; not part of the candidate |
| MCP wrapper and loader | `framework/adapter.py`, `framework/mcp_server.py` | No learner edit capability |
| Host conversion | `tools/prepare_codex_demo.py`, `tools/prepare_claude_demo.py` | No learner edit capability |
| Original experience | Explicit external input, copied into a session snapshot | Read-only through learner tools |
| Run status, audit, baseline fingerprints | Session `control/` | Not exposed through learner file tools |
| Temporary work | Session `scratch/` | Read/write, not delivered |

## Callable contract

`adapter.json` currently declares the contract version, adapter identity and revision label, Context, optional roles, and tools. This filename is a prototype convention; the declaration itself is editable. A valid replacement must still satisfy the framework contract.

Each tool declares a name, description, Python `file.py:function` entrypoint, object input/output JSON Schemas, and optional MCP annotations. The function is ordinary Python: no MCP imports, decorators, registration calls, or server entrypoint. New declared functions automatically appear in MCP tools/list when a new runtime starts.

Context may be a Markdown path or an adapter-owned Python renderer returning Markdown. MWM uses the latter to compile its briefing, open questions, and memory catalog. This representation logic belongs to MWM, not the shared framework.

The initial runtime supports synchronous or asynchronous Python callables and JSON object results. It binds optional experience and a sandbox execution resource through a reserved `_resources` parameter. Optional role exporters let methods retain their native role storage, including MWM SPEC.md files. No claim is made about arbitrary language runtimes, dependency installation requested by the learner, live environment probes, or model-resource services.

## Evolution session

Codex and Claude Code can execute the MWM method through a common framework-controlled capability surface. The candidate adapter is fully writable; experience is always readable and read-only. Additional environment paths or all reads are user-defined. Network is enabled by default and can be disabled for tool/command traffic while retaining model API transport.

```text
session/
  adapter/      editable candidate; all core files may change
  evidence/       original-format observation snapshot; always learner read-only
  method/       immutable method materials
  environment/  immutable capability description
  history/      immutable previous-core snapshot
  scratch/      temporary learner files; not published
  control/      immutable policy, audits, preflight, result
  host/         meta-harness working directory, separate from the core
```

The framework prepares the candidate and method, freezes the execution policy, runs actual OS permission probes, starts the selected client, and collects the candidate. File operations and run_command go through the same sandbox boundary; reads outside the intrinsic areas require user grants. The broker refuses experience writes and policy changes. Unsupported enforcement blocks startup.

This is a tool-operation boundary, not a sandbox around model authentication and transport. The shared executor currently uses the installed Codex sandbox CLI for both clients. OS/interpreter files required by tools are read-only infrastructure. Network access does not imply remote services are read-only. See [execution-policy.md](execution-policy.md) for the exact contract and limitations.

MWM production-tool admission is separately stricter: test.py and learned run.py require Linux bubblewrap and remain unavailable here when that backend is absent. General sandboxed exploration is now available on this Mac through the common policy executor.

## MWM reference port

**Correction:** The initial simplified port has been replaced with the original MWM structure, single wm dispatch, full method materials, journal/docket maintenance, and sandbox-dependent tool admission. See [the fidelity correction](mwm-fidelity-correction.md). The earlier integration results apply to the superseded prototype, not this corrected implementation.


The original implementation was read from `/Users/haizhong/code/metaworld`, commit `22b67ea64eb86e68b7947f09f940ecee66b967c1`. Relevant sources: `mwm/wm_iface.py`, `mwm/wm_tools.py`, `mwm/session.py`, and `mwm/contract_texts/{core,agenda}.md`.

Retained method ideas: briefing plus catalog, on-demand memory and worked examples, original-experience access, scoped and dated claims, competing interpretations, repair before acquisition, executable admission discipline, journal, and consumption-path audit.

Adapted boundaries: all core files can evolve; the learner contract and raw experience are outside the core. There are no wrapper-owned immutable control files mixed into the candidate repo. The method now retains original tool bundles, SPEC.md roles, docket and journal maintenance, and single-wm routing. The new framework supplies MCP and host conversion.

This is a runnable reference port, not a reproduction of the complete research stack. Venue adapters, original benchmark scoring, Linux execution sandboxes, managed delegation, Harbor/Codex learner backends, active environment exploration, and the old Git publication machinery are not ported. Native acting roles use the existing host connectors. Revisions currently use content fingerprints and candidate snapshots, not automatic Git commits.

## Commands

```sh
# Use the initial MWM core in either acting host
./harnessadapter mwm --codex
./harnessadapter mwm --claude

# Run an actual MWM learning session against the synthetic fixture
./harnessadapter evolve mwm --evidence fixtures/mwm_evidence

# Inspect a prepared learning session without calling a model
./harnessadapter evolve mwm --evidence fixtures/mwm_evidence --prepare-only

# After reviewing the reported candidate, prepare a fresh acting workspace
./harnessadapter /absolute/path/to/session/adapter --host codex \
  --workspace /absolute/path/to/new-host-workspace \
  --evidence /absolute/path/to/session/evidence
```

The fixture is fabricated and explicitly supplies policy observations. It verifies the software loop and temporal knowledge updates, not hidden-policy discovery or performance gains. Running the Evolver invokes the configured Claude model; the default timeout is 240 seconds and the reported API-cost budget is USD 3.

## Optional Training State: agreed protocol, not yet implemented

Each iteration accepts an Adapter, Experience, Configuration, and optional Training State. It returns a candidate Adapter and optional updated Training State. A stateless Evolver returns None for the latter.

Training State carries algorithm-specific search history or progress, analogous to optimizer state. It is separate from the Evolver definition and from the Adapter's knowledge/runtime state. Deployment consumes the Adapter alone. Resuming optimization may also consume compatible Training State.

The framework should provide storage and isolation while the Evolver defines the state format. Candidate activation and state retention are separate decisions. The current implementation returns candidates and run records, not a general Training State checkpoint; state persistence, resume, and the corresponding permission boundary remain to be implemented.
