# MWM adapter

The MetaWorld Model (V2 default) as a framework template: the world model a learner maintains, and the code that
serves it to hosts through one `wm(name, args)` tool.

```text
memory/                    Scoped, dated prose entries and _template.md
examples/                  Worked examples and _template.md
tools/<name>/              TOOL.md + run.py (literal SCHEMA and run) + test.py
agents/<name>/SPEC.md      Sub-agent specs (roles), exported to the host
consumption/brief.md       Host briefing, served under the 30,000-character cap
meta/journal.md            Rolling session record (older entries collapse into meta/journal-archive.md)
meta/cases/                The docket: open investigations, never rolled
CONTRACT.md                The learner's contract, rendered by the Evolver every session
ADAPTER.md                 The host package, rendered by the Evolver after every adopted session
.mwm/edit_times.json       Per-file session times, the catalog's ordering key (written by the Evolver)
interface.py, mwm_core/    The serving code: the push package, the wm tool, the code channel
check.py                   The template check: the host package still renders
adapter.yaml               The manifest: exports, structure (learnable / fixed), check
```

The serving code is fixed by the template (`structure.fixed` in adapter.yaml); the Trainer restores it, and the MWM
Evolver discards a session that changes it (contract rule 3). Hosts receive the five-part package: framing, briefing,
open questions, memory catalog, and how to reach the model. A task's own pipeline renders it live with
`interface.context()` and the venue's host settings; a host that reads the context file gets ADAPTER.md. Learned
programs are discovered by reading their SCHEMA statically and run only in the framework's Python sandbox after their
own test passes; without it, TOOL.md is the prose fallback. Record addresses (`q17`, `h005`) reach worked examples
(`recall`) and the raw record (`trace`, when the task binds an `evidence` resource).

Train it with `template: mwm` and `evolver.name: mwm`; see `examples/mwm/` for the CL-Bench venues.
