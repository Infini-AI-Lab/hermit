# AgentDojo defense comparison

The official AgentDojo banking, Slack, travel and workspace suites share native
replay, model calls, Docker defense hooks and scoring with the AgentDyn
integration. [Setup and recipes](https://github.com/Infini-AI-Lab/hermit/tree/main/examples/agentdojo)
cover both [SHE](../methods/she.md) and [EvoSafeHarness](../methods/evosafe.md).
No model weights, native tools or judge predicates are changed.

The frozen comparison imports existing `tool_knowledge` payloads from the pinned
upstream logs, removes prior trajectories/scores, audits native initialization,
and splits by user task. It has 57 TRAIN, 54 reserved validation and 406 TEST
cases. Both Codex recipes select on TRAIN only and reserve TEST for frozen
no-defense/initial/final evaluation. See the
[data protocol](https://github.com/Infini-AI-Lab/hermit/blob/main/examples/agentdojo/utils/benchmark/DATA.md)
for source revision, exclusions, ownership and hashes.

Both methods use the same fixed Nemotron 3.5 Lightning victim, 25 tool iterations,
zero temperature and GPT-6 Sol/high Codex learners. SHE retains its 20-round
upstream measured-best selection; EvoSafe keeps its three-proposal source cascade.
Their search and auxiliary-judge budgets differ as documented method settings.
Clean utility, attack success, raw injected utility, safe completion and execution
errors have separate denominators. The scalar evaluation score is not the full
defense objective.

## Results

Both 2026-11-17 Lightning/Codex comparisons completed. These are **single-pass
reference measurements** under the frozen protocol above. Each row covers the
same 406 TEST IDs; denominators count completed
episodes, while errors count all planned attempts. Each method retains its own
no-adapter attempt because repeated model executions vary.

| Experiment / adapter | Clean utility | ASR | Errors / planned |
|---|---|---|---|
| EvoSafe / None | 47/54 (87.0%) | 81/344 (23.5%) | 8/406 |
| EvoSafe / Initial | 49/55 (89.1%) | 57/344 (16.6%) | 7/406 |
| EvoSafe / Final | 48/55 (87.3%) | 8/347 (2.3%) | 4/406 |
| SHE / None | 50/55 (90.9%) | 73/343 (21.3%) | 8/406 |
| SHE / Initial | 50/54 (92.6%) | 59/345 (17.1%) | 7/406 |
| SHE / Final | 48/54 (88.9%) | 12/343 (3.5%) | 9/406 |

Both final adapters reduce observed ASR and increase safe injected completion,
while clean utility decreases against their respective initial seeds. EvoSafe
errors decrease from 7 to 4; SHE errors increase from 7 to 9. All rows have
execution errors, so complete-run native defense objectives remain unavailable.
No statistical significance, uniform improvement or equal-compute ranking is
claimed. See the full records for paired changes, exact safe-completion figures,
source/data hashes, adapter selection and limitations:

- [EvoSafe results](https://github.com/Infini-AI-Lab/hermit/blob/main/examples/agentdojo/evosafe/README.md#results).
- [SHE results](https://github.com/Infini-AI-Lab/hermit/blob/main/examples/agentdojo/she/README.md#results), including the same-artifact repeat variation in TRAIN rounds 6 and 9.

Official AgentDojo and AgentDyn install the same `agentdojo` package name and
need separate Python environments.
