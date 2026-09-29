# Working advice

- Loops: a command repeated with the same output will not change. If a command already showed you its
  output, do something different (another table, file, period or wording) or answer.
- Compute every final number with `calc`, not in your head, and answer with the value it printed,
  rounded as the question asks.
- Provenance: use numbers you have seen printed by a tool. Before you rely on a value, print the line
  it comes from, so the number in your calculation is the number in the source.
- Answer count and format: give exactly as many values as the question asks for, in the order and the
  format it asks for (for example a bracketed list when it asks for one).
- Percent or fraction: when the question asks for a percent, answer in percent (12.5, not 0.125);
  when it asks for a ratio, fraction or decimal, do not multiply by 100.
- Alternatives: when two candidate values fit (two tables, periods, printings or readings), weigh them
  against the question's exact wording and answer the one it asks for; do not average them.

OfficeQA table notes:
- Bulletins revise earlier numbers. Unless the question names a publication date or issue, use the latest printing of each value: for every table cell you use, run `printings --table <id> --row <period> --col <column>` and take the value printed by the latest issue listed.
- Match the period exactly. FY (federal fiscal year: July-June through FY1976, October-September from FY1977) and CY (calendar year) rows are different rows even with the same year number. show_table fills the period of every row (FY/CY from the banners); check that the period of each cell you use is the one the question asks for.
- show_table marks rows it repaired (SHIFT+1, SPLIT, ROWSPLIT), month rows whose year is not printed (Y?), and rows where the table's total identity FAILS. If a value you use sits on such a row, compare it with another printing (printings) before using it. r/p marks (revised/preliminary) alone are not a problem.
- Several tables in one issue can look alike. Search with find_tables using the question's key nouns and prefer the table whose title and column headers match the question's wording; note its id so printings can follow it across issues.
- For a sum over the columns of one row, list the whole row (show_table <file> <line> --long --grep <period>) and decide for each column whether the question includes it; compute the sum with calc from the printed cells, not from memory.

# Finishing a question

- When you have an answer, run `submit --answer "<answer>"`. The harness checks it and prints either
  `recorded` or one finding.
- A finding names one possible problem with the submitted answer. When it shows `Offer: <value>`, the
  harness computed that alternative itself, from your own calculation with one input replaced.
  Accept: run the `submit --answer "<value>"` line it prints. Keep: run
  `submit --answer "<your answer>" --keep "<reason>"`. Either is fine; the choice is yours.
- After `recorded`, end your turn with exactly that answer as your final message.
- Lines starting `[harness:...]` come from the harness (loop notes, a summary of your work after a
  context compaction, submit results); they are not part of the task.
