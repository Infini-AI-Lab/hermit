# OfficeQA with OPRO

[OPRO](https://arxiv.org/abs/2309.03409) (Yang et al., ICLR 2024; [code](https://github.com/google-deepmind/opro)) on
OfficeQA, train / test: the [`opro`](../../../docs/methods/opro.md) Evolver searches instructions on the 50 training
questions, keeps the best validation mean of the top 3 on training (24 validation questions), and the fixed final
adapter is evaluated on the 172 test questions. The adapter is SkillOpt's initial skill as one `ADAPTER.md`
([`prompt-only`](../../../hermit/adapter/templates/prompt_only/) template). It runs in OfficeQA's
[GPT-5.6-Luna setting](../README.md#gpt-56-luna-on-a-chatgpt-login): GPT-5.6-Luna at medium effort acting through the
Codex agent on the machine's ChatGPT login (no API key), and the optimizer one Codex session per call
(`llm: {cli: codex}`). The image, the egress proxy and the login are set up once as in the recipe's
[A model on a ChatGPT login](../../../docs/recipes/officeqa.md#a-model-on-a-chatgpt-login).

```bash
hermit train --config examples/officeqa/opro/smoke_codex_luna.yaml   # 4 training questions, 2 steps of 2 candidates
hermit train --config examples/officeqa/opro/run_codex_luna.yaml     # 50 training questions, 3 steps of 4 candidates
hermit eval  --config examples/officeqa/opro/run_codex_luna.yaml --adapter final
```

`data.step_size` is the training split's size: each OPRO step scores every new instruction on every training
question, and the first step refuses a smaller one.

## What to read after the smoke

In `runs/smoke-luna-opro/`:

- each step's `output/meta_prompt.md`: the scored instructions in ascending order, then the training problems (the
  question alone, `question_field: question`) with the agent's answer or the judge's feedback;
- `output/reply.*.txt` and `workspace/state/opro.json`: each reply's `<INS>` instruction, its per-task training
  scores, and what was skipped and why;
- `checkout/ADAPTER.md`: the instruction `finalize` chose, and the reason in the run's summary.

## Result

One training and its evaluation on the 172 test questions, one pass: GPT-5.6-Luna at medium effort acting and
learning (the [recipe](../../../docs/recipes/officeqa.md#opro) has the run and its records).

| Adapter | accuracy | − the initial skill, points |
|---|---|---|
| the initial skill (`../utils/seed_adapter`) | 0.355 | |
| **`final`, candidate 2 of 13 (validation 0.625)** | **0.436** | **+8.1** [+1.7, +14.5] |

Paired per question, 95% bootstrap interval. `final` is one 398-character instruction in place of the 886-character
skill; it ends "then return only the requested answer format", and long or Markdown answers fall from 55 to 13 of 172.
It is in [`adapter/`](adapter/), with its card ([`adapter/.hermit/CARD.md`](adapter/.hermit/CARD.md)).
