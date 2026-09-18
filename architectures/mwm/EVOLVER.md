# Evolution guide: MetaWorld Model

This document is for the Evolver, not the acting host. The live acting context is rendered from the MWM package; ADAPTER.md documents that surface. This guide belongs to the evolvable core and does not grant permissions.

## What this adapter maintains

A world model of an external environment: scoped and dated knowledge, its grounding in worked examples, unresolved investigations, and justified executable capabilities. The original V2 default has a persistent docket and a rolling journal; it does not seed learned roles or tools.

## Structure

- memory/<topic>.md: prose knowledge with brief, scope, last_confirmed_t, derived_from, and optional open fields.
- examples/<record-address>.md: authored worked examples grounding those claims.
- tools/<name>/{TOOL.md,run.py,test.py}: learned programs with literal SCHEMA and run(**kwargs), admitted only after sandboxed tests.
- agents/<name>/SPEC.md: role trigger, scope, dates, provenance, and instructions. The framework converts these for the acting host.
- consumption/brief.md: the current briefing; host delivery applies the original cap.
- meta/cases/: unresolved investigations that persist until settled.
- meta/journal.md and its archive: session records with separate rolling behavior.
- interface.py and mwm_core/: method-specific consumption and dispatch code.
- adapter.json: the public loading contract; hosts use a single wm(name,args) tool.

## Evidence and evolution

The evidence store lives outside this adapter and is always read-only. Its README.md defines the available record format, logical addresses, timing, feedback, and access limitations. Do not assume every store has the synthetic demo's layout. Keep derived_from links resolvable through worked examples and original records.

When using the MWM Evolver, read the full method contract, agenda, and referenced skills/patterns. Repair old claims before acquiring new ones; only increase last_confirmed_t when observations support the claim. Preserve historical validity and uncertainty. Maintain the docket separately from journal entries. Update the briefing and verify every promised access path.

All core files may evolve, including this guide and the consumption implementation. Changes must preserve the declared public contract or update it coherently. User-defined environment reads and networking are supplied by framework policy. Read environment/README.md or call get_policy; this document cannot expand those permissions. An unavailable sandbox is not a passing executable admission test.
