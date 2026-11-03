# Adapter card: luna-mh

- Template: `meta-harness-adapter@1.0`
- Started from: `c9f07eb0dff2dfb7b30cfecd5263bbb88a1099c5`
- Evolver: `meta_harness_adapter`
- Task: task-info/
- Training: 13 epoch(s), 13 step(s)
- Spending: rollouts $0.0000, Evolver $0.0000 (+656 calls of unknown cost); 0.89 h

## Settings that affect what can be trusted

- sandbox: {"enabled": true, "enforce": "required", "network": "restricted", "allow": ["chatgpt.com"], "read": ["../utils/documents"], "packages": [], "env": [], "audit": null, "profiles": {"rollout": {"enabled": false}}}
- evolver_access: {"environment": [], "tools": [], "send_material_out": false}
- validation data given to the Evolver: False

Warnings raised at start:
- sandbox.network is restricted through a proxy (best effort): a program that ignores proxy variables can still reach the network.

## What was learned

r06_source_cell_guard, the best mean score on the search set (0.680; seed 0.380; 13 evaluated)

See the final step's report: `step_0013/output/report.md` in the run records.

## Evaluation

Not evaluated yet (`hermit eval`).

Where it has not been tested: any task, model or pipeline other than the one above.
