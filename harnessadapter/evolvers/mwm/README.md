# MWM Evolver — original method materials

This method preserves the source MWM V2 default: docket on, hypothesis off. It supplies the original contract, agenda, meta_skills_v110, and pattern library. METHOD.md only explains the adaptation to framework file tools and external ownership boundaries.

- `materials/core.md`, `materials/agenda.md`: original source texts; rendered into the session's read-only CONTRACT.md.
- `materials/skills/`: nine original pinned skills and their references.
- `materials/patterns/`: the original mechanism library.
- `maintenance/`: adapted original journal and docket maintenance.
- `prompt.py`: renders deployment slots, performs method-specific maintenance, and constructs the memory index and session status.
- `source-map.json`: upstream commit and copied-file provenance.

The framework supplies the selected Codex or Claude Code learner backend, explicit read/write capabilities, evidence snapshots, control logs, and candidate delivery. Method/evidence/environment/history are read-only through the learner tools; adapter and scratch are writable. Every core file can evolve, but MWM's default artifact organization and admission discipline should be preserved unless evidence justifies a recorded method change.

```sh
harnessadapter evolve mwm --evidence examples/mwm_evidence --block 1 --tick 2
```

`--prepare-only` builds a reviewable session without calling a model. Block and tick are supplied by the caller rather than computed from benchmark outcomes. The learner starts by reading the rendered contract and the environment/evidence READMEs.

Executable tests use test_python, which requires Linux bubblewrap. Without it, verification is explicitly unavailable and programs cannot be admitted. The default environment is offline. Snapshots replace automatic Git publication; candidates are not activated. See docs/mwm-fidelity-correction.md for retained semantics and remaining differences.

Additional environment reads and network access are configured by the user and enforced by the framework. Evidence access is intrinsic and always read-only. See docs/execution-policy.md. The method does not grant itself additional permissions.
