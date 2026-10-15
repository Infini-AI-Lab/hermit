# Evolution guide: MetaWorld Model

This document is for the Evolver, not the acting host. ADAPTER.md is the host package rendered from this world model after every adopted session; every host reads that file as it is. This guide does not grant permissions.

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
- adapter.yaml: the template's manifest; hosts use a single wm(name, args) tool.
- .mwm/edit_times.json: written by the Evolver after each session (the catalog's ordering key); not yours to edit.

## Evidence and evolution

The evidence store is mounted read-only at /evidence; its README.md defines the record format, the addresses, timing and feedback. Keep derived_from links resolvable through worked examples and original records.

When using the MWM Evolver, read the full method contract, agenda, and referenced skills/patterns. Repair old claims before acquiring new ones; only increase last_confirmed_t when observations support the claim. Preserve historical validity and uncertainty. Maintain the docket separately from journal entries. Update the briefing and verify every promised access path.

The world-model artifacts may evolve; the serving code (adapter.yaml, interface.py, check.py, mwm_core/) and this guide may not — CONTRACT.md rules 2 and 3. What this run can reach of the environment is listed in /environment/README.md; this document cannot expand it. An unavailable sandbox is not a passing executable admission test.
