# OfficeQA with Evo-Harness

[Evo-Harness](https://arxiv.org/abs/2608.15071) (Wei et al., EMNLP 2026;
[code](https://github.com/A-EVO-Lab/a-evolve/tree/release/evo-harness)) on OfficeQA: the
[`evo_harness`](../../../docs/methods/evo_harness.md) Evolver compiles failed attempts into general and topic skills,
batch by batch, from SkillOpt's initial skill. It runs in OfficeQA's
[GPT-5.6-Luna setting](../README.md#gpt-56-luna-on-a-chatgpt-login): GPT-5.6-Luna at medium effort acting through the
Codex agent on the machine's ChatGPT login (no API key), and the learner one Codex session per call. The image, the egress
proxy and the login are set up once as in the recipe's
[A model on a ChatGPT login](../../../docs/recipes/officeqa.md#a-model-on-a-chatgpt-login).

```bash
python examples/officeqa/evo_harness/make_seed.py                                  # the initial skill + an empty block
hermit train --config examples/officeqa/evo_harness/smoke_codex_luna.yaml          # 4 questions, batches of 2
hermit train --config examples/officeqa/evo_harness/stream_codex_luna.yaml         # the paper's protocol: the 172 online
hermit train --config examples/officeqa/evo_harness/run_codex_luna.yaml            # train on 50
hermit eval  --config examples/officeqa/evo_harness/run_codex_luna.yaml --adapter final         # frozen, on the 172
hermit eval  --config examples/officeqa/evo_harness/run_codex_luna.yaml --adapter seed_adapter  # the initial skill
hermit eval  --config examples/officeqa/evo_harness/run_codex_luna.yaml --adapter none          # no adapter
```

## What to read after the smoke

The smoke is not a measurement; it shows whether each of the method's levers works in a real session. In
`runs/smoke-luna-evo-harness/`:

- `workspace/state/evo_harness.json`, per step: `proposals` (the candidates from the failed rollouts), `topics` and
  `general` (what the curators accepted, merged or skipped), `general_skills` and `topic_skills`;
- the steps' `output/`: every reflection and curator prompt and reply;
- `checkout/ADAPTER.md` and `checkout/skills/`: the skill unchanged at the top, the general skills in the block, the
  topic skills as host skills;
- the later rollouts' Codex traces: that the agent opened a skill.

## Result

One run of each and one pass, GPT-5.6-Luna at medium effort acting and learning (the
[recipe](../../../docs/recipes/officeqa.md#evo-harness) has the records). The batch is the paper's 16; the stream is the
paper's protocol, and with the judge's feedback it is online supervision, unlike this example's label-free neighbours.

| Adapter or protocol | accuracy [95% bootstrap interval] | − the initial skill, points | won / lost |
|---|---|---|---|
| none | 0.331 [0.262, 0.401] | −1.7 [−7.6, +4.1] | 12 / 15 |
| the initial skill (`seed_adapter`) | 0.349 [0.279, 0.424] | | |
| **`final`: trained on 50, frozen, on the 172** | **0.570** [0.494, 0.645] | **+22.1** [+14.5, +29.7] | 46 / 8 |
| **the stream's own answers over the 172** | **0.552** [0.477, 0.628] | **+20.3** [+13.4, +27.3] | 40 / 5 |

Paired per question, 95% bootstrap intervals; one run of each, one pass. The stream against `final`: −1.7 [−8.1, +4.7].

A run's harness is its `runs/<run>/checkout/`, kept out of the repository as CONTRIBUTING.md asks. The paper reports
means over three runs; this is one.

## Settings and differences

A step is a batch of 16 (`data.step_size`), 16 sessions at once, one pass over the data. Each reflection reads the
judge's feedback, which names the expected answer. No validation; the last harness is final. The full table of
differences from the authors' code is on the [method page](../../../docs/methods/evo_harness.md).
