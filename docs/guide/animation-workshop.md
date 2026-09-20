# Animation Workshop

`animation-workshop` is an experimental architecture and Evolver for web physics animation. MWM remains available unchanged as a separate knowledge-oriented choice. The new architecture starts empty; it does not ship a hand-authored fluid solver.

## Role and contents

The adapter gives a fresh actor an implementation starting point it can inspect and adapt. `methods/` contains decisions and tradeoffs, `implementations/` contains source, and `examples/` demonstrates integration. `index.json` advertises these entries; `briefing.md` supplies concise guidance; `preferences.md` records scoped preferences. These categories do not prescribe a schema for every document.

The `workshop` tool lists, searches and reads artifacts. Source reads return `next_offset`, `eof` and SHA-256; callers may read all pages and verify that a revision has not changed. The actor copies/adapts code inside its own task workspace. Published animations must contain their own runtime logic and must not call MCP during playback. Additional callable tools may be declared through the normal adapter contract when useful.

## Evolution

Select the architecture and Evolver explicitly:

```sh
harnessadapter evolve --architecture animation-workshop --evolver animation-workshop \
  --learning-material ./learning-material --output ./runs
```

The Evolver diagnoses the largest implementation difficulty, chooses an adequate representation, builds concrete help, tests it, and records limitations. It may retain complete small examples. It does not require the MWM memory schema, executable-admission workflow or open-question docket.

LearningMaterial should preserve the exact task, source, trajectory, exported context, real adapter call logs, captures with timing, checks, and feedback provenance. Distinguish actual user feedback, AI reviewer feedback, numeric measurements and self-assessment. Successful calls and source reads are usage evidence, not causal contribution scores.

## Optional preview binding

The SDK supports `evolve(..., preview_enabled=True)` **only when the caller supplies a renderer**. The renderer watches `control/preview_jobs/<id>/status.json`, renders the snapshotted `index.html` and writes observations under `observations/<id>/`. It atomically updates status to complete or error. The normal CLI does not bind a renderer and rejects preview requests.

`request_preview` snapshots one self-contained HTML file from adapter/scratch under the OS sandbox (1 MB limit, two requests per evolution). Companion local files are not included. `get_preview` exposes status and observation paths; `read_image(area="observations", ...)` displays returned frames. Generated observations are read-only to the learner. A renderer must honor the declared network policy and serve only the selected snapshot, never the session root. Browser observations are diagnostic inputs, not an independent quality score. This is an initial caller-driven preview contract, not a bundled browser daemon.

## Evaluation discipline

Freeze the adapter for each fresh generation, keep the original task unchanged, preserve every output, and review both improvements and regressions. Log source acquisition separately from demonstrated integration/execution. Self-generated tests cannot prove visual fidelity or transfer; transfer still needs another scene. Keep all campaign artifacts outside this source repository.
