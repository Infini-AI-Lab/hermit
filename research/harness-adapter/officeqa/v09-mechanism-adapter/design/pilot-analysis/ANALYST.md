# Pilot postmortem: why did mech-static and the T=1.0 cell lose to text-static?

Error analysis of a completed, sealed and graded pilot of an agent-harness experiment. Three cells ran the same
50 OfficeQA Pro questions with GLM-5.3-flash inside the Codex CLI (v9 harness):

- **a** `text-static`, temperature 0 — L0 environment + L1 table tools (`find_tables`, `show_table`, `printings`,
  `calc`) + a ~2.5 KB seed text in AGENTS.md (`<root>/adapters/text-w0/SEED.md`). Strict 31/50.
- **b** `mech-static`, temperature 0 — same environment and tools, but the seed knowledge is delivered as 10
  executed rules at `submit` time (`<root>/adapters/mech-w0/{rules,policy.json,SEED.md}`; SEED.md is only an
  829-byte "how to submit" note), plus a broker control layer: loop notes, a post-compaction ledger splice, and a
  submit gate that prints findings with computed alternatives ("Accept / Keep"). Strict 20/50.
- **c** `text-static` at temperature 1.0, top_p 0.95. Strict 24/50.

Official go/no-go for the mechanism: NO-GO (findings fired on 57% of right episodes vs 52% of wrong; flips after a
finding: wrong→right 1, right→wrong 2). Temperature rule: T=0.

## Materials (read-only)

- `index.json` (this folder): per question the text, 0/1 correctness for a, b, c, the three v8 arms and Opus, the
  episode directory of each cell, and the path of the earlier v8 divergence analysis for the same question (if it
  exists) under `../../officeqa-divergence-analysis/per-question/`.
- An episode directory `<root>/runs/episodes/NNN/`: `task.json`, `prediction.json` (submitted answer),
  `summary.json` (status, elapsed, requests, controller counters), `records/events.jsonl` (Codex events:
  `item.completed` items of type `reasoning` = the actor's short progress notes, `agent_message`,
  `command_execution` with `command` and `aggregated_output`), `workspace/AGENTS.md` (the context the actor saw),
  and for cell b `api/checks/NNN.json` (every submit: payload, findings, offers, status),
  `api/controller.jsonl` and `api/NNN/controller.json` (loop notes, ledger splices — text the model saw that is NOT
  in events.jsonl), `api/ledger.jsonl` (broker ledger).
- Corpus: `../../opus-officeqa-133/corpus/`.

## Hard rules

- Never open reference answers or graders: nothing under `prepared/private/`, no `answers.jsonl`, no
  `reward.py`, nothing under `private-evaluation/`, no `*-grades.json`. Correctness comes only from `index.json`.
- No web access. Write only your own output files. Never write any final answer value (a cell's answer or the
  correct one) and avoid copying numeric corpus values; describe differences qualitatively or as relative error.

## For each assigned question write `q/<task_id>.json`

```json
{
  "task_id": "...", "correct": {"a":1,"b":0,"c":1},
  "summary": "one paragraph: what separated the right cell(s) from the wrong one(s)",
  "b_vs_a": {
    "applies": true,
    "first_divergence": {"stage": "INTERPRETATION|SOURCE_ISSUE_VINTAGE|SOURCE_TABLE|CELL_SELECTION|EXTRACTION|FORMULA|ARITHMETIC|ROUNDING_FORMAT|EXTERNAL_KNOWLEDGE|OPERATIONAL|OTHER", "where": "b step refs", "what_b_did": "...", "what_a_did": "..."},
    "seed_text_effect": {"a_used_seed_advice": "yes|no|unclear", "which_advice": "quote ≤ 20 words from a's SEED.md or 'none'", "evidence": "a's notes/commands showing it acted on it", "would_b_have_benefited": "yes|no|unclear"},
    "mechanism_effect": {"submit_rounds": 0, "findings": ["rule ids"], "offers_right": "yes|no|none", "b_changed_answer_because_of_gate": "right_to_wrong|wrong_to_right|no_change", "loop_notes": 0, "loop_note_helped": "yes|no|n/a", "ledger_splice": "yes|no", "workflow_cost": "did the submit/Accept-Keep protocol or harness text change b's behaviour in other ways (e.g. stopping early, reopening, confusion)?"},
    "tools_usage": {"a": "which L1 tools, how", "b": "…"}
  },
  "c_vs_a": {"applies": true, "first_divergence": {"stage": "...", "where": "...", "what_c_did": "...", "what_a_did": "..."}, "temperature_effect": "how sampling at T=1.0 plausibly changed the outcome (e.g. different table choice, arithmetic slip, less verification, shorter run)"},
  "lesson": "what this implies for the harness design"
}
```

Set `applies: false` when the two cells agree on correctness for that pair. Reply with a one-line summary per
question.
