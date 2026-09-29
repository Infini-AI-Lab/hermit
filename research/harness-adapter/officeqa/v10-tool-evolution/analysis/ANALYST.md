# v10 adapter analysis: what should the tools and the text of the next harness adapter be?

Error analysis of a completed agent-harness pilot. Four cells ran the same 50 OfficeQA Pro questions with the
GLM-5.3-flash model inside the Codex CLI harness (v9 plumbing), all with the same environment and the same four
table tools (`find_tables`, `show_table`, `printings`, `calc`; usage text in each episode's `workspace/README.md`):

- **a** `text-static`, temperature 0: a ~2.5 KB advice text in AGENTS.md (`workspace/AGENTS.md` of the episode).
  Strict 31/50.
- **b** `mech-static`, temperature 0 (pilot 1): NO advice text (only an 829-byte "how to submit" note); the advice
  was delivered instead as 10 rules executed at `submit` time (findings with "Accept / Keep" offers), plus broker
  loop notes and a post-compaction ledger splice. Strict 20/50.
- **b2** `mech-static`, temperature 0 (pilot 2, stopped after 25 questions): the SAME advice text as a, plus the
  submit section, plus 5 remaining rules (answer_count, later_printing, period_match, recompute_final, scale).
  Strict 13/25 (a: 15/25 on the same 25).
- **c** `text-static` at temperature 1.0 / top_p 0.95: like a, sampled. Strict 24/50.

For every question there is also a **reference solution** (an earlier documented solve by a stronger model that was
graded; `correct.opus` says whether it was right) and the earlier per-question analyses of the v8 runs and of pilot 1.

The next adapter (v10) will own three things and all three will be evolved by a learner: the **text** (AGENTS.md),
the **tools** (the four table tools' code and usage text; new tools may be added), and optionally **controls**
(loop guard, submit checks). The purpose of this analysis is to decide the v10 starting point: which text to keep or
change, which concrete tool changes to make, which controls to keep, and what to drop.

## Materials (read-only)

- `index.json` (this folder): one entry per question: `ordinal` (pilot episode number), `task_id`, `question`,
  `issue47` (the official answer or wording of this question is publicly disputed), `correct` (0/1 per cell a, b,
  b2, c and for `v8_none`, `v8_prompt`, `v8_cards`, `opus`), and per cell under `cells`: run stats, `error_bin`
  (a value-free label of how far a wrong answer is from the reference: `within_1pct`, `1-5pct`, `>25pct`,
  `scale_10^2`, `count_mismatch(...)`, `empty`, `sign_flip`, ...), `digest`, `outline`, `adapter_context`,
  `episode_dir`. Also `opus_logs` (reference solution folder), `v8_analysis` (the v8 divergence analysis of the same
  question, JSON) and `pilot1_analysis` (pilot-1 discordance analysis, JSON, only for 19 questions).
- A **digest** `digests/NNN/<cell>.md`: the readable trajectory of one cell on one question: a header (status,
  elapsed, requests, tokens, compactions, gate rounds, the answer it submitted), the harness inserts the model saw
  (controller cells: loop notes, gate results), then numbered steps `[n] THINK:` (the actor's short progress notes as
  logged by the harness), `[n] SAY:`, `[n] RUN (exit c):` (shell command + clipped output). `<cell>.outline.md` has
  one line per step; for long runs read the outline first, then open specific steps with
  `grep -n '^\[123\] ' <digest>` and `sed -n`.
- The reference solution folder `opus_logs`: `worklog.md` (evidence trail with file:line), `calc.py`,
  `solution.json` / `solution.structured.json` (interpretation, sources, decisions, alternatives, pitfalls),
  `review.md`. **Do not open its `transcripts/` subfolder.**
- The episode directory of a cell (`episode_dir`) if a digest clip hides something: `records/events.jsonl`,
  `workspace/AGENTS.md`, `workspace/README.md` (tool usage the model saw), and for b/b2 `api/checks/NNN.json`
  (every submit: payload, findings, offers, status) and `api/ledger.jsonl`.
- Tool source (to judge what a tool change would take): `../officeqa-pro133-v9-plumbing/packs/officeqa/tools/cli.py`
  and `otables.py`; `../officeqa-pro133-v9-plumbing/harness_core/tools/calc.py`.
- Corpus (to check a claim yourself): `../opus-officeqa-133/corpus/treasury_bulletin_YYYY_MM.txt`.

## Hard rules

- Never open reference answers or graders: nothing under `prepared/private/`, no `answers.jsonl`, no
  `reference_answer.json`, nothing under `evidence/` or `private-evaluation/`, no `reward.py`, no `*-grades.json`.
  Correctness comes only from `index.json`.
- No web access. Write only your own output file(s) named below.
- **Never write a final answer value** (any cell's, the reference solution's, or the correct one) into your output,
  and avoid copying numeric values from the corpus. Describe differences qualitatively ("b took the first printing,
  a the latest revision", "b2's value is ~3% low", "one column to the left"). Step numbers, file names, line numbers,
  years, table ids and relative errors are fine.
- Cite evidence: cell steps as `a[12]`, `b2[7]`; reference evidence as `file:line` or a worklog step.

## For each assigned question write `q/<task_id>.json`

```json
{
  "task_id": "UID....", "ordinal": 0, "issue47": false,
  "correct": {"a": 1, "b": 0, "b2": 0, "c": 1, "opus": 1},
  "question_type": "short label",
  "decisive_step": "the one decision that separates right from wrong on this question (which issue/table/row/column/convention), and where the reference solution made it (file:line or worklog step)",
  "cells": {
    "<cell>": {
      "correct": 1,
      "path_summary": "2-4 sentences: how this cell found (or failed to find) the source and computed the answer",
      "first_divergence": {"stage": "INTERPRETATION|SOURCE_ISSUE_VINTAGE|SOURCE_TABLE|CELL_SELECTION|EXTRACTION|FORMULA|ARITHMETIC|ROUNDING_FORMAT|EXTERNAL_KNOWLEDGE|OPERATIONAL|NONE", "where": "<cell>[n]", "what_it_did": "...", "what_a_right_cell_or_the_reference_did": "..."},
      "saw_correct_evidence": "yes|partial|no (did the right table/row/issue appear in its outputs at any point?)",
      "tool_use": {"find_tables": 0, "show_table": 0, "printings": 0, "calc": 0, "other_shell": 0, "friction": "tool errors, wrong options, pages cut, truncated output, runaway output, loops — with steps"},
      "text_use": "did its notes show it acting on a sentence of AGENTS.md? which one (≤ 15 words)? or 'none'",
      "harness_use": "b/b2 only: loop notes, gate findings, offers accepted/kept, and whether any changed the answer; else 'n/a'"
    }
  },
  "fix": {
    "smallest_change": {"level": "TEXT|TOOL|CONTROL|NONE", "change": "the concrete change, written so an engineer could implement it: for TEXT the sentence to add or rewrite; for TOOL the option/output/new tool and the exact extra line(s) the model would have seen; for CONTROL the runtime behaviour", "would_have_fixed": ["cells it would have fixed"], "could_hurt_right_cells": "yes|no|unlikely + why", "generality": "domain_general|document_qa_general|officeqa_specific", "confidence": "high|medium|low"},
    "alternatives": [{"level": "...", "change": "...", "why_not_first": "..."}],
    "why_not_text": "if the fix is TOOL or CONTROL: why a sentence of text would not have been enough (use cell a, which had the text, as evidence when it applies)",
    "irreducible": "if no adapter change would help (ambiguous question, disputed reference, luck): say so"
  },
  "text_verdict": ["for each advice item of a's AGENTS.md that was relevant here: 'kept-and-helped' | 'ignored' | 'misled' | 'irrelevant', with the item's first words"],
  "notes": "anything else useful for the v10 design (e.g. a tool output that was misread; a page that hid the needed column)"
}
```

Fill `cells` for every cell that ran this question (b2 only for ordinals 1–25), right or wrong. For a right cell,
`first_divergence.stage` is `NONE`. Think hard about `fix.smallest_change`: prefer a change that would have altered
what the model **saw** (a tool output, a line of text) over one that asks the model to behave differently; and say
concretely what it would have printed.

## Final reply

After writing your file(s), reply with a short summary per question: the cells that were wrong, the first-divergence
stage, and `fix.smallest_change` in one line. No answer values.
