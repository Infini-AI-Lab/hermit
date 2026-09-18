# MWM fidelity correction

The initial harnessadapter MWM demo was a simplified rewrite. Its successful transport test did not establish a faithful MWM port. This correction preserves the original V2 default representation and consumption semantics while moving generic transport and controller state outside the core.

## Preserved structure

```text
memory/<topic>.md                         Scoped, dated prose
examples/<record-address>.md              Worked examples grounding claims
tools/<name>/TOOL.md                      Trigger, provenance, repair history, fallback
tools/<name>/run.py                       Literal SCHEMA + run(**kwargs)
tools/<name>/test.py                      Admission test
agents/<name>/SPEC.md                     Role trigger, scope, dates, provenance, prompt
consumption/brief.md                      Original capped briefing
meta/journal.md                          Recent session records
meta/journal-archive.md                   Older complete records, created on rolling
meta/cases/                              Persistent open investigations
```

Only templates are initialized. There are no pre-seeded learned tools or researcher roles. The original seed texts are reused. The V2 default is docket on, hypothesis off; this correction does not make the older hypothesis variant the default.

## Preserved behavior

- The five-section PUSH package: host framing, briefing, open questions, memory catalog, and access instructions.
- One PULL interface, wm(name,args). Learned tool names stay inside wm; no automatic top-level return_check MCP tool is added.
- Learned schemas are read statically. A tool is served only after its test passes in the execution sandbox. Failed programs use their prose fallback; execution never silently falls back to the host process.
- Native role exports are derived from SPEC.md without replacing its source representation with a JSON role index. Registration is performed by the host connector. Old generated roles are archived outside discovery when removed.
- Recall addresses resolve worked examples and point to raw records. Trace uses the original logical-address lookup and turn-based `from` window, including padded record filenames.
- Briefing limits, memory catalog order and cap, last-confirmed stamps, and open-question compilation reuse the original method code.
- Journal rolling keeps the last five sessions in full; older records are archived. The docket remains separate, with the original caps and session status.
- The Evolver receives the original core contract, agenda, nine pinned meta_skills_v110 skills, and pattern library. Its short METHOD.md describes the transport adaptation, not a replacement learning algorithm.

## Ownership adaptation

The adapter owns its representation and consumption implementation (`interface.py` and `mwm_core/`) as editable core. The framework owns the MCP protocol, host configuration, explicit resource bindings, and file capabilities. MWM-specific maintenance remains in evolvers/mwm/maintenance rather than becoming a universal framework policy.

Original CONTRACT.md and learner skills are supplied in a read-only method area. Original evidence, environment capability descriptions, controller state, and immutable history snapshots stay outside the core. All core files remain editable. The generated contract explicitly replaces the old write-scope paragraph with this boundary.

Snapshots preserve the source and pre-maintenance state. This implementation does not claim automatic Git commits or original benchmark release behavior. The learner can inspect its prior core through the read-only history area.

## Execution availability

The supplied execution primitive uses Linux bubblewrap with a read-only adapter mount and no network. This Mac does not have that backend. Its actual behavior is therefore: no learned executable admission, no learned-tool execution, and no wm run. There is no unsandboxed or sandbox-exec substitute.

The adapter's own Python consumption implementation is still trusted code loaded by the framework. The learned-tool admission gate is not a claim that arbitrary core code is OS-isolated. An additional deployment isolation layer remains necessary for untrusted core code.

The offline reference has no active environment probes, benchmark evaluator, or managed seat-takeover backend. Native role constraints follow the supported host's real capabilities. The copied original skills describe multiple source deployment modes; this run's environment and transport documents identify which modes are available.

## Verification

The regression suite checks seed layout, an empty initial role set, one wm export, five-section context ordering, native SPEC conversion and retirement, journal/docket maintenance, original trace addressing, and admission-before-invocation. Admission success/failure policy tests use a stub execution resource; they do not claim a live bubblewrap test on this Mac.

Source: `/Users/haizhong/code/metaworld`, commit `22b67ea64eb86e68b7947f09f940ecee66b967c1`. See evolvers/mwm/source-map.json for source hashes. Original source files and benchmark data were read only. The earlier demo and its raw run logs remain preserved, explicitly superseded as evidence about MWM fidelity.

## Corrected integration run

A real Claude Code learning session completed at `.demo/evolution/mwm-skanm7vz/`. Its tool log confirms reads of the rendered contract, environment README, author-knowledge, curate-brief, keep-journal, design-representation, audit-consumption-path, decide-executable, and patterns/DECIDE.md.

The candidate changed six artifacts: two worked examples, one dated prose memory, one persistent docket case, the briefing, and journal. It retained both observed policies and left boundary/rounding/persistence questions open. No learned tool or role was invented. Source, evidence, framework, method, environment, and history fingerprints stayed unchanged during learning.

The real MCP check exposed only wm, read the generated memory, followed recall to its raw record, and resolved q2 through the original trace interface. This is a synthetic integration check, not a benchmark score. The candidate is not automatically activated. Reported API-cost accounting was USD 1.14861275, not necessarily an incremental subscription charge.

Sixteen regression tests passed. Live Linux sandbox execution was not tested on this Mac; success/failure admission policy was tested with an explicit stub backend, and the actual local deployment fails closed when the required backend is absent.
