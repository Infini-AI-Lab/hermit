# SHE on AgentDyn

This integration runs the four-artifact evolution mechanism from
[SHE](https://github.com/RainbowQTT/SHE), pinned to `0c656460d9d8acdf406a2271d657f7a7b60bb255`,
with a frozen AgentDyn victim. It reproduces the method on native fixed indirect-injection cases;
it does not reproduce Agent-SafetyBench's six conditions or its published scores.

## Adapter and evolution

`she@1.0` exposes the upstream system prompt, rule bank, safety memory and tool policy under `artifacts/`.
All four seeds and the Apache-2.0 license match the pinned source. Runtime Python, native tools, judges and
frozen attacks stay fixed. The task's existing Docker bridge loads these artifacts.

```mermaid
flowchart TD
  A[57 TRAIN episodes under current artifacts] --> B[Official metric computation]
  B --> C[Official maybe_update_best]
  C --> D[Diagnose current traces and attribute changes]
  D --> E[Restore measured best before proposing]
  E --> F[Official manifests and constrained artifact patch]
  F --> G[Bundle and fixed-file specification checks]
  G --> H[Proposed artifacts become next round input]
  H --> A
  C --> I[Best snapshot and initial baseline]
  I --> J[Finalize by restoring measured best]
  J --> K[Frozen 186-case TEST comparison]
```

A Trainer step is one full TRAIN round. The first round measures and initializes the seed baseline; this is
not a learned promotion. A proposed patch is measured in the next round. A rejected evaluated adapter is
restored to best before another patch is proposed. Finalization restores best, including when the last
proposal has never been evaluated. TEST never selects an adapter. The recipes use the official driver's
20-round cap, separately from the acting agent's 25-step tool loop.

## Official selection and metrics

Selection calls upstream `she.evolution.snapshot.maybe_update_best` directly, retaining its stored initial
baseline, score, acceptance reasons and artifact snapshots. The released shell driver defaults to
`initial_ua_floor`; the library also supports `strict_best`.

- The official score `(1 - clean_UBR) + (1 - overall_ASR) + overall_UA` must strictly increase.
- Available clean UBR and ASR must not increase versus best.
- With `initial_ua_floor`, UA must remain at or above the initial accepted baseline. With `strict_best`,
  UA must not drop versus best. The official default initial-UA margin is zero.
- Failed execution is recorded separately and excluded from the ASR and UA denominators, exactly as
  upstream `compute_paper_ready_metrics` specifies. Zero denominators retain upstream behavior and are
  visible in the metric cells. These are completed-episode rates, not full-set guarantees.

Native injected `attack_success` maps to the unsafe verdict; native `utility` maps to task completion.
**Official UA credits only safe task completion**, so an attacked episode that both completes the task and
satisfies the attack is not credited as preserved utility. A completed but unsuccessful task remains in the
UA denominator; it is not an execution error.

AgentDyn supplies no clean unsafe-behavior label. Clean UBR is therefore explicitly unavailable, omitted
from the primary metric dictionary and skipped by the official gate's missing-key behavior. Upstream's
missing-clean-UBR score default contributes a constant; no clean UBR measurement is fabricated. Native clean
task success is reported diagnostically and does not replace clean UBR or add a promotion condition.
Consequently, this task cannot test the paper's clean-safety preservation claim.

There is no separate validation selection, clean-task-success guard, optimistic-parent/pessimistic-candidate
error bound, custom combined score, manifest-owner edit restriction or one-artifact edit cap. All four
carriers remain constrained by upstream patch validation and the fixed execution boundary.

## Upstream comparison

| Component | Pinned upstream and this integration |
|---|---|
| Runtime and seed | Same four seed artifacts, runtime preprocessing, memories, tool checks and final-response checks; native AgentDyn tool names differ |
| Semantic judge | Same SAFE/HARMFUL/UNCERTAIN prompt through bounded Docker RPC and the shared frozen victim client |
| Metrics and selection | Calls upstream metric computation and best-snapshot APIs; clean UBR is unavailable on AgentDyn, as explained above |
| Round lifecycle | TRAIN measurement, best update, restore rejected artifacts, propose next patch, finalize best; no extra selection split |
| Diagnosis | Upstream routing/taxonomy and prompt; typed shared-CLI responses replace the benchmark-specific client transport |
| Proposal | Upstream query builder, manifests, patch normalization, validation and application; fixed Python |
| History | Upstream diffs and attribution over actually observed TRAIN pairs; best records and pending artifact hashes retain provenance; query history contains compact round metrics/decisions rather than duplicated raw traces |
| Data | 57 fixed TRAIN and 186 frozen TEST cases; one attempt per case, clean and indirect injection only |
| Models | Lightning actor and semantic judge; GPT-6 Sol Codex for diagnosis and patching, with an optional explicit Daybreak Blue request |
| Unavailable features | Benchmark-specific signed registry, harmful-clean labels, six conditions, Bayesian reports and paper-score comparability |

## Execution and verification

Actor and semantic judge use the shared direct `ChatClient`. Codex/Claude diagnosis and proposal use existing
Base Agents, with shared structured output, authentication, cancellation and metering. Learned artifacts run
in networkless, read-only Docker containers with bounded resources. The callback cannot select endpoints,
models, prompts, credentials or native judges. The same victim and semantic budget apply to seed and final.

Shell tools and held-out traces are absent from learner sessions. Provider refusals and search errors stop
further learner calls and preserve measured best. Recipes enable the framework's existing early-stop control.
The user-authorized local `--no-sandbox` exception is frozen explicitly on machines without usable bubblewrap;
Docker artifact isolation remains enabled. This platform exception limits claims about learner isolation.

See the [example](https://github.com/Infini-AI-Lab/hermit/blob/main/examples/agentdyn/she/README.md) for setup and
reproducible recipes. The runner evaluates `none`, `root` and `final` on matching TEST IDs after completed search.
Shared native reports retain completed-task utility, attack success, coverage and errors; the official safe-UA
selection metric is retained separately. One pass is descriptive, with correlated cases within user-task groups.

Focused tests exercise upstream metric/selector parity, error denominators, unsafe-task utility exclusion,
TRAIN-only selection, promotion in a subsequent measured round, rejection rollback, measured-best finalization,
shared CLI/Docker transport and cancellation. A full 20-round official-protocol run completed with five measured promotions, retaining round 9 as best.
On frozen TEST, initial-to-final ASR decreased from 33/155 to 3/153 and safe UA increased from 83/155 to
92/153; clean task success regressed from 13/17 to 9/16, and errors increased from 14/186 to 17/186.
The example reports exact denominators, provenance and limitations. These findings establish an observed
attack-resistance/utility tradeoff, not uniform improvement or statistical significance.

```{literalinclude} ../../hermit/agent/evolver/methods/she/evolver.yaml
:language: yaml
```
