# OfficeQA with ACE

[ACE](https://arxiv.org/abs/2510.04618) (Zhang et al., ICLR 2026; [code](https://github.com/ace-agent/ace)) on
OfficeQA in the SkillOpt paper's setting, offline: the [`ace`](../../../docs/methods/ace.md) Evolver grows a playbook
on the 50 training questions, keeps the playbook with the best score on the 24 validation questions, and the fixed
final adapter is evaluated on the 172 test questions. The adapter is SkillOpt's initial skill followed by ACE's
playbook ([`ace`](../../../hermit/adapter/templates/ace/) template). It runs in OfficeQA's
[GPT-5.6-Luna setting](../README.md#gpt-56-luna-on-a-chatgpt-login): GPT-5.6-Luna at medium effort acting through the
Codex agent on the machine's ChatGPT login (no API key), and ACE's Reflector and Curator one Codex session per call
(`llm: {cli: codex}`). The image, the egress proxy and the login are set up once as in the recipe's
[A model on a ChatGPT login](../../../docs/recipes/officeqa.md#a-model-on-a-chatgpt-login).

```bash
python examples/officeqa/ace/make_seed.py                # SkillOpt's initial skill + an empty playbook
hermit train --config examples/officeqa/ace/smoke_codex_luna.yaml   # 4 questions one by one, 2 validation, eval 4
hermit train --config examples/officeqa/ace/run_codex_luna.yaml     # 50 training questions, 2 epochs
hermit eval  --config examples/officeqa/ace/run_codex_luna.yaml --adapter final
```

## What to read after the smoke

The smoke is not a measurement; it shows whether each of ACE's levers works in a real session. In
`runs/smoke-luna-ace/`:

- `workspace/state/ace.json`, per sample: `cited` (the bullet ids the agent named; empty everywhere means the agent
  does not use the playbook's ids and the counters cannot move), `tags` / `rounds` (the Reflector's tags and the
  retry scores), `curator` (`applied` or why it was skipped) and `added`;
- the steps' `output/calls/`: every Reflector and Curator prompt and reply;
- `checkout/ADAPTER.md`: the skill unchanged at the top, the bullets under their sections, counters above zero;
- the training rollouts' Codex traces: that the agent read the skill and wrote a `bullet_ids:` line in its final
  message (Codex does not record its reasoning).

## Result

One training and its evaluations on the 172 test questions, one pass each: GPT-5.6-Luna at medium effort acting
and learning (the [recipe](../../../docs/recipes/officeqa.md#ace) has the run and its records).

| Adapter | accuracy | − the initial skill, points |
|---|---|---|
| none | 0.233 | −14.5 [−20.9, −8.1] |
| the initial skill, with ACE's empty playbook | 0.378 | |
| **`final`, best validation (79 bullets)** | **0.640** | **+26.2** [+18.6, +33.7] |
| the last playbook (106 bullets) | 0.593 | +21.5 [+13.4, +29.7] |

Paired per question, 95% bootstrap intervals. Validation scored 0.708 after both epochs, so the first epoch's playbook
is kept; on the test it is level with the last one (+4.7 [−0.6, +10.5]). `final` is [`adapter/`](adapter/), its card
in [`adapter/.hermit/CARD.md`](adapter/.hermit/CARD.md).

## Settings and differences

ACE's per-sample loop is `data.step_size: 1`: one rollout, its reflection (and up to three retry sessions for a
wrong answer), the Curator, then the next question. Two epochs; validation at each epoch's end. The Reflector sees
the judge's feedback, which names the expected answer, so this is ACE with labels, the paper's offline setting.
The full table of differences from the authors' code is on the [method page](../../../docs/methods/ace.md).
