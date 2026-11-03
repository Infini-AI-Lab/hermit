# Evidence Recovery Procedure

Use this procedure only when the requested operand, plotted value, or external metadata is not present in the
text currently being read. It is a bounded recovery pass, not permission to infer missing values.

1. State the missing item precisely: its measure, entities, interval, and required representation. Keep the
   source identity and semantic endpoint audit from the main instructions in view.
2. Search the local corpus for an alternate representation using two distinctive terms from the task and one
   source-context term. Prefer a table or a second issue that contains the same labeled series. Read a small
   surrounding span and verify its headers, unit, period, and revision before using it.
3. If the candidate source is a local PDF or image and the needed value is visual, inspect the local file with
   an available geometry-preserving command (`pdfinfo`, `pdftotext -layout`, or `pdftoppm`, in that order as
   applicable). Use only commands that exist; do not spend time installing tools or attempting network access.
   For a chart, do not replace plotted observations with nearby prose or a hand-written example array.
4. For metadata absent from the corpus, search local bibliographic entries and neighboring source records for
   the exact identifier and title. Treat an inferred date or rate as unverified unless a local source states it.
5. Stop after this one alternate-representation pass. A value is usable only if every operand has a source
   location and matching label, period, unit, and role. Otherwise do not calculate from a guessed, partial, or
   proxy dataset; return to the task and answer only from evidence that is actually supported.

Before the final output gate, re-open the supporting source span or image once and confirm that the recovered
item is the requested one. This verification is bounded to one pass and must not delay final serialization.
