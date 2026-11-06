# Adapter card: luna-opro

- Template: `prompt-only@1.0`
- Started from: `3dc113889b760d0d9c0bc21b963fe8a9fd543920`
- Evolver: `opro`
- Task: ../utils/task-info/
- Training: 3 epoch(s), 3 step(s)
- Spending: rollouts $0.0000, Evolver $0.0000 (+835 calls of unknown cost); 2.41 h

## Settings that affect what can be trusted

- sandbox: {"enabled": true, "enforce": "best_effort", "network": "restricted", "allow": [], "read": [], "packages": [], "env": [], "audit": null, "profiles": {"rollout": {"enabled": false}}}
- evolver_access: {"environment": [], "tools": [], "send_material_out": false}
- validation data given to the Evolver: True

Warnings raised at start:
- sandbox.network is restricted through a proxy (best effort): a program that ignores proxy variables can still reach the network.
- validation data is given to the Evolver with whatever it contains; if it contains answers, the Evolver may overfit to the validation split.

## What was learned

candidate 2 of 13, the best validation mean of the top 3 on training (11: 0.542, 2: 0.625, 3: 0.542)

See the final step's report: `step_0003/output/report.md` in the run records.

## Evaluation

| Adapter | Tasks | Repeats | Mean score | Solved at least once | Cost (USD) |
|---|---|---|---|---|---|
| final | 172 | 1 | 0.436 | 0.436 | unknown |

Where it has not been tested: any task, model or pipeline other than the one above.
