# MWM adapter core — original structure

This adapter preserves the original MetaWorld V2 default layout and consumption semantics. It replaces the earlier simplified world_model.py port. All core files remain evolvable; generic MCP transport and host installation remain framework-owned.

```text
memory/                    Scoped, dated prose entries and _template.md
examples/                  Worked examples and _template.md
tools/<name>/              TOOL.md + run.py (literal SCHEMA and run) + test.py
agents/<name>/SPEC.md       Provenance-bearing role prompts
consumption/brief.md        Host briefing, served under the original cap
meta/journal.md             Rolling session record
meta/journal-archive.md     Created when old session entries are rolled
meta/cases/                 Persistent unresolved investigations
interface.py               Public-export bridge; one wm tool
mwm_core/                  MWM-specific consumption logic adapted from source
adapter.json               Framework loading contract
```

The seed contains templates, not learned claims, executable tools, or a pre-made researcher role. Native roles are discovered from SPEC.md and converted during deployment. There is no separate agents/index.json to keep synchronized.

The host receives the original five-part package. Tools remain behind wm(name,args), including learned programs. Original record addresses and turn-index trace windows are retained. MCP wraps the original textual result as an object with a text field.

Tool schemas are read by AST, never by importing learned run.py files. Admission and execution require the framework's Linux bubblewrap resource. When unavailable, learned tools are not served or executed; the host may read TOOL.md as a prose fallback. macOS sandbox-exec is not used as an unverified substitute. The adapter's own consumption code is still trusted Python loaded by the framework; this is not a claim that arbitrary core code is sandboxed.

The MWM Evolver keeps the source contract, agenda, nine pinned skills, and pattern library under harnessadapter/evolvers/mwm. Journal rolling, docket caps, memory status, and executable admission are method-specific policies. Evidence, environment bindings, immutable snapshots, controller state, and host config live outside the editable core.

```sh
harnessadapter mwm --codex
harnessadapter mwm --claude
harnessadapter evolve mwm --evidence examples/mwm_evidence --block 1 --tick 2
```

The current deployment is offline and uses native host delegation. Benchmark environment drivers, managed seat takeover, and automatic Git publication are not supplied. See docs/mwm-fidelity-correction.md in the framework for source provenance, preserved behavior, and remaining deployment differences.
