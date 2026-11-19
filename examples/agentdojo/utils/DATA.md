# AgentDojo frozen comparison-v1

This is a local fixed-case protocol for comparing SHE and EvoSafeHarness on the
same task set. It is **not** an official AgentDojo leaderboard result or a direct
numerical comparison with AgentDyn's different tasks and payloads.

## Source and preparation

- Official source: `ethz-spylab/agentdojo` at
  `089ed468cf3ed0322acc66b0211f26d9d90dbf60`; native task version `v1.2.2`.
- Existing log collection: `runs/gpt-4o-2024-05-13`, attack family `tool_knowledge`.
  All 629 recorded goal cases are considered, irrespective of their old outcomes.
  No attack-generation code runs. Only suite/task/goal IDs and frozen payloads are
  imported; prior trajectories, responses, utility and security values are removed.
- Native audit reuses AgentDyn's ID/vector/environment/no-op checker, parameterized
  for the four official suites. It constructs fresh native environments and checks
  local predicates without model calls. Of 629 cases, 584 are eligible. The other
  45 (five banking groups, nine goals each) already satisfy the native user utility
  predicate before any action in both clean and injected environments. They are
  excluded before group ownership is assigned. This structural check is not a
  certification that every semantic goal/payload pairing is effective.

The historical family covers nine banking, five Slack, seven travel and six
Workspace goal IDs. The pinned native registry has 14 Workspace injection tasks;
unrecorded goals are not synthesized. “All goals” below means all eligible
**recorded** goals for the held-out groups, not the entire current registry.

From the repository root:

```bash
python examples/agentdojo/utils/prepare_benchmark.py \
  ../agentdojo/runs/gpt-4o-2024-05-13 examples/agentdojo/utils/data/comparison-v1
```

The command refuses to overwrite an output directory. It writes task rows and
split ID files, a source audit, and a manifest with hashes, native inventory,
source file paths/hashes and group ownership. Generated cases and audit logs stay
ignored; public tool schemas in `task-info/tools.json` are reusable interfaces.

## Group ownership and budgets

Seed `20261117` shuffles user groups independently per suite. TRAIN and reserved
validation allocate 19 and 18 groups by largest remainder, in proportion to the
number of groups per suite with at least two eligible goals. Each split must
represent every suite. TEST gets every remaining eligible group, including groups
with only one eligible goal. Every clean case and injection variant for a user task
has one owner; no variant can cross splits. Data preparation never uses model outcomes.

TRAIN and validation retain one clean execution and two rotated existing goals
per group, matching AgentDyn's 57/54 development-case budgets. TEST retains one
clean execution and **all** eligible recorded goals for each of its groups.
Cases are interleaved across suites so the first 12-case EvoSafe cascade covers
all four domains. For a group/goal, the lexically first case hash selects a frozen
variant independently of outcomes.

| Suite | TRAIN clean / injected | Reserved validation clean / injected | TEST clean / injected |
|---|---:|---:|---:|
| Banking | 2 / 4 | 2 / 4 | 7 / 63 |
| Slack | 5 / 10 | 4 / 8 | 12 / 60 |
| Travel | 4 / 8 | 4 / 8 | 12 / 84 |
| Workspace | 8 / 16 | 8 / 16 | 24 / 144 |
| Total | **19 / 38** | **18 / 36** | **55 / 351** |

The task JSONL SHA-256 is
`d50945a77e81afb21a7556a10b29d1989a99c4822eabd45f81c4d67013f0d65e`.
Both Codex recipes select on TRAIN only and leave validation unused, following
the corresponding AgentDyn Codex protocols. TEST is evaluated only with frozen
`none`, `root` and `final` adapters after search. This preserves split separation;
it does not erase provider pretraining exposure to this public benchmark.

## Metrics

Use native user-task utility and injection-task success predicates unchanged.
Report clean utility, ASR, raw injected utility and safe injected completion with
their own completed-case denominators. Count all execution failures and planned
cases separately. Complete coverage means every planned case has a record; it
can still contain errors. The complete-run defense objective is unavailable when
any case fails. Marginal Wilson intervals are descriptive: shared user tasks and
one-run variation invalidate independent-trial significance claims.
