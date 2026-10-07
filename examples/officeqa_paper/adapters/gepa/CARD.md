# Adapter card: paper-gepa

- Template: `prompt-only@1.0`
- Started from: `341178ce83501a2b7ef5d0d5ab380845279a5d22`
- Evolver: `gepa`
- Task: task-info/
- Training: 4 epoch(s), 68 step(s)
- Spending: rollouts $1.3869, Evolver $7.4196; 6.48 h

## Settings that affect what can be trusted

- sandbox: {"enabled": true, "network": "restricted", "allow": ["https://api.fireworks.ai"], "read": [], "env": ["FIREWORKS_API_KEY"]}
- evolver_access: {"environment": [], "tools": [], "send_material_out": false}
- validation data given to the Evolver: True

Warnings raised at start:
- sandbox.network is restricted through a proxy (best effort): a program that ignores proxy variables can still reach the network.
- validation data is given to the Evolver with whatever it contains; if it contains answers, the Evolver may overfit to the validation split.

## What was learned

candidate 1 of 28, the best validation mean (0.708; seed 0.500)

See the final step's report: `step_0068/output/report.md` in the run records.

## Evaluation

| Adapter | Tasks | Repeats | Mean score | Solved at least once | Cost (USD) |
|---|---|---|---|---|---|
| final | 172 | 3 | 0.564 | 0.645 | 3.4533 |

Where it has not been tested: any task, model or pipeline other than the one above.
