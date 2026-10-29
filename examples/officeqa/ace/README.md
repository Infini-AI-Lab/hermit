# OfficeQA with ACE

[ACE](https://arxiv.org/abs/2510.04618) (Zhang et al., ICLR 2026; [code](https://github.com/ace-agent/ace)) on
OfficeQA in the SkillOpt paper's setting, offline: the [`ace`](../../../docs/methods/ace.md) Evolver grows a playbook
on the 50 training questions, keeps the playbook with the best score on the 24 validation questions, and the fixed
final adapter is evaluated once on the 172 test questions. The adapter is SkillOpt's initial skill followed by ACE's
playbook ([`ace`](../../../hermit/algorithms/templates/ace/) template); the acting agent, its Codex harness and the
models (GLM 5.3 Flash acting and learning, medium effort) are those of the all-Flash GEPA and SkillOpt runs, so the
three methods compare on one row of settings.

```bash
python examples/officeqa/ace/make_seed.py                         # SkillOpt's initial skill + an empty playbook
export OFFICEQA_CODEX_IMAGE=officeqa-codex:0.162.0
hermit check --config examples/officeqa/ace/smoke_codex.yaml
hermit train --config examples/officeqa/ace/smoke_codex.yaml      # 4 questions one by one, 2 validation, then eval 4
hermit train --config examples/officeqa/ace/run_codex_flash.yaml  # 50 training questions, 2 epochs
hermit eval  --config examples/officeqa/ace/run_codex_flash.yaml --adapter final
hermit eval  --config examples/officeqa/ace/run_codex_flash.yaml --adapter seed_adapter   # the starting adapter
```

## What to read after the smoke

The smoke is not a measurement; it shows whether each of ACE's levers works in a real session. In
`runs/smoke-codex-ace/`:

- `workspace/state/ace.json`, per sample: `cited` (the bullet ids the agent named; empty everywhere means the agent
  does not use the playbook's ids and the counters cannot move), `tags` / `rounds` (the Reflector's tags and the
  retry scores), `curator` (`applied` or why it was skipped) and `added`;
- the steps' `output/calls/`: every Reflector and Curator prompt and reply;
- `checkout/ADAPTER.md`: the skill unchanged at the top, the bullets under their sections, counters above zero;
- the training rollouts' Codex traces: that the agent read the skill and named bullets in its reasoning.

## Settings and differences

ACE's per-sample loop is `data.step_size: 1`: one rollout, its reflection (and up to three retry sessions for a
wrong answer), the Curator, then the next question. Two epochs; validation at each epoch's end. The Reflector sees
the judge's feedback, which names the expected answer, so this is ACE with labels, the paper's offline setting.
The full table of differences from the authors' code is on the [method page](../../../docs/methods/ace.md).
