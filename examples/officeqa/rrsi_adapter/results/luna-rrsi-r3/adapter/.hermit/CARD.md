# Adapter card: luna-rrsi-r3

- Template: `rrsi-adapter@1.0`
- Started from: `519b15840b73a789793e6cb99515d5589db02f0e`
- Evolver: `rrsi_adapter`
- Task: task-info/
- Training: 21 epoch(s), 21 step(s)
- Spending: rollouts $0.0000, Evolver $0.0000 (+3871 calls of unknown cost); 6.51 h

## Settings that affect what can be trusted

- sandbox: {"enabled": true, "enforce": "required", "network": "restricted", "allow": ["chatgpt.com"], "read": [], "packages": [], "env": [], "audit": null, "profiles": {"rollout": {"enabled": false}}}
- evolver_access: {"environment": [], "tools": [], "send_material_out": false}
- validation data given to the Evolver: False

Warnings raised at start:
- sandbox.network is restricted through a proxy (best effort): a program that ignores proxy variables can still reach the network.

## What was learned

H_T = r03A after 10 rounds (S 0.6700, S* 0.6700, H_0 0.4100; 3 accepted)

See the final step's report: `step_0021/output/report.md` in the run records.

## Evaluation

Not evaluated yet (`hermit eval`).

Where it has not been tested: any task, model or pipeline other than the one above.
