# OfficeQA Pro 50: GLM + Codex versus GLM + Codex + answer-informed MWM

This fresh v4 experiment follows the user's request of 2026-09-21. It retains the
v3 native Codex harness, model, tools, document corpus, grading and OS isolation.
The changes are: 50 questions, no model-request/tool-call count caps, a per-response
ceiling equal to the remaining output allowance, and completed-batch official
answer feedback for the GLM learner. This is gold-supervised online adaptation,
not a standard frozen held-out evaluation or the official leaderboard setting.

## Fixed data

- Same dataset revision: `763a8366abf2a3605c381d53586d844dc60fa756`.
- First 50 of the earlier frozen 70, preserving its original order. Seed remains
  `metaworld-officeqa-pro-codex70-online-v1`; sort SHA256(seed + NUL + uid).
  Neither answers nor prior performance influence question selection.
- Both arms attempt the identical ordered 50 questions once each.
- Same 697 officially downloaded parsed TXT documents and deterministic FTS5 index.
  No answer-side selection or source_docs/source_files hints.
- Offline parsed-text subset, retaining questions requiring outside information.
  It is not the full 133-question PDF plus web condition.

## Models, harness and budgets

- A: `accounts/fireworks/models/glm-5p3-flash` + actual native Codex CLI.
- B: identical GLM + actual Codex CLI + native MetaHarnessAdapter MWM memory.
- Fresh conversation and folder per question. B starts from generic repository W0.
  No old run answers, feedback, learned adapter or Astra-generated task critique.
- Actor uses Fireworks Responses; learner uses Fireworks Chat Completions.
  A recording relay is a transport/security boundary, not a replacement actor loop.
- Each actor: no logical request-count cap, 1,800 seconds including waits, and
  65,536 aggregate output tokens. Output includes reasoning when counted in the
  provider's output usage. The token budget is output-only, as in v3, not input+
  output. Each response can use up to the entire remaining output allowance.
- Each learning session: no logical request-count or file-tool-count cap,
  1,800 seconds including waits, 65,536 aggregate completion tokens, with the
  same remaining-allowance per-response behavior. Chat reasoning_effort=4096.
- Actor temperature 0, native Codex reasoning high. Learner temperature 0.
  Structural limits (read/write sizes, context windows, paths) are not removed.
- One actor per arm; the two arms may run concurrently. One frozen adapter for
  each five-question batch. B waits for its learner before the next batch.
- A shared scheduler allows two API requests in flight, with a one-second
  dispatch gap. Only explicit pre-generation rate refusals retry identical
  payloads, at most five wire attempts. This is transport recovery, not a cap on
  the agent's model/tool reasoning turns. Generated outputs and unknown failures
  are not retried. Missing usage is never described as zero cost.
- A generated response with unknown output usage cannot safely continue within
  the token budget; record the failure rather than granting unmetered allowance.
- Individual transport/tool timeouts remain infrastructure health limits. Outer
  episode/session deadlines and cumulative output accounting bound the experiment.

## Sandbox

- Same externally enforced macOS OS sandbox around native Codex and its children.
  Only its own task folder is writable. Instructions/configuration, corpus,
  index, permitted runtime, and active adapter are read-only.
- Actor cannot read master answers, original CSV, source mappings, current or
  historical evaluation records, siblings, future tasks or the other arm.
- Actor network can reach only its own localhost inference relay. Relay forwards
  only to the fixed Fireworks model endpoint. Other local/external destinations,
  web search, remote tools/files, model changes and cross-task response IDs denied.
- Learner file-tool processes have no network and only bounded native MWM file
  areas. The controller supplies GLM transport; no browser, shell, alternate model
  or human feedback channel is available to the learner.
- No inherited user/global Codex configuration, plugins, skills, memories or
  multi-agent access. macOS cannot nest Seatbelt, so the inner Codex sandbox is
  disabled only inside the outer enforced sandbox. Actual denial tests are required.

## Completed-batch answer feedback

- `learn_every=5`. Q1-5 use W0, then GLM produces W1 for Q6-10, and so on.
  If admission fails, retain the last admitted W. W10 after Q46-50 is saved but
  cannot affect the 50-question score. Its cost is separately identifiable.
- Before releasing reference answers, the controller seals the exact five
  original adapted predictions and task identities. It then filters only those
  five official answers into separate `reference_answer.json` files.
- Each learner gets only current-batch tasks, fixed predictions, public completed
  tool/message trajectories and five reference answers. It gets no source hints,
  future tasks/answers, baseline traces or grader-generated critiques/scores.
- Trusted controller receipt `private-learning-seals/XX.json` binds task,
  prediction and released-reference hashes, with sealing/release timestamps.
  Receipt is not a model-readable master key. The evidence manifest binds its hash.
- Learner must fully read each episode's task, prediction, trajectory and reference;
  those reads must reach a later GLM request before it may write feedback/memory.
- Only GLM authors `glm_reference_answer_feedback`: observations,
  reference_comparison, uncertainty and proposed_lesson for each of the five IDs.
  Official answers are supervision data, not human/Astra-authored explanations.
- Update reusable Markdown procedures and checks, not a task-ID/answer lookup table.
  This is prompted behavior, not a claim that a semantic filter can prove perfect
  absence of answer memorization. Actor cannot write its own frozen adapter.
- Mechanical structure/provenance checks admit updates; no human/Astra picks
  candidates by quality or supplies task-specific corrections. Astra writes the
  fixed implementation and protocol, so we do not claim zero Astra involvement.
- Prediction hashes are rechecked before subsequent batches and final scoring.
  No retaking/regrading earlier questions after seeing their official answers.

## Scoring and evidence

- Exactly 100 original prediction records sealed before final official scoring.
- Same pinned reward.py; primary numeric tolerance 0, secondary 0.01 separately.
  No LLM judge. Empty/failed answers stay in the fixed 50-question denominator.
- Same final format: one nonempty direct-answer line at most 250 characters.
  No manual extraction or repair from failed trajectories.
- Report paired results, disagreement counts, failures, output usage and learning
  cost, actual adapter lineage, and original GLM feedback. Extra learning compute
  and access to completed reference answers belong explicitly to B's treatment.
- This is one sequential experiment. Improvement is not established before data,
  and a difference cannot be attributed solely to memory separately from answer
  supervision and extra inference. Audit saved records, not independent network
  attestation or a guarantee about the model's pretraining data.

## Dataset condition to resolve before releasing real answer feedback

The official HF access page restricts answer-key use to train models evaluated on
OfficeQA or artificially inflate scores. It does not explicitly define whether
persistent non-weight textual adaptation is covered. The local download receipt
contains no applicability clarification or separate authorization. Record the
user's/publisher's clarification before the real gold-feedback launch; do not
represent an unresolved interpretation as confirmed permission. Offline tests
use invented data and do not require any official answer exposure to a model.

Sources:
- https://huggingface.co/datasets/databricks/officeqa
- https://github.com/databricks/officeqa/tree/7b9a3c154ef9fb40215bb67934afc43e6799de16
- https://docs.fireworks.ai/ecosystem/fireconnect/codex
- https://docs.fireworks.ai/guides/response-api
