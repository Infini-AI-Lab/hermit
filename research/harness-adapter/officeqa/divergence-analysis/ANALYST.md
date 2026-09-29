# Failure analysis: GLM-5.3-flash + Codex CLI runs on OfficeQA Pro

You are doing error analysis of an agent-harness evaluation. Three independent runs ("arms") of
the GLM-5.3-flash model inside the Codex CLI harness answered the same questions over a corpus of
697 U.S. Treasury Bulletin issues (TXT files, one per issue, plus an FTS5 search helper
`officeqa.py`). For every question you also get a **reference solution**: a documented solution
file set (sources with file:line, the calculation script, the decisions taken, a reviewer's check)
from an earlier solving run that was graded correct (or, for Type R, graded wrong). Your job is to
find, for each wrong GLM run, the first step where it departed from a correct solution, why, and
which harness mechanism (context text, tool, automatic check, control flow, or cross-question
memory) would most plausibly have prevented the failure for this actor.

## Materials (read-only)

- `index.json` in this folder: one entry per question with the question text, 0/1 correctness for
  `opus_final`, `none`, `prompt`, `cards`, run stats, `error_bin` (a coarse, value-free label of how
  far a wrong GLM answer is from the reference: `within_1pct`, `1-5pct`, `>25pct`, `scale_10^2`,
  `count_mismatch`, `empty`, …), `issue47` (the question is on the public list of questions whose
  official answer or wording is disputed), and paths:
  - `opus_logs`: the reference solution folder: `worklog.md` (evidence trail with file:line),
    `calc.py`, `solution.json` (interpretation, sources, decisions, alternatives considered,
    pitfalls, confidence), `review.md`/`review.json` (a reviewer's check), `final.txt`.
    **Do not open its `transcripts/` subfolder** (raw session logs, not needed and not in scope).
    Every folder also has `solution.structured.json` (the solver's structured result: interpretation,
    key sources, key decisions); use it when `solution.json` or `worklog.md` is missing (13 folders).
  - per arm `digest`: a readable trajectory of the GLM run: numbered steps `[n] THINK:` (the actor's short
    progress notes as logged by the harness), `[n] SAY:`, `[n] RUN (exit c):` (shell command + clipped output).
    The header gives status, elapsed time, request count and the final answer it submitted.
    `digests/NNN/<arm>.outline.md` has one line per step; for long runs read the outline first,
    then open specific steps with `grep -n '^\[123\] ' <digest>` and `sed -n`.
  - per arm `adapter_context`: the exact `AGENTS.md` the actor saw for that question.
    - `none`: only the base task instructions (no adapter).
    - `prompt`: base instructions + a learned text document (procedure, conventions, and many
      `L-...` lessons written by a text-only learner after each block of 5 graded questions). It
      grows to ~230 KB; **grep it** for the table/series/concept at hand instead of reading it whole.
    - `cards`: base instructions + a deck of "cards" (C0NN: scope / claim / unless / support), capped
      at ~16 KB, shown whole on every task.
- Arm roots (for anything else you need about a GLM run, e.g. `records/events.jsonl` if a digest
  clip hides something): `../officeqa-pro133-{none,prompt,cards}-v8-work/runs/{baseline|adapted}/NNN/`.
- Corpus (to check a claim yourself): `../opus-officeqa-133/corpus/treasury_bulletin_YYYY_MM.txt`.

## Hard rules

- Never open reference answers or graders: nothing under `prepared/private/`, no `answers.jsonl`,
  no `reference_answer.json`, nothing under `evidence/` or `private-evaluation/`, no `reward.py`.
  Correctness comes only from `index.json`.
- No web access of any kind.
- Write only your own output files (named below). Do not modify anything else.
- **Never write a final answer value** (Opus's, GLM's, or the correct one) into your output, and
  avoid copying numeric values from the corpus. Describe differences qualitatively ("GLM took the
  June 1965 printing, Opus the latest revision in the 1968 issue", "GLM's value is ~3% off").
  Step numbers, file names, line numbers, years, table names and relative errors are fine.
- Be concrete and evidence-based: cite GLM steps as `[n]` in the named arm's digest and Opus
  evidence as `file:line` or worklog step. If something is uncertain, say so.

## Type A task: reference solution correct, at least one GLM arm wrong

(In the schema below, "opus" fields refer to the reference solution.)

Write `per-question/NNN.json` (NNN = zero-padded ordinal) with this shape:

```json
{
  "ordinal": 0, "task_id": "UID....",
  "question_type": "short label, e.g. 'CAGR of agency outlays across fiscal years'",
  "skills_required": ["from: locate_issue, pick_vintage, locate_table, read_flattened_table, multi_value_extraction, unit_or_scale, date_or_period_logic, external_knowledge, formula_finance, formula_stats, multi_step_arithmetic, rounding_format"],
  "opus_path": [{"stage": "interpret|choose_source|locate_table|pick_cells|extract|compute|round_format|verify", "decision": "what Opus decided and why", "evidence": "file:line or worklog step"}],
  "opus_key_moves": ["the 1-3 moves that made Opus right (e.g. 'checked every later issue for revisions and took the latest', 'verified column alignment by summing components')"],
  "arms": {
    "<arm>": {
      "correct": true,
      "error_bin": "copied from index.json",
      "first_divergence": {
        "stage": "OPERATIONAL|INTERPRETATION|SOURCE_ISSUE_VINTAGE|SOURCE_TABLE|CELL_SELECTION|EXTRACTION|FORMULA|ARITHMETIC|ROUNDING_FORMAT|EXTERNAL_KNOWLEDGE|OTHER",
        "glm_step": "[n] or range",
        "what_glm_did": "…", "what_opus_did": "…", "why_glm_went_that_way": "what its logged notes/commands show about the choice, if anything",
        "consequence": "how it produced the wrong answer"
      },
      "secondary_errors": [{"stage": "…", "glm_step": "[n]", "note": "…"}],
      "saw_correct_evidence": {"value": "yes|partial|no", "where": "[n] … (did the right table/row/issue appear in its outputs at any point?)"},
      "glm_expressed_doubt": {"value": "yes|no", "where": "[n] …"},
      "operational": {"timed_out": false, "no_answer": false, "loop_or_thrash": "none|mild|severe", "tool_friction": "e.g. heredoc failures, rg missing, huge outputs", "notes": "…"},
      "adapter_context": {"relevant_item": "lesson/card ids that apply to this question, or 'none' (for the none arm: 'n/a')", "item_was_correct_guidance": "yes|no|partly|n/a", "followed": "yes|no|partly|n/a", "caused_or_contributed_to_error": "yes|no|possibly|n/a", "notes": "…"},
      "remedies": [{"level": "TEXT|TOOL|CHECK|CONTROL|STATE", "intervention": "the smallest concrete mechanism that would have prevented this failure", "generality": "domain_general|document_qa_general|officeqa_specific", "confidence": "high|medium|low", "why": "evidence that it would work for THIS actor (e.g. 'the prompt arm already had this lesson as text and ignored it at [n]')"}]
    }
  },
  "right_arms_note": "for arms that were right: what they did differently at the divergence point (or 'same path, lucky fork')",
  "text_vs_mechanism": "one or two sentences: could a better *text* instruction alone plausibly have fixed the wrong arms, or does this need a mechanism (tool/check/control/state)? Use the prompt arm as evidence: did it have relevant text and still fail?",
  "notes": "anything else useful for harness design"
}
```

Include an entry under `arms` for **every** arm, right or wrong (for a right arm, fill `correct`,
`error_bin: null`, and a one-line `right_arms_note` instead of the divergence fields).

Remedy levels:
- **TEXT**: an instruction, convention or lesson placed in the actor's context.
- **TOOL**: a new or better callable capability (e.g. a table extractor that returns a clean grid
  with headers, a "find all printings of this row across issues" lookup, a calculator/format tool).
- **CHECK**: an automatic verifier that runs on the actor's intermediate or final output and
  returns a signal (unit/scale sanity, recompute from cited cells, cross-issue consistency, answer
  format against the question's requested format, components-sum-to-total).
- **CONTROL**: a change to the control flow of the episode (mandatory stages such as
  plan → locate → extract with citation → compute in a script → verify → commit; a second
  independent attempt and reconciliation; forced commit before timeout; budget management;
  a reviewer pass).
- **STATE**: memory that persists across questions and is retrieved by relevance (e.g. an index
  of where a series lives by year range, learned from earlier questions).

## Type R task: reference solution wrong, at least one GLM arm right

(In the schema below, "opus" fields refer to the reference solution.)

Write `reverse/NNN.json`:

```json
{
  "ordinal": 0, "task_id": "UID....", "issue47": false,
  "question_type": "…",
  "opus_divergence": {"stage": "same stage taxonomy", "what_opus_did": "…", "what_right_glm_did": "…", "evidence": "Opus file:line / worklog step; GLM [n]"},
  "opus_confidence": "high|medium|low (from solution.json)", "reviewer_flag": "what the reviewer said (verdict/uncertain?)",
  "right_glm_arms": ["…"], "right_by_principled_choice_or_luck": "principled|luck|unclear, with reason",
  "wrong_glm_arms_same_as_opus": "did the wrong GLM arms make the same choice as Opus?",
  "lesson_for_harness": "what this teaches (e.g. the 'latest revision' default is wrong when …; or the official answer is disputed so ignore)",
  "notes": "…"
}
```

## Final reply

After writing your files, reply with a short plain summary: for each question, one line with the
first-divergence stage of each wrong arm and the top remedy level. No answer values.
