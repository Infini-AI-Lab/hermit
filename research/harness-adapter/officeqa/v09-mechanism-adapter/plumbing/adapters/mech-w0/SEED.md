# Finishing a question

- When you have an answer, run `submit --answer "<answer>"` (add `--alt "<other>"` when you also
  considered another value). The harness checks it and prints either `recorded` or one finding.
- A finding names one possible problem with the submitted answer. When it shows `Offer: <value>`, the
  harness computed that alternative itself, from your own calculation with one input replaced.
  Accept: run the `submit --answer "<value>"` line it prints. Keep: run
  `submit --answer "<your answer>" --keep "<reason>"`. Either is fine; the choice is yours.
- After `recorded`, end your turn with exactly that answer as your final message.
- Lines starting `[harness:...]` come from the harness (loop notes, a summary of your work after a
  context compaction, submit results); they are not part of the task.
