# MetaWorld Model adapter

This core preserves MWM's V2 default artifact structure. Its live Context is rendered by interface.py using the five-part MWM package: host framing, briefing, open questions, memory catalog, and access instructions. This file documents the adapter; it is not a replacement briefing.

All host access uses one wm(name, args) tool. Learned tools remain tools/<name>/{TOOL.md,run.py,test.py}; hosts call wm(name=<SCHEMA name>, args=...). Programs are discovered statically and admitted only after their tests pass in the framework sandbox. Without that sandbox they remain unavailable, with TOOL.md as the prose fallback. No learned program is imported into the wrapper process.

Roles retain agents/<name>/SPEC.md. Only templates are seeded, not a pre-made researcher. The export bridge translates discovered specs into the framework's role contract for native Codex or Claude Code registration.

Knowledge is prose under memory/. Worked examples under examples/ link derived_from addresses to raw evidence supplied outside the core. trace uses record addresses such as q17 and the original turn-index argument `from`, not arbitrary file paths and character offsets. read uses /world-model/... paths.

The docket under meta/cases/ persists independently of journal rolling. The MWM Evolver retains the original contract, agenda, skills, patterns, and method-specific maintenance outside the editable core. All files inside this core remain evolvable under the new framework boundary.
