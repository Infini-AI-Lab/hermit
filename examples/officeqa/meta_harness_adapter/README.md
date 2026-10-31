# OfficeQA with Meta-Harness Adapter

The outer loop of [Meta-Harness](https://arxiv.org/abs/2603.28052) (Lee et al. 2026;
[code](https://github.com/stanford-iris-lab/meta-harness)) with the search confined to the adapter, on OfficeQA in the
SkillOpt paper's setting, with Codex as the acting agent. Meta-Harness also rewrites the acting agent's own loop; here
the agent is fixed and only the adapter changes. The
[`meta_harness_adapter`](../../../docs/methods/meta_harness_adapter.md) Evolver runs a Codex proposer that reads every
earlier candidate's files, scores and trajectories and writes two new adapters per round; each is evaluated on the 50
training questions (the search set), and the best there is evaluated once on the 172 test questions. The adapter is
the [`meta-harness-adapter`](../../../hermit/algorithms/templates/meta_harness_adapter/) template: every file outside
`.hermit/` is the proposer's to write.

```bash
python examples/officeqa/meta_harness_adapter/make_seed.py                  # ADAPTER.md = SkillOpt's initial skill
export OFFICEQA_CODEX_IMAGE=officeqa-codex:0.162.0
hermit train --config examples/officeqa/meta_harness_adapter/demo.yaml       # one round: the seed and two candidates
hermit train --config examples/officeqa/meta_harness_adapter/run.yaml        # six rounds of two, 13 epochs
hermit eval  --config examples/officeqa/meta_harness_adapter/run.yaml --adapter final
```

The acting agent is `../utils/agent/codex_agent.py` with `deploy: adapter`: `ADAPTER.md` becomes the session's
`AGENTS.md` and every other file of the adapter is copied to the same path in the session's workspace, so a
candidate may bring scripts or notes along with its instructions (`task-info/README.md` tells the proposer so). The
Codex image, the models (GLM 5.3 Flash acts and proposes, medium effort), the 3,600 s session limit and the one-pass
evaluation are those of the Continual Harness and Harness² streams (`../continual_harness/`,
`../harness2_stream/`). The proposer runs in the run's sandbox, with read access to the corpus and no network but its
model's endpoint.

Each proposal step's `step_NNNN/output/` holds the round's prompt, the proposer's session and its report;
`workspace/state/evolution_summary.jsonl` lists every candidate with its hypothesis, base and mean on the search set,
and `workspace/state/candidates/<id>/adapter/` its files.

## Result

One training (`run.yaml`, run `mh-flash`, 2026-10-30) and one evaluation of its final adapter on the 172 test
questions; Codex 0.162.0 with GLM 5.3 Flash (medium) acting and proposing, 3,600 s per session; exact match. One
pass, so each accuracy carries a standard error of about 3.7 points.

| Candidate | Search set (50) |
|---|---|
| The starting adapter | 0.60 |
| The other eleven candidates | 0.64 to 0.72 |
| **`r06_exact_source_lock`, handed back** | **0.72** |

| Test (172) | accuracy |
|---|---|
| Harness², reference execution of the initial skill (the adapter the search starts from) | 0.576 |
| **Meta-Harness Adapter, final adapter** | **0.640** |
| Continual Harness, the stream's answers | 0.634 |
| ReHarness, the stream's answers | 0.634 |

Paired per question with 95% bootstrap intervals: Meta-Harness Adapter − initial skill **+6.4** [+1.2, +11.6] (17
questions won, 6 lost); Meta-Harness Adapter − Continual Harness +0.6 [−5.8, +7.0] (17 won, 16 lost); Meta-Harness
Adapter − ReHarness +0.6 [−5.2, +6.4] (13 won, 12 lost). The streams learn on the test questions themselves;
Meta-Harness Adapter searched on the training questions and was tested once.

Every candidate changed `ADAPTER.md` alone. The final one (5.7 KB) is the starting skill rewritten into an answer
contract (count, order, sign, precision, no unrequested units), an evidence plan with an exact-source lock (operands
from the bulletin, table and edition the question names), a source audit before computing, and computation in
Python. Its formatting rule quotes two training answers as examples of a bare numeral; it holds no test answers.

Spending and time: rollouts USD 8.39 (650 on the search set), proposer USD 0.17, 1.5 h; the test evaluation USD
1.82. The method's row is on the [recipe's Flash row](../../../docs/recipes/officeqa.md#the-flash-row) with the
others'; the paper's own runs are larger (about 20 rounds of 2-3 candidates).
