# Adapter card: luna-ace

- Template: `ace@1.0`
- Started from: `0cf6d3ec6911d9682df1309ca98c1bccd9234b73`
- Evolver: `ace`
- Task: ../utils/task-info/
- Training: 2 epoch(s), 100 step(s)
- Spending: rollouts $0.0000, Evolver $0.0000 (+386 calls of unknown cost); 1.45 h

## Settings that affect what can be trusted

- sandbox: {"enabled": true, "enforce": "best_effort", "network": "restricted", "allow": [], "read": [], "packages": [], "env": [], "audit": null, "profiles": {"rollout": {"enabled": false}}}
- evolver_access: {"environment": [], "tools": [], "send_material_out": false}
- validation data given to the Evolver: True

Warnings raised at start:
- sandbox.network is restricted through a proxy (best effort): a program that ignores proxy variables can still reach the network.
- validation data is given to the Evolver with whatever it contains; if it contains answers, the Evolver may overfit to the validation split.

## What was learned

the best validation playbook (epoch 1 sample 50, validation 0.708; 2 validation(s))

See the final step's report: `step_0100/output/report.md` in the run records.

## Evaluation

| Adapter | Tasks | Repeats | Mean score | Solved at least once | Cost (USD) |
|---|---|---|---|---|---|
| final | 172 | 1 | 0.640 | 0.640 | unknown |

Where it has not been tested: any task, model or pipeline other than the one above.
