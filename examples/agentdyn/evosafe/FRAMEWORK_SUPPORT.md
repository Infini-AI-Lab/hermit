# EvoSafeHarness and the Hermit core

EvoSafeHarness changes nothing in `hermit/` outside its template (`hermit/adapter/templates/evosafe/`) and its
Evolver (`hermit/agent/evolver/methods/evosafe/`). The training loop (rollout → material → one Evolver call →
check/commit) is unchanged. This note records how the method fits the existing interfaces, and what could later
become framework support. Nothing here is required to run it.

## How the method fits the existing interfaces

| Need | Where it lives |
|---|---|
| Per-episode defense verdicts `{kind, utility, attack_success}` | The judge returns them as `metrics` in `JudgeResult.extra`; the run record keeps them under `judge.extra.metrics`. The scalar `score` is only a single-number view and is not used by the method. |
| Defense metrics with separate denominators (clean utility, ASR, objective, injected utility, secure completions, errors) | `contract.py` computes them from run records: `aggregate` (strict) and `summary` (counts failed runs) in the Evolver, and `examples/agentdyn/utils/report.py` / `compare.py` on `hermit eval`'s `results.jsonl`. `hermit eval`'s `mean_score` is not a defense metric. |
| A failed episode keeps its usage and log | The bridge returns a `RunOutput` with `{"episode_error": ...}`, its tokens, cost and log; the judge raises on it, so the Trainer records a failed run (`status: judge_error`) that is metered and keeps its log. A failed run is never a verdict. |
| Victim and classifier accounting | Accumulate the shared `Result.usage` token counts (including cached input) and `cost_usd`; the bridge never reprices tokens. `usage.json.cost_usd` is the known subtotal, with `unknown_usage_calls` and `unknown_cost_calls` counters. An incomplete episode total is reported as unknown to the Trainer; complete totals include the classifier exactly once. |
| Structured role answers from Claude Code or Codex | `cli_roles.py` calls the `claude` / `codex` Base Agent through `make(...).run(...)`. Codex gets `--output-schema` and `--ephemeral` through the Base Agent's `args` setting; Claude gets `--tools ""` (no built-in tools) the same way and `persist_session: false`. The reply is validated against the role's JSON schema in Layer 3. |
| Provider refusals stop a role without retry | Read from the reply and the trajectory's error steps (never from the raw CLI log). |

## Candidates for framework support (open for discussion)

1. **Task-level metrics.** An optional judge-level aggregate, recorded next to `mean_score` in step records,
   learning material, `run_agent` results and `hermit eval`'s summary, would make a defense objective visible
   without a separate report script.
2. **Usage of a failed pipeline run.** A pipeline exception that carries usage could keep it in the run record,
   instead of a task routing the failure through its judge.
3. **Structured output in CLI Base Agents.** An `output_schema` setting for `codex` and `claude` (the reply as
   the structured JSON, validated) and normalized refusal/failure flags in `Result.extra`, so methods need no
   CLI-specific arguments.
4. Records of several candidate trials within one step: the Evolver archives them in `output/`.
5. Repeated and paired evaluation: `run_agent` has no `repeats`, so the Evolver loops over it.
6. A validation gate owned by the framework: the Evolver runs validation and decides promotion itself.
7. Logs for validation runs: `run_agent` withholds them by design, so a validation failure cannot be diagnosed
   from its trajectory.
8. A framework-owned episode deadline for direct functions: `agent.timeout_min` does not stop such a call.
   This task now enforces `episode_timeout_s` in its subprocess supervisor; learned hooks run in a separate
   container. See `REPRODUCTION.md`.
