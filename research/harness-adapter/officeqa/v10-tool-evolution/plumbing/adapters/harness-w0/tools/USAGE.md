Table tools (read-only): find_tables, show_table and printings print pages of at most 8,000 bytes; one query per call.
  find_tables "<words or table id>" [--years 2003-2004] [--limit 25] [--all] [--title-only]
      find_tables matches every word (prefix for words of 4+ letters) in the title, header or row labels
      (--rows is the default; --title-only searches title and header only); on no full match
      find_tables lists the best partial matches; each group line says which words matched (match:...)
      find_tables prints the group lines first ([FD-1] title | issues | rows | as-of), then the latest 3
      parts per group; --all lists every part (12 per group), --years and --limit narrow a cut page
  show_table <file> <line> [--stitch] [--match <col words>] [--cols 1-3] [--grep <row>] [--period FY2021] [--long] [--raw] [--from-row N]
      show_table takes <file> <line> (not file:line) and prints one table with filled periods (FY/CY/YYYY-MM),
      column labels, repairs and identity checks under show_table's NOTES (read them on every page)
      --grep is a case-insensitive substring of the row label, filled period or section ('|' = OR),
      not a regex and not a column filter; --match keeps the columns whose label contains the words
      --period FY2021 keeps the rows whose filled period falls in that year (12 rows for a fiscal year)
      every page starts 'TABLE <id> | <file>:<line> | part k | page rows a-b of N'; --from-row is 0-based:
      pass the number the continuation line prints; --stitch joins the parts of a multi-part table
  printings --table <id or title words> --row <period or label> [--col <column words or (n)>] [--years A-B] [--section <words>]
      printings lists the same row/column in every issue that prints it, SUMMARY first: revisions, first
      and latest printing; printings has no --all
      printings --row takes the row's period (2003-01, FY2003) or label and --col the column label or (n);
      swap them for a table with periods as columns; printings --section keeps one panel of a stacked table
  Quote multi-word values: show_table ... --match "net receipts", printings ... --col "(3)"
  calc: compute every candidate (both readings, percent and decimal) in one run and pick from its printout
  calc -c '<python>'  |  calc file.py  |  calc <<'PY' ... PY
      calc runs Python 3.12 (numpy, pandas) and prints your code together with its output; the value of a
      final expression is printed by calc, as in an interactive session
