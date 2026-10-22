# MWM Evolver

The native MetaWorld Model learner (V2 default: docket on, hypothesis off) on the Trainer, for `template: mwm`.
`evolver.py` documents the step in full; in short:

- One training step is one block (`data.step_size` episodes, `data.shuffle: false`). The first step also runs the
  native bootstrap session; with `block_size` set, a shorter final block gets no session.
- Before the session: maintenance (`maintenance/`: journal roll, docket caps, gate advice) and the contract
  (`materials/core.md` + `agenda.md` + the venue's slots from the `deployment` directory (`params.deployment`, default `task-info/mwm/`; the method's own slots are in `deployment.py`)), by `prompt.py`.
- The session: one Codex run with the native prompt, in the adapter directory, with `/evidence`, `/environment`,
  `/skills`, `/patterns` and `/world-model` mounted (`mounts.py`), the Trainer's other areas hidden, in its own network
  namespace. Only the model meter and a gate onto the venue's channels are forwarded (`_ToolGate`); every session
  measures that first and does not start if it fails. `bin/captest` is the native tool-test command.
- After it: adopted if it exited cleanly, took an action, and left the serving code alone; then every tool's own
  test runs and failures are parked in `tools/_rejected/`. Otherwise the adapter is restored; a session that took no
  action is retried once.
- The task protocol: `RunOutput.extra["mwm"] = {unit, number, entry}` per episode, and an evidence store in the
  native layout passed as `params.evidence` (granted through `evolver_access.environment`).

`materials/` holds the original contract, agenda, nine skills and pattern library; `source-map.json` their upstream
provenance. Parameters and defaults are in `evolver.yaml`.
