# Animation Workshop

`animation-workshop` is an experimental architecture and Evolver for web physics animation. MWM remains available unchanged as a separate knowledge-oriented choice. The new architecture starts empty; it does not ship a hand-authored fluid solver.

## Role and contents

The adapter gives a fresh actor reusable making capabilities it can select, inspect, compose and adapt. `methods/` contains decisions and tradeoffs, `implementations/` contains cohesive source with explicit scene boundaries, and `examples/` demonstrates integration and adaptation. `index.json` advertises these entries; `briefing.md` routes by capability and applicability rather than the most recent task; `preferences.md` records scoped preferences. These categories do not prescribe a schema for every document.

The `workshop` tool lists, searches and reads artifacts. Source reads return `next_offset`, `eof` and SHA-256; callers may read all pages and verify that a revision has not changed. The actor copies/adapts code inside its own task workspace. Published animations must contain their own runtime logic and must not call MCP during playback. Additional callable tools may be declared through the normal adapter contract when useful.

## Evolution

Select the architecture and Evolver explicitly:

```sh
harnessadapter evolve --architecture animation-workshop --evolver animation-workshop \
  --learning-material ./learning-material --output ./runs
```

The transfer-oriented Evolver diagnoses the capability behind an observed difficulty, separates reusable behavior from scene-specific geometry and presentation, builds concrete help, and checks adaptation. A current task is a learning case, not the identity of the adapter. Complete scenes remain useful examples, but copying the newest scene is not the default workflow. The method requires neither a universal engine/API nor the MWM memory schema, executable-admission workflow or open-question docket.

LearningMaterial should preserve the exact task, source, trajectory, exported context, real adapter call logs, captures with timing, checks, and feedback provenance. Distinguish actual user feedback, AI reviewer feedback, numeric measurements and self-assessment. Successful calls and source reads are usage evidence, not causal contribution scores.

## Optional preview binding

The SDK supports `evolve(..., preview_enabled=True)` **only when the caller supplies a renderer**. The renderer watches `control/preview_jobs/<id>/status.json`, renders the snapshotted `index.html` and writes observations under `observations/<id>/`. It atomically updates status to complete or error. The normal CLI does not bind a renderer and rejects preview requests.

`request_preview` snapshots one self-contained HTML file from adapter/scratch under the OS sandbox (1 MB limit, two requests per evolution). Companion local files are not included. `get_preview` exposes status and observation paths; `read_image(area="observations", ...)` displays returned frames. Generated observations are read-only to the learner. A renderer must honor the declared network policy and serve only the selected snapshot, never the session root. Browser observations are diagnostic inputs, not an independent quality score. This is an initial caller-driven preview contract, not a bundled browser daemon.

## Evaluation discipline

Freeze the adapter for each fresh generation, keep the original task unchanged, preserve every output, and review both improvements and regressions. Log source acquisition separately from demonstrated integration/execution. Self-generated tests cannot prove visual fidelity or transfer; transfer still needs another scene. Keep all campaign artifacts outside this source repository.


## Capability boundaries and reuse checks

A useful implementation states what it owns (such as evolving state), what comes from the scene (such as surface queries or configuration), and which properties should be checked after an actor changes it. A callback, parameter object or documented source-edit boundary can be enough. Keep an integrated mechanism usable; splitting it into unrelated helpers does not make it transferable.

For example, a contact-and-detachment capability might separate surface geometry from state transitions and rendering. A planar scene and a scene with several faces would exercise that boundary differently. This is an illustrative boundary, not a prescribed fluid solver or proof that an existing example supports both. Other animation families need their own appropriate abstractions and checks.

Within the run budget, the Evolver checks the observed failure and one small structurally different diagnostic case. It identifies shared code, changed scene bindings, and any core rewrites. Changing only color, speed or camera does not establish this kind of reuse. Self-authored cases are visible development probes, not held-out evaluation. Missing or failed checks must be recorded; the method may narrow scope, retain an experimental capability, or make no change.

When two visual previews are available, plan their use for the observed case and the structural variation using the final intended capability where feasible. Repairing a failed first trial can take priority over the second case. Report which exact revision was rendered and which checks were skipped; do not label an unrendered modification visually validated.

When continuing an older adapter, the Evolver should move latest-task prescriptions out of its global briefing, retain scoped examples and preferences, and incrementally extract useful capabilities. This revision changes future instructions and the empty architecture's guides. It does not modify historical adapter instances, run a new experiment, or establish improved transfer performance by itself. Runtime/MCP contracts and catalog required fields are unchanged.
