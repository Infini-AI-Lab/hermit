# MWM adapter core — original structure

This adapter preserves the original MetaWorld V2 default layout and consumption semantics. It replaces the earlier simplified world_model.py port. All core files remain evolvable; generic MCP transport and host installation remain framework-owned.

```text
memory/                    Scoped, dated prose entries and _template.md
examples/                  Worked examples and _template.md
tools/<name>/              TOOL.md + run.py (literal SCHEMA and run) + test.py
agents/<name>/SPEC.md       Provenance-bearing role prompts
ADAPTER.md                 Host briefing (the opening block every host sees); no size cap
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

## About this adapter

This core preserves MWM's V2 default artifact structure. Its live Context is rendered by interface.py using the five-part MWM package: host framing, briefing, open questions, memory catalog, and access instructions. ADAPTER.md is the briefing itself (the host-facing text, as in every other adapter); this README documents the structure.

All host access uses one wm(name, args) tool. Learned tools remain tools/<name>/{TOOL.md,run.py,test.py}; hosts call wm(name=<SCHEMA name>, args=...). Programs are discovered statically and admitted only after their tests pass in the framework sandbox. Without that sandbox they remain unavailable, with TOOL.md as the prose fallback. No learned program is imported into the wrapper process.

Roles retain agents/<name>/SPEC.md. Only templates are seeded, not a pre-made researcher. The export bridge translates discovered specs into the framework's role contract for native Codex or Claude Code registration.

Knowledge is prose under memory/. Worked examples under examples/ link derived_from addresses to raw evidence supplied outside the core. trace uses record addresses such as q17 and the original turn-index argument `from`, not arbitrary file paths and character offsets. read uses /world-model/... paths.

The docket under meta/cases/ persists independently of journal rolling. The MWM Evolver retains the original contract, agenda, skills, patterns, and method-specific maintenance outside the editable core. All files inside this core remain evolvable under the new framework boundary.
