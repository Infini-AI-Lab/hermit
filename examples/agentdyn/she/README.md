# SHE on AgentDyn with Lightning

This integrates the four-artifact evolution mechanism from [SHE](https://github.com/RainbowQTT/SHE), pinned to
`0c656460d9d8acdf406a2271d657f7a7b60bb255`. The acting victim is frozen; only the system prompt, rule bank,
safety memory and tool policy evolve. Attacks remain the existing frozen AgentDyn cases. See the
[method page](../../../docs/methods/she.md) for the upstream comparison, selection rules and adaptation limits.

## Install and prepare

Use Python 3.12 and the [pinned, patched AgentDyn checkout](../README.md#install). Install all dependencies in
one resolver invocation so the optional benchmark cannot silently replace Hermit's constraints:

```bash
uv venv .venv-agentdyn
uv pip install --python .venv-agentdyn/bin/python -e '.[test]' --group she -e ../AgentDyn
source .venv-agentdyn/bin/activate
```

Follow the [shared preparation commands](../README.md#prepare-the-cases), including `--full-test`, to create
`../utils/data/full-test-v1`. The prepared data contains 57 TRAIN, 54 reserved validation and 186 TEST cases, from disjoint
user-task groups. SHE uses TRAIN for official best-harness selection and leaves validation unused.
TRAIN is unchanged; TEST has 18 clean and 168 injected cases. Preparation never uses model
outcomes, and injection payloads and native judges remain unchanged.

## Models and recipes

| File | Acting and runtime-judge victim | Learner | Search |
|---|---|---|---|
| `run_nemotron_codex.yaml` | Nemotron 3.5 Lightning | GPT-6 Sol Codex, high effort | 20-round cap, 12-hour budget |
| `run_nemotron_claude.yaml` | Same Lightning victim | Claude Code | Same data and gate |
| `run_nemotron_codex_daybreak.yaml` | Same Lightning victim | Codex with an explicit approved Daybreak Blue request | Same data and gate |
| `run.yaml` | Minimal custom-model example | Shared direct learner | Small configurable batches |

All Lightning recipes retain `max_iters: 25` for the acting agent. The separate 20-round cap follows the
released SHE driver. Each round measures all 57 TRAIN cases, calls upstream `maybe_update_best`, diagnoses
failures, restores measured best and proposes the next round's patch. The first round initializes baseline;
a proposal is eligible only after its next-round measurement. Finalization restores measured best.
There is no separate validation-selection stage. TEST remains frozen evaluation only.

The official gate requires a strictly better `(1 - clean_UBR) + (1 - ASR) + UA` score, nonincreasing available
clean UBR and ASR, and safe attack-task utility at or above the initial baseline. Execution failures are
excluded from upstream metric denominators and reported separately. AgentDyn has no clean unsafe label:
clean UBR stays explicitly unavailable, and native clean utility adds no selection constraint. See the
[method page](../../../docs/methods/she.md) for this task limitation and exact label mapping.

Configure `vllm` outside version control, in Hermit's backend settings or a file named by `HA_BACKENDS`:

```yaml
vllm:
  base_url: https://your-model-service.example/v1
  api_key_env: VICTIM_API_KEY
  key_required: true
  apis: [chat]
```

The tested service model ID is `nvidia/nvidia/nemotron-3.5-lightning`; the repeated prefix belongs to that ID.
Use a local `models` alias if your endpoint exposes another name. Credentials never enter adapter artifacts or
learner prompts. The selective judge uses this same frozen model through the shared client, with eight bounded
calls per episode and no tools. Unknown verdicts block uncertain actions. Seed and final use identical budgets.

## Run

Commit source changes and select fresh run/tree paths before using the frozen experiment runner:

```bash
python examples/agentdyn/utils/run_experiment.py \
  --config examples/agentdyn/she/run_nemotron_codex.yaml
```

The runner freezes source, configuration, model settings and data, records installed dependency versions,
and checks Docker and the learner;
trains; then evaluates `none`, `root` and `final` on the same TEST IDs. It verifies actual learning-stage outcomes
rather than treating a successful process exit as completed search. A failed learner call preserves measured best and is recorded. A provider refusal or search error
persists a stop flag and returns `stop`; recipes enable `trainer.allow_early_stop`. Even if a custom Trainer
ignores that signal, later SHE steps make no further learner calls. Refused requests are not rerouted.

The committed recipes use Hermit's existing learner sandbox and a host task pipeline that launches the learned
hooks' separate networkless Docker containers. On a machine without usable bubblewrap, a local experiment may
pass `--no-sandbox` to this runner. It freezes `sandbox.enabled=false` into the effective configuration and
records that exception as `runtime_overrides` in the snapshot manifest without editing the committed recipe. This
does not disable Docker isolation for learned artifacts. The Codex session remains read-only.

Optional Daybreak settings request approved account access, not an override of provider safety checks. Use the
extra recipe only with a CLI and account supporting that option, and record it with the experiment.

## Verification and outputs

```bash
HERMIT_TEST_DOCKER=1 python -m pytest tests/methods/she -q
python -m pytest --changed
bash docs/build.sh
```

The tests exercise the actual upstream runtime, constrained patching, native replay, fresh state, memory and
rule effects, final-response replacement, same-victim semantic judging, Docker RPC, TRAIN-only evidence,
official metric/selector parity, error denominators, next-round promotion and measured-best finalization. The fixed
specification checks run unconfined only for repository-owned test fixtures; platform sandbox checks are separate.

Per-round outputs retain `training-records.json`, diagnoses, routes, manifests, patch, candidate,
`iteration-diff.json`, `attribution.json` and `selection.json`. State retains `she-feedback.json`, including the
best TRAIN records and the pending adapter hash. Official snapshots and baseline metadata live in
`state/she-official/`. Failed TRAIN episodes retain their partial public message
histories for diagnosis; reads are restricted to current TRAIN material. Episode logs include native trajectories,
tool audit,
`defense-state.json`, semantic-judge calls and metered usage. Native exceptions retain their type and a bounded stack of code locations in `failure.json`, without exception
text, source lines or local variables. State-export and cleanup failures are recorded separately in
`finalization-errors.json`; they preserve the original episode error, close remaining clients and retain known
usage. Such finalization failures remain episode errors and cannot count as successful defenses. Only shared model clients perform model calls.

The runner exports comparison reports and root/final adapters under the ignored run's `artifacts/` directory.
Keep manifests, effective config, spending, per-case records and adapter snapshots together. Do not commit
private backend settings, generated data, logs or per-run adapter snapshots.

## Results

A full official-protocol run completed on 2026-11-12 from source
`423cc012591d453fb042c7512e1888b2e44860b4`, using the recipe above, GPT-6 Sol high effort with an explicit
Daybreak Blue request, and Lightning as actor and selective judge. TRAIN is unchanged at 57 cases; reserved
validation is unused. All 20 TRAIN rounds completed without learner errors: round 1 initialized baseline,
rounds **2, 3, 5, 6 and 9** promoted evaluated candidates, and 14 later comparisons retained best. Finalization
exported the measured round-9 snapshot rather than the last proposed patch. The final artifact hash matches
saved best; system prompt and tool policy changed from the original seed.

TRAIN ASR decreased from 9/36 (25.0%) to 0/37 (0.0%), and official safe UA increased from 21/36 (58.3%) to
30/37 (81.1%). These are completed injected-episode metrics; they are not held-out performance.

The following **frozen TEST** comparison contains all 186 planned cases per variant, with 18 clean and 168
injected cases from 18 user-task groups. Percentages use each variant's own completed-case denominators.
UA means safe task completion, rather than raw task utility.

| TEST metric | No adapter | Initial SHE | Final SHE |
|---|---|---|---|
| Clean task success | 13/18 (72.2%) | 13/17 (76.5%) | 9/16 (56.3%) |
| Attack success, lower is better | 46/154 (29.9%) | 33/155 (21.3%) | 3/153 (2.0%) |
| Official safe UA | 75/154 (48.7%) | 83/155 (53.5%) | 92/153 (60.1%) |
| Raw injected task success | 97/154 (63.0%) | 95/155 (61.3%) | 94/153 (61.4%) |
| Execution errors, all planned cases | 14/186 | 14/186 | 17/186 |

All execution errors exhausted the 25-step loop without a terminal assistant response. Final errors comprise
2 clean and 15 injected cases, compared with 1 clean and 13 injected errors for initial SHE. Errors are not
credited as blocked attacks. Complete coverage does not mean all episodes succeeded: the run status is
`complete_with_episode_errors`, and complete-run native objectives remain unavailable.

The final adapter improves observed attack resistance and safe injected-task completion, but **clean task
success regresses**. Among jointly completed clean pairs, none improved, two regressed and thirteen tied;
three clean pairs involve an execution error. This is not an unconditional improvement or a statistical
significance claim. A single run and shared user-task groups limit inference. Clean UBR remains unavailable.

The frozen data SHA-256 is `b529dfa72103fe8c55972e1960dcf492adda2be7dfdd6b1c0a87433007e043fb`;
AgentDyn revision is `5353cf7615b135cace8d07c8f12dac53a16b6db3`, Codex CLI is `0.164.0`, and the installed SHE
revision is the pin above. The learner model ID is configured; the CLI did not emit a separate serving-model ID.
Acting uses concurrency 12, 8,192 output tokens, temperature zero and 25 iterations;
selective judging allows eight calls. `--no-sandbox` records the user-authorized bubblewrap exception; artifact
Docker isolation remains enabled. Total search plus TEST wall time was 17,178.5 seconds (about 4 h 46 min).
Reported TRAIN/search token totals were 95,438,369 rollout and 5,214,982 learner tokens, excluding TEST.
Dollar spending is unknown because prices are unconfigured; zero subtotal placeholders are not zero cost.

Reproduce with the committed recipe and fresh run/tree names. Ignored run
`runs/agentdyn-she-lightning-codex-daybreak-official-r2/artifacts/` retains effective config, source/data hashes,
all per-case records, comparison reports and root/final snapshots. Earlier interrupted or nonofficial attempts
are separate and are not the results above. Generated cases, trajectories and snapshots are not committed.
