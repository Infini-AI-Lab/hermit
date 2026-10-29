# Run records and recovery

A run is identified by its config's `trainer.run_name` and output directory. Relative configuration paths resolve
against the config file's directory. A shared adapter tree stores versions for runs from the same template.

## Find the evidence

| Path | Use |
|---|---|
| `<output_dir>/<run_name>/steps.jsonl` | Completed steps, rollout scores, learner status, usage, and commit references |
| `<run>/learning_material/step_NNNN/` | Inputs, outputs, feedback, and available trajectories supplied to the learner |
| `<run>/step_NNNN/` | Saved step records and learner-state snapshots |
| `<run>/checkout/ADAPTER.md` | Current or finalized instructions |
| `<run>/checkout/.hermit/CARD.md` | Provenance and recorded final evaluation |
| `<run>/summary.json` | Final selection, stop reason, and checkpoints |
| `<run>/eval/<selector>/` | Separate evaluation summaries and per-attempt results |
| `<output_dir>/trees/<template-id>/` | Default Git tree location; configurable with `trainer.tree_dir` |

For the Quick Start:

```bash
cat .demo/first-training/first-training/steps.jsonl
cat .demo/first-training/first-training/summary.json
git -C .demo/first-training/trees/prompt-only log --all --oneline -8
```

The step score comes from rollouts with the pre-update adapter. Check the learner status, resulting commit, and
later held-out evaluation to understand an update. Read the step's learning material to verify exactly what
feedback the learner received.

## Watch a running experiment

Using the real-model walkthrough's paths:

```bash
tail -f .demo/first-model/runs/first-model/steps.jsonl
```

Each line appears **after a complete step**, not after every token or individual rollout. Press Ctrl-C to stop
following the file. It may not exist during startup or while the first batch/learner call is still running.
Use the training terminal or its redirected process log to inspect that interval.

After step 1 completes:

```bash
cat .demo/first-model/runs/first-model/learning_material/step_0001/tasks/train-001.json
cat .demo/first-model/runs/first-model/step_0001/output/report.md
git -C .demo/first-model/runs/trees/prompt-only log --all --oneline -8
```

For an accepted update, compare its pre-update parent and resulting commit directly from its record:

```bash
python -c 'import json, subprocess; from pathlib import Path; r=json.loads(Path(".demo/first-model/runs/first-model/steps.jsonl").read_text().splitlines()[0]); subprocess.run(["git", "-C", ".demo/first-model/runs/trees/prompt-only", "diff", r["parent"], r["commit"], "--", "ADAPTER.md"], check=True)'
```

For multi-file adapters, remove the trailing file filter to see every changed path.

## Read a step record

A reduced version of the offline tutorial's actual first-step record, with unrelated fields omitted:

```json
{
  "step": 1,
  "epoch": 1,
  "tasks": ["train-1"],
  "rollout": {"mean_score": 0.0, "scores": {"train-1": 0.0}},
  "evolver": {"status": "done", "summary": "learned uppercase output rule"},
  "status": "done",
  "changed": true,
  "check": {"passed": true, "issues": []},
  "repaired": []
}
```

The rollout score describes the adapter before the update. `changed` reports whether the resulting commit differs
from `parent`; inspect `check` and `repaired` to understand validation and repair.
`evolver.status: done` by itself does not assert that files changed or that performance improved.

| Observation | Interpretation / next action |
|---|---|
| `done`, `changed: false` | A valid no-op, skipped reflection, rejected internal candidate, or unusable rewrite can explain it; read the method report |
| `failed` or `out_of_budget` | The previous adapter is kept; inspect errors and usage before assuming the whole run stopped |
| `repaired` is nonempty | The Trainer corrected invalid changes; inspect the final accepted diff rather than only the proposed edit |
| No new step line yet | Could be active work or an incomplete step; check the process output and learner logs |
| `summary.json` appears | The run reached finalization; inspect `stop_reason` and `finalize`, not just the presence of a final tag |

`config.yaml` and setup files appear at run preparation. Step output/state snapshots appear at completed
boundaries. `summary.json` and the final card are completion artifacts; `eval/` appears only when evaluation is
run separately. A budget stop may occur before the next step is committed, so the last line in `steps.jsonl`
does not necessarily account for all in-flight work. The final summary reports the stop reason and spending
known to that process; a resumed process restores accounting from completed records.

## Resume an interrupted run

```bash
hermit train --config run.yaml
```

The default `trainer.resume_mode: auto` restores an unfinished run from its last completed step, including adapter,
learner state, recorded progress, and budget accounting. Work after that boundary can be repeated; external
pipeline effects and provider charges are not undone.

Keep the config, data, code, model settings, and external environment compatible with the original run. Recovery
does not recreate a benchmark service's external state unless the task integration does so.

A completed run has `summary.json` and refuses another `train` with the same name. Choose a new run name.
`trainer.resume_mode=disable` instead starts over after renaming the previous run directory and branch; use it
only when a restart is intended.

## Branch an experiment from an adapter

```bash
hermit train --config run.yaml trainer.run_name=experiment-02 start_from=experiment-01/final
```

The referenced tag must exist in the configured template tree. A compatible adapter directory also works.
`start_from` initializes adapter files for a new run; it does not restore the previous learner state.
Selecting a recovery step/tag through `resume_mode` is not implemented.

## Distinguish three kinds of persistence

| Mechanism | What it preserves |
|---|---|
| Trainer crash recovery | Last completed step's adapter, progress, state, and accounting |
| Evolver `state/` | Method-owned search candidates, experience, or other serialized state |
| `evolver.resume` | A learner host session handle, only when the method supports it |

Python instance attributes alone do not survive a process restart. See
[Trainer lifecycle](../framework/trainer.md#lifecycle-and-state) and
[Evolver state](../framework/evolver.md#state-and-session-lifetime) for the precise contracts.
