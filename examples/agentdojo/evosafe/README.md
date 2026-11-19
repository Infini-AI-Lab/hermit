# EvoSafeHarness on AgentDojo

[`run.yaml`](run.yaml) connects the existing EvoSafeHarness method and
`evosafe@1.0` adapter to official AgentDojo's frozen replay task.
Follow [installation and data preparation](../README.md) first. The config's
paths are relative to this folder; preparation writes `../utils/data/`.

The victim defaults to `openai/gpt-4o-mini-2024-07-18`. Configure its backend and
credentials, and replace `llm: null` with your proposer before training. The
existing EvoSafe Designer/Criticizer/Analyzer roles, candidate selection and
clean-utility constraints are unchanged. The defense's structured hook framework
is `agentdojo`; server is the native suite name. Tool schemas arrive at runtime.

The bridge enforces `episode_timeout_s: 900` independently of `agent.timeout_min`.
`max_iters: 15` bounds the native tool loop. Learned hooks require Docker and the
`python:3.12-slim` image.

After configuring the models:

```bash
hermit check --config examples/agentdojo/evosafe/run.yaml
hermit train --config examples/agentdojo/evosafe/run.yaml
hermit eval --config examples/agentdojo/evosafe/run.yaml --adapter final
```

For a no-defense comparison use `hermit eval --config ... --adapter none` on the
same frozen test IDs. The shared AgentDyn `report.py` consumes the same per-case
metric contract; it is also usable for AgentDojo run records. Failed executions
are errors, not successful defenses. The Lightning/Codex comparison below records the first completed run.

## Matching Lightning/Codex experiment

[`run_nemotron_codex.yaml`](run_nemotron_codex.yaml) uses the shared
[57/54/406 protocol](../utils/DATA.md) and the same fixed Lightning actor settings
as SHE. TRAIN has 57 cases; reserved validation is unused. The Designer,
Criticizer, Cascade and Analyzer use the existing `evosafe` implementation, with
three proposals, cascade sizes 12/57, source gates, 0.07 improvement/tolerance
settings, conservative parent-error bounds and worst-case candidate-error bounds.
Fresh Codex roles use GPT-6 Sol/high, web search disabled and read-only sessions.
The optional output classifier has four bounded same-victim calls per episode.
These are AgentDyn's existing Codex settings, not a new search variant.

```bash
python examples/agentdojo/utils/experiment.py \
  --config examples/agentdojo/evosafe/run_nemotron_codex.yaml
```

See [shared setup and comparison protocol](../README.md#shared-she-and-evosafe-comparison).
The runner exports `none`, `root` and `final` TEST comparisons automatically.
Source/data/config snapshots, per-case records and adapter exports stay local
under the ignored run's `artifacts/`. Test outcomes never feed candidate selection.

## Results

The following are single-pass reference measurements under the frozen local
protocol. All three adapters use matching data, victim settings and evaluation
budgets.

The 2026-11-17 run `agentdojo-evosafe-lightning-codex-v1` completed all three
proposals. Every candidate had an approved independent Criticizer review; trial 1
was accepted and trials 2/3 retained the parent. The final adapter's three editable
files match accepted trial 1 exactly. Its adapter-tree training commit is
`57dfbe2fea8e3554e82056a6e972462b8492864f`; `ADAPTER.md`, `defense.py` and `BUNDLE.md`
all differ from the initial seed. This is a learned adapter, not the unchanged seed.

All three frozen TEST variants cover the same 406 IDs (55 clean, 351 injected)
from 55 held-out groups. Rates use completed episodes, with errors listed separately.
Safe completion requires native task utility and attack failure together.

| TEST metric | No adapter | Initial EvoSafe | Final EvoSafe |
|---|---|---|---|
| Clean task success | 47/54 (87.0%) | 49/55 (89.1%) | 48/55 (87.3%) |
| Attack success, lower is better | 81/344 (23.5%) | 57/344 (16.6%) | 8/347 (2.3%) |
| Raw injected task success | 243/344 (70.6%) | 263/344 (76.5%) | 268/347 (77.2%) |
| Safe injected completion | 214/344 (62.2%) | 240/344 (69.8%) | 264/347 (76.1%) |
| Clean / injected execution errors | 1 / 7 | 0 / 7 | 0 / 4 |
| Total errors / planned cases | 8/406 | 7/406 | 4/406 |

The final adapter reduces observed ASR and improves safe injected completion,
while clean utility falls by one success against the initial seed. Paired clean
cases have one improvement, two regressions and 52 ties, with no unpaired errors.
This is an observed tradeoff, not uniform improvement or statistical significance.
A single pass, repeated model variability and shared user groups limit inference.

Every recorded error lacked a terminal assistant response under the 25-iteration
bound. The experiment status is `complete_with_episode_errors`: coverage is complete,
but errors never count as blocked attacks and complete-run defense objectives are
unavailable. The scalar `hermit eval` mean is not the defense objective.

Source is frozen at `f829fc91390d81c992fbd6a90cb56e19d8b5396c`, official AgentDojo at
`089ed468cf3ed0322acc66b0211f26d9d90dbf60`, native task version `v1.2.2`, and task
JSONL SHA-256
`d50945a77e81afb21a7556a10b29d1989a99c4822eabd45f81c4d67013f0d65e`.
The victim is configured as `nvidia/nvidia/nemotron-3.5-lightning`; the Codex learner
requests `gpt-6-sol`/high with CLI `0.164.0`. Codex exec does not provide a separate
observed serving-model ID. Actor concurrency is 12; generation uses 8,192 output
tokens, temperature zero and 25 tool iterations. The hook image is the pinned
`python@sha256:6b25cbe6577518889d553cdd5a5da97d259348d8f6e45b527dd731caaba27a07`.

The experiment supervisor records 6,183.7 seconds (about 1 h 43 min). Pre-TEST
Trainer token counters are `rollout=1,523,511` and `evolver=22,456,529`; these are
framework metering buckets, not an assumed actor/proposer partition. Dollar
spending is unknown because prices are unconfigured; zero known-subtotal fields
must not be presented as zero cost. TEST usage is retained separately per case.

Ignored local run `runs/agentdojo-evosafe-lightning-codex-v1/artifacts/` retains the
manifest, frozen/effective config, per-case none/root/final records, comparisons,
training records and adapter exports. `review-artifacts.tar.gz` bundles the
structured reports and adapters; raw episode logs remain local. No generated
cases, trajectories, credentials or per-run snapshots are committed.
