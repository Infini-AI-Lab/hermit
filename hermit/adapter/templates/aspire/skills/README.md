# Shared Robot Skills

| Skill | Purpose |
|---|---|
| [localize.md](localize.md) | Segmentation prompts, disambiguation, and 3D localization |
| [grasp.md](grasp.md) | Grasp selection, orientation, verification, and pick sequences |
| [transport.md](transport.md) | Waypoints, collision-free transit, and placement |
| [manipulation.md](manipulation.md) | Pushing, wiping, insertion, bimanual coordination, and other contact tasks |

Every entry is `## <title>` with **Failure signature.**, **When to apply.**, a repair (prose and, when useful, one
```python snippet that runs under the program rules), and **Provenance.** (origin task, development seeds,
verifier result, held-out result).
