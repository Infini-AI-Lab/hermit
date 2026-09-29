## Task family notes (OfficeQA; a fixed, host-written description of the environment for the learner, never shown to the actor)

- Corpus: 697 U.S. Treasury Bulletin issues, 1939-2025, one parsed TXT file per
  issue (treasury_bulletin_YYYY_MM.txt) with tables flattened into '|'-separated
  rows. Monthly issues until 1982, quarterly after. The same table rows are
  reprinted across several issues; later issues can carry revisions. Extraction
  artifacts exist: shifted year labels, merged rows, flattened multi-level
  headers; printed row and column totals are the way to prove which column is
  which.
- The actor's tools: a shell with python3 and no network, the corpus files, a
  SQLite FTS5 index and the helper officeqa.py (search(query, limit<=20,
  filename_glob), read(filename, start_line, num_lines<=150), list_files).
  Search is an OR over the query words ranked by BM25, so broad phrases hit
  every issue. In the actor's sandbox `python - <<'EOF'` heredocs fail (no temp
  file); `python -c` and script files written in the workspace work; rg is not
  available, grep -n is. The actor has no web access; whether a series the
  question names exists in the corpus is something you can check with
  search_corpus.
- Grader: the official reward.py at tolerance 0. The answer's numbers must
  equal the reference numbers at the reference's own rounding. Tolerated:
  thousands separators, $ signs, unit words after a bare number, trailing
  zeros. Graded: percent versus decimal form, the order of a bracketed list,
  the count of numbers, one line of at most 250 characters. The LAST agent
  message is the submission: a progress note as the last message is graded as
  the answer. A timed-out or empty submission scores 0.
- The benchmark's stated conventions: maximum precision unless the question
  specifies; when several documents report the same metric, the most
  up-to-date revision unless the question specifies an exact date.
- Diagnosis ladder, name the first rung that failed: S1 issue and vintage
  (which issue the row comes from; whether a later issue revises it); S2
  table or section; S3 row, column, units (millions vs billions, fiscal vs
  calendar year, percent vs percentage points); S4 the extracted cells (count
  them); S5 formula or convention (write the convention next to the number,
  enumerate the alternatives, match the question's wording); S6 rounding and
  format; S7 runtime (timeout, repeated identical commands, progress note as
  final message, no completed turn).
