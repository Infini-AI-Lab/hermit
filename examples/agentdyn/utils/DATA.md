# AgentDyn cases: inventory, exclusions and splits

Every case is an **existing frozen** AgentDyn case. Nothing is generated, rewritten or optimized. Each row also
carries the EvoSafe contract fields: `kind` (`clean` or `injected`), `group` (`<suite>/<user task>`) and
`domain` (the suite). Case selection uses source fields, native initialization checks and fixed seeds. It never
uses model outcomes.

## Source

- AgentDyn revision `5353cf7615b135cace8d07c8f12dac53a16b6db3`.
- Source collection: `runs/google_gemini-2.5-flash/{shopping,github,dailylife}` in that checkout.
- Only `suite_name`, `user_task_id`, `injection_task_id` and `injections` are imported. Payloads are copied
  unchanged; old responses, traces, scores and errors are discarded.

| Suite | Native user tasks | Native injection goals | Native vectors | Frozen source cases |
|---|---:|---:|---:|---:|
| Shopping | 20 | 9 | 28 | 180 |
| GitHub | 20 | 9 | 30 | 180 |
| DailyLife | 20 | 10 | 38 | 200 |

`prepare_expanded.py` runs a check on every source case: it initializes the clean and injected environments and
calls the **native judge dispatcher** on an empty trace. Clean utility, injected utility and attack success must
all start out false. Trace-based judges are supported. GitHub `injection_task_1`, for example, implements
`security_from_traces`.

This check verifies the wiring between environment and judge. It does not prove that every victim encounters
each payload, that user/goal pairs are independent, or that judges capture every part of a natural-language task.
The cases come from one recorded attack collection and reuse goal families; they are not independent attack
methods.

## Excluded user-task groups

These groups have a utility judge that contradicts its own user request in the pinned source. The judges
themselves are unchanged; the groups are left out.

| Suite / user task | Evidence in pinned source |
|---|---|
| Shopping / 4 | User asks for one item; utility requires quantity two |
| Shopping / 19 | User requests an invoice under `/user`; utility requires `/user/invoices` |
| GitHub / 9 | Utility reads both pre- and post-state from `post_environment` |
| GitHub / 12 | User asks to invite all collaborators; utility accepts either one |
| DailyLife / 18 | User says today; environment date is 2024-05-19, utility requires 2024-05-20 17:30 |

Other tasks were inspected at the source level but are not certified free of defects.

## Inventory and split ownership

`prepare_expanded.py` (seed `20261101`) writes `full/`: 569 cases from 55 eligible user groups, 55 clean plus 514
injected. Each group is owned by exactly one split: 19 train, 18 validation, 18 test. The SHA-256 of
`full/tasks.jsonl` is `4cc68ea19e1adc518b0a0965641b75c174853396231b55770006b5ac8fac791e`.

All variants of a user task stay in their owner's split, and a larger protocol never moves a group to another
split.

## The protocol

Three steps build it, each keeping the cases of the previous one:

1. `prepare_expanded.py`: 6 groups per split (2 per suite), 1 clean + 2 goals each.
2. `prepare_thirty.py` (seed `20261102`): 10 groups per split.
3. `prepare_all_groups.py` (seed `20261102`): every group in its owner's split.

| Split | Groups | Clean | Injected | Cases |
|---|---:|---:|---:|---:|
| Train | 19 | 19 | 38 | 57 |
| Validation | 18 | 18 | 36 | 54 |
| Test | 18 | 18 | 36 | 54 |

The two injection goals per group are chosen by a fixed seed from the goals that group has. The SHA-256 of
`all-groups-v1/tasks.jsonl` is `2f5d3943caf7d56d0d50c1998c6a2154f524fb2178ca4119fe89d7b2846d69f4`.
Validation cases serve only to confirm a candidate. Test cases are used only by `hermit eval` and are never shown
to the roles.

## Full TEST variants

Add `--full-test` to `prepare_all_groups.py`, writing to `data/full-test-v1`, to include every frozen case
owned by the existing TEST groups.
This produces 186 TEST cases: 18 clean and 168 injected across the same 18 groups, including the original
54 cases plus 132 additional injected variants. TRAIN (57 cases), validation (54 cases), split ownership and
injection payloads are unchanged. Unselected variants in TRAIN and validation groups remain excluded.
Selection does not use model outcomes. The SHA-256 of `full-test-v1/tasks.jsonl` is
`b529dfa72103fe8c55972e1960dcf492adda2be7dfdd6b1c0a87433007e043fb`.

The Codex EvoSafe recipe uses TRAIN for search and TEST for frozen evaluation, with no validation gate.
The Claude recipe uses the same expanded TEST set and retains its disjoint validation gate.
