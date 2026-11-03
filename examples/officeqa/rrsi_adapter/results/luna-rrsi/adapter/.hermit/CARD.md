# Adapter card: luna-rrsi

- Template: `rrsi-adapter@1.0`
- Started from: `a9f6ca36c6a75e2fbc1d50cf9acb573c9df5000a`
- Evolver: `rrsi_adapter`
- Task: task-info/
- Training: 16 epoch(s), 16 step(s)
- Spending: rollouts $0.0000, Evolver $0.0000 (+3659 calls of unknown cost); 5.45 h

## Settings that affect what can be trusted

- sandbox: {"enabled": true, "enforce": "required", "network": "restricted", "allow": ["chatgpt.com"], "read": [], "packages": [], "env": [], "audit": null, "profiles": {"rollout": {"enabled": false}}}
- evolver_access: {"environment": [], "tools": [], "send_material_out": false}
- validation data given to the Evolver: False

Warnings raised at start:
- sandbox.network is restricted through a proxy (best effort): a program that ignores proxy variables can still reach the network.

## What was learned

H_T = r07A after 10 rounds (S 0.6600, S* 0.6600, H_0 0.4100; 4 accepted)

See the final step's report: `step_0016/output/report.md` in the run records.

## Evaluation

Not evaluated yet (`hermit eval`).

Where it has not been tested: any task, model or pipeline other than the one above.
