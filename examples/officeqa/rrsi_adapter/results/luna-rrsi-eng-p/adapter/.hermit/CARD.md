# Adapter card: luna-rrsi-eng-p

- Template: `rrsi-adapter@1.0`
- Started from: `9754d7cfa5925ef7ea98b8ff28b72c8ac5d6df65`
- Evolver: `rrsi_adapter`
- Task: task-info/
- Training: 22 epoch(s), 22 step(s)
- Spending: rollouts $0.0000, Evolver $0.0000 (+3974 calls of unknown cost); 10.39 h

## Settings that affect what can be trusted

- sandbox: {"enabled": true, "enforce": "required", "network": "restricted", "allow": ["chatgpt.com"], "read": [], "packages": [], "env": [], "audit": null, "profiles": {"rollout": {"enabled": false}}}
- evolver_access: {"environment": [], "tools": [], "send_material_out": false}
- validation data given to the Evolver: False

Warnings raised at start:
- sandbox.network is restricted through a proxy (best effort): a program that ignores proxy variables can still reach the network.

## What was learned

H_T = r07A after 10 rounds (S 0.6700, S* 0.6700, H_0 0.4300; 4 accepted)

See the final step's report: `step_0022/output/report.md` in the run records.

## Evaluation

Not evaluated yet (`hermit eval`).

Where it has not been tested: any task, model or pipeline other than the one above.
