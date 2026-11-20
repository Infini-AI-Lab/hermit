# SHE on AgentDojo

This uses the existing `she@1.0` four-artifact adapter and `she` Evolver, calling
[SHE's pinned upstream library](https://github.com/RainbowQTT/SHE/tree/0c656460d9d8acdf406a2271d657f7a7b60bb255).
The system prompt, rule bank, safety memory and tool policy evolve; the victim,
AgentDojo tasks, native judges and frozen injection payloads remain fixed.
The [method guide](../../../docs/methods/she.md) documents update/selection fidelity.
No new method or replacement selector is introduced for AgentDojo.

## Setup and protocol

Follow the [AgentDojo comparison guide](../README.md#shared-she-and-evosafe-comparison)
and [data protocol](../utils/benchmark/DATA.md). Install the additional dependency in the
same resolver invocation as the benchmark and Hermit:

```bash
uv pip install --python .venv-agentdojo/bin/python -e '.[test]' --group she -e ../agentdojo
```

`run_nemotron_codex.yaml` matches AgentDyn's tested actor settings: Nemotron 3.5
Lightning, 25 tool iterations, 8,192 output tokens, temperature zero, 12 concurrent
episodes, and a 900-second whole-episode deadline. The selective judge uses the
same frozen model, with eight bounded calls per episode. Codex uses GPT-6 Sol,
high effort, web search disabled, read-only sessions and a 1,500-second role bound.
`run.yaml` is a minimal API-proposer example and needs its `llm` configured.

The comparison data has 57 TRAIN cases (19 user groups), 54 reserved validation
cases (18 groups) and 406 TEST cases (55 groups). Validation is unused by this
TRAIN-only protocol. Each of at most 20 rounds measures all TRAIN cases; upstream
`maybe_update_best` applies the `initial_ua_floor` gate. Diagnosis, attribution
and patches use current TRAIN observations only. A proposed adapter becomes
eligible after the next measured round. Finalization restores measured best,
including when the latest proposal was never measured.

AgentDojo, like AgentDyn, supplies no clean unsafe-behavior label. Clean UBR is
unavailable rather than inferred from native utility. The official score and
available-metric gate are unchanged; the gate does not guarantee clean task
utility. Report raw clean utility and its paired regressions separately. Safe UA
means native injected task success **and** attack failure; it differs from raw
injected utility. Failed executions stay out of completed-case denominators and
are counted separately. Neither failures nor missing verdicts count as defenses.

The native suites also lack SHE's signed tool registry. The reused runtime
checks membership against the native public tool schemas, while signed-registry
integrity is unavailable, exactly as in the AgentDyn task adaptation. Neither
this example nor its scores establish signed-tool integrity.

## Run

Configure `VICTIM_API_KEY` and a private `HA_BACKENDS` mapping for `vllm`, as in
the [shared guide](../README.md). Commit source/config changes and choose fresh
run/tree names before invoking the frozen runner:

```bash
python examples/agentdojo/utils/experiment.py \
  --config examples/agentdojo/she/run_nemotron_codex.yaml
```

The runner checks Docker, the seed's four artifacts and the shared Codex role;
freezes source, data and settings; trains; then evaluates `none`, `root` and
`final` on identical TEST IDs. It exports comparison reports, per-case records,
usage and adapter snapshots under the ignored run's `artifacts/` directory.
Learning failures/refusals remain recorded failures; a process exit alone does
not establish successful learning. Refusals are not rerouted or retried.

## Results

The following are single-pass reference measurements under the frozen local
protocol. All three adapters use matching data, victim settings and evaluation
budgets.

### Completed search

Run `agentdojo-she-lightning-codex-v1` completed all 20 TRAIN rounds without learner
errors. The official selector updated its best **record** at rounds 2, 6 and 9;
finalization restored the round-9 measured snapshot. Rounds 6 and 9 measured the
same four-artifact content, so their different scores are repeat variability, not
another learned artifact update. The final adapter's rule bank and tool policy
differ from seed; system prompt and safety memory remain unchanged.

The final adapter-tree training commit is
`0eac808a01855e864a1a0a9fc8138766f504c69b`. Its four-artifact digest is
`092ba06a0f22f0582c993bfb6d96099db71a3b83bf79124cd8da5c9b519fd440`, matching both the
official best harness and round 9's observed snapshot (also round 6's content).
The round-20 unmeasured pending proposal has different digest
`11b67239604f3ed63fb8bb2be5a7638950b10c9873169bfe34da185e7026970a` and is excluded.

On TRAIN, seed ASR is 7/37 and safe UA 25/37; the best round-9 record has ASR 0/37
and safe UA 33/37. Both records have one injected execution error and clean native
utility 17/19. Round 6 has the same artifact as round 9 but safe UA 32/37, showing
why best-record promotions cannot be interpreted as distinct causal improvements.
Failures are excluded by the upstream selector's completed-case denominators and
must remain separately reported. These TRAIN numbers are not held-out results.

The source is frozen at `f829fc91390d81c992fbd6a90cb56e19d8b5396c`, official AgentDojo
at `089ed468cf3ed0322acc66b0211f26d9d90dbf60` with native task version `v1.2.2`, and
SHE at `0c656460d9d8acdf406a2271d657f7a7b60bb255`. The task JSONL SHA-256 is
`d50945a77e81afb21a7556a10b29d1989a99c4822eabd45f81c4d67013f0d65e`.
The configured victim is `nvidia/nvidia/nemotron-3.5-lightning`, Codex requests
`gpt-6-sol`/high with CLI `0.164.0`, and no separate observed serving-model ID is
available from Codex exec. Pre-TEST Trainer token counters are
`rollout=24,422,520` and `evolver=3,158,175`, with 4,672.7 seconds of Trainer wall
time. Dollar prices are unconfigured; zero known-subtotal fields are not zero cost.

An independent audit of retained records/code confirmed all 1,140 learning rows
and all selection IDs belong to TRAIN, and verified the observed/pending snapshot
chain and final export. Retained SHE role events have no tool calls. The audit
covers retained records and implementation paths; not every historical
`prompt.txt` is retained. A separate result audit recomputed the completed TEST
metrics and verified case and adapter provenance.

### Frozen TEST

The 2026-11-17 comparison is complete: every adapter has exactly the same 406 TEST
IDs, comprising 55 clean and 351 injected attempts from 55 held-out user groups.
Percentages use completed-case denominators; errors are separate. Safe completion
means native task success and attack failure together.

| TEST metric | No adapter | Initial SHE | Final SHE |
|---|---|---|---|
| Clean task success | 50/55 (90.9%) | 50/54 (92.6%) | 48/54 (88.9%) |
| Attack success, lower is better | 73/343 (21.3%) | 59/345 (17.1%) | 12/343 (3.5%) |
| Raw injected task success | 250/343 (72.9%) | 265/345 (76.8%) | 268/343 (78.1%) |
| Safe injected completion | 222/343 (64.7%) | 239/345 (69.3%) | 267/343 (77.8%) |
| Clean / injected execution errors | 0 / 8 | 1 / 6 | 1 / 8 |
| Total errors / planned cases | 8/406 | 7/406 | 9/406 |

The final adapter reduces observed ASR and improves safe injected completion, but
clean utility declines and execution errors increase. In paired clean cases against
initial SHE, one improves, three regress and 50 tie; one pair has an execution
error. Native clean utility is not an acceptance constraint; the official clean-UBR
signal is unavailable on this task. This is an observed attack-resistance/utility tradeoff, not uniform
improvement or a statistical-significance claim. The single pass, correlated
injection variants and demonstrated repeat variability limit inference.

No adapter has an error-free complete-run defense objective. The run status is
`complete_with_episode_errors`; complete coverage is not complete success, and
errors never count as blocked attacks. Initial SHE has seven nonterminal episodes.
The no-adapter run has seven nonterminal episodes and one model response without a terminal
answer. Final SHE has eight nonterminal episodes and one response truncated at
the model output limit. The 25-iteration bound and strict protocol failure handling
are identical across variants.

The supervisor records 8,154.8 seconds (about 2 h 16 min). Actor concurrency is 12;
all variants use 8,192 output tokens, temperature zero and 25 iterations. The
selective judge has the same eight-call budget for seed and final. The fixed hook
image is
`python@sha256:6b25cbe6577518889d553cdd5a5da97d259348d8f6e45b527dd731caaba27a07`.

Ignored local `runs/agentdojo-she-lightning-codex-v1/artifacts/` retains the source
snapshot reference, effective configuration, native results, training selection
records, comparison reports and root/final adapters. The reviewed reports and
adapters are also bundled as `review-artifacts.tar.gz`; raw logs remain local.
No generated cases, trajectories, credentials or per-run snapshots are committed.
