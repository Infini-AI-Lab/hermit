# OfficeQA corpus table formats and the v9 table-tool prototype

Scope: the 697 Treasury Bulletin TXT files in `opus-officeqa-133/corpus/` (these are byte-identical to the v8 agent
corpus `officeqa-pro133-*-v8-work/prepared/agent/corpus/`; I checked 3 files with `cmp`). The prototype lives in
`understand/tables_proto/` (`otables.py`, `run_cases.py`, `sweep.py`, outputs in `out/`, index in `cache/`).
Nothing here contains answer values. The `out/*.txt` renderings contain raw corpus cells; `printings` outputs are masked.

Terms used below:
- **OCR** (Optical Character Recognition): the scan-to-text step that produced these files. Most damage described here comes from it.
- **FY / CY**: fiscal year / calendar year.
- **p / r flags**: *preliminary* / *revised* markers printed next to numbers.
- **Vintage / printing**: the same cell as printed in a given issue. Values get revised in later issues.
- **FTS5**: SQLite's full-text search engine, already used by the v8 `officeqa_tools.py`.

## 0. Summary

1. **Every table in every era is a pandas `to_markdown` pipe table.** Each one is a single header line, a `| --- |`
   line and the body. There are no fixed-width tables. The era differences are in titles, header flattening, period labels and
   OCR damage, not in delimiting. The pandas artifacts are:
   - `nan` for empty cells;
   - `Unnamed: k_level_j` for empty header cells;
   - ` > ` joining multi-row headers;
   - `.1`/`.2` suffixes on duplicate labels (side-by-side panels);
   - `2085.0` float coercion;
   - thousands commas stripped, except in cells that also carry a flag or text.
2. **Table identity changes by era.**
   - 1939–43: free-text titles.
   - 1944–mid-1969: `Table N.- Title`, numbered within a section, so an id is only unique together with its section.
   - From 1969_09: coded ids such as `FFO-3`, `FD-1`, `CM-I-3`.
   - ids suffer OCR damage: `FF0-` (418 title lines), `1FS-`, `CM-11-1`, `FCP-V11-2`.
   - Issues 1999_12–2001_06 split words into syllables (`TA BLE FD-1.—Sum mary of Fed eral Debt`). A plain grep for a title misses them.
3. **Year labels are printed only on the first row of each year block** (`1938-May`, `June`, … `1939-Jan.`).
   Variants:
   - newest-first order (OFS-2);
   - years printed only in column headers or in separate year-label rows;
   - years lost entirely (AY-1 1970_06);
   - FY and CY annual rows under section banners, with identical labels.
   Fill-down has to handle all of these, and must say when it is guessing.
4. **The prototype's `find_tables` / `show_table` / `printings` tools**
   - pass 17 and partially handle 4 of 21 structural test cases built from the failure analysis (Q8–Q130);
   - reproduce Opus's vintage findings exactly for Q81 (4 printings) and Q86 (3 identical + 1 revised);
   - flag the OCR misreads Opus found for Q17 through the row-total identity;
   - flag the label slide that sank GLM on Q30.
   - Partial only: header realignment for MQ-1 and the TSO spurious label (warns but does not fix), garbled year labels in 1972 PDO-1, and a fused footnote+flag+value cell (FFO-5 1981_12).
   - Robustness sweep: `analyze()` ran over 12,806 tables (the first issue of every year) with 0 crashes.

## 1. Encoding common to all eras (verified on 11 sampled issues and a corpus-wide census)

| Feature | Evidence |
|---|---|
| Delimiting | Every table is a contiguous block of lines starting with `\|`. Line 2 is always `\| --- \|…`, and all rows have the same cell count. The survey of 1939_06, 1944_03, 1952_04, 1960_06, 1968_06, 1975_03, 1984_03, 1995_03, 2005_03, 2016_03 and 2024_03 found `nosep=0` and `ragged=0`. |
| Empty cells | `nan` (4–12% of cells per decade). `-` means zero/none. `*` means less than half a unit ("Less than $500,000"). `n.a.` means not available. |
| Header flattening | Multi-row PDF headers are collapsed into one line with ` > `, e.g. `Defense Department > Military \| Defense Department > Civil` (treasury_bulletin_1985_06.txt:465). Empty header cells become `Unnamed: 3_level_1`. Parents are repeated: `Fiscal year or month > Fiscal year or month`. |
| Side-by-side panels | Duplicate labels get `.1`, `.2` suffixes: `Date \| … \| Date.1 \| … \| Date.2` (1944_03:3985), `Treasury bonds 1/.1 … .3` (1970_06:4946). |
| No header detected | The header is `\| 0 \| 1 \| 2 \|`. It is usually a continuation fragment after a page break or a caps sub-heading: 90 of the 349 tables in 1984_03 (e.g. :46, :142, :2589). |
| Float coercion | Columns that had a blank become floats (`2085.0`, `3.2` for printed `3.20`). This is 24% of cells in the 1930s and 4–16% later. Trailing zeros are lost, so precision cannot be read off the text. |
| Commas | Plain numbers have their commas stripped. Comma-formatted numbers without a flag or other text are ~0% of cells in every decade. A comma therefore signals a cell pandas kept as text: `r 16,311`, `1,844 1/2`, `$4,723`. |
| Titles / units | Plain lines above the table: an id or title line, an optional subtitle, then units `(In millions of dollars)` or, in modern issues, `[In millions of dollars. Source: …]`. Footnotes (`1/ …`, `* Less than…`, `r Revised.`, `p Preliminary.`, `Source:`) are plain lines after the table. |
| Page furniture | There are no explicit page markers. Page numbers are standalone integers, 47–160 per issue (`-1-` in 1939). Running heads are ALL-CAPS section names (`FEDERAL FISCAL OPERATIONS`), `Treasury Bulletin`, and month-year footers (`March 2016`, 2016_03:63). A "page" must be inferred from the nearest standalone integer. This matters for Q126 ("pinned report page"). |
| TOC tables | The table of contents (TOC) is itself a pipe table (`\| CM-IV-3. - Total liabilities … \| 153.0 \|`, 1984_03:130). It makes up 4–5% of tables from the 1970s on and must not be mistaken for data. |
| Junk | One hallucinated off-domain table, `\| Variable \| Baseline \| 2 Years \| 4 Years \|` with medical rows (1939_06:88). A "Thank you for your order!" order form also appears as a table in some 2010s issues. |

## 2. Era by era

Coverage figures come from the index over all 95,053 tables: `cache/tables.sqlite`, computed in section 3.

### 1939–1943: untitled, pre-numbering (84% of tables have only a plain title line)
- **Titles**: free text above the table, e.g. `Monthly Receipts, Expenditures and Excess of Expenditures` followed by `(In millions of dollars - on basis of daily Treasury statements, unrevised)` (1939_06:198-200).
- **As-of dates**: in subtitles such as `As of April 30, 1941` (1941_05:953).
- **Year fill-down**: `1938-May`, then `June` … `Dec.`, then `1939-Jan.` (1939_06:241-249, 281-289). Year×month matrices put years in rows and months in columns.
- **Header damage**:
  - Level-0 spans are misaligned, e.g. `Receipts > Total | Receipts > Total.1 | … | Expenditures £/ > Customs`. Customs is really a receipts column, and `£/` is an OCR'd `1/` (1939_06:232).
  - The `Excess of receipts` label sits at c12 while its data is in c14.
- **Footnotes**:
  - `1/`, `2/`, `(*)`, `*`.
  - Footnote plus a stray digit: `1,844 1/2` (1939_06:305) and `378 1/2`. This collides with genuine fractions in security names (`2-1/2%`).
- **Year-group rows**: in maturity schedules a bare row `| 1941 | nan | … |` heads a block (1941_05:959).
- **OCR damage**: spelling (`Expendituree`, 1939_06:211), a hallucinated table (1939_06:88), and stacked-row packing in some cells (`Mar. 15, 1948 Mar. 15, 1950`, 1941_05:942).

### 1944 – mid-1969: `Table N.- Title` numbered within sections (43% of 1940s tables, 90% of 1950s, 84% of 1960s)
- **Ids**: `Table 1.- Summary of Federal Securities`, unique only with the ALL-CAPS section running head.
- **As-of dates**:
  - in section heads, e.g. `TREASURY SURVEY OF OWNERSHIP, JANUARY 31, 1962` (1962_03:3369);
  - in subtitles, e.g. `…Outstanding February 29, 1972` (1972_03:2078).
- **Flattened multi-panel tables (the core difficulty)**:
  - 1944_03:3985-4074: three panels of `Date | values…` side by side, with the monthly, weekly and daily sections in different layouts.
    - Panels 1–2 of the monthly block carry 2 values under a 3-label header group, so a column is missing and label mapping is ambiguous.
    - The right panel's `1943-Jan.` label slid into the middle panel (:4031). This is Q30's failure.
  - 1956_07:2598/2603: the sub-label is shifted left onto the period column: `Period > From 10 to 20 years 3/ | Taxable… > and after | Moody's…`. This is Q62's failure.
  - 1956_08:3176-3177: the correct header is printed as two in-body rows.
- **Merged banners**: section banners fill every cell (`| Monthly Series 4/ - (continued) | Monthly Series 4/ - (continued) | …`, 1944_03:4021).
- **Flags**: suffix `r` (`2,659r`, `1r`, `92r`, 1968_06:433-460), `(Est.)` rows, `Fiscal 1968 to date`, and legend lines `r Revised.` / `p Preliminary.`.
- **Leader dots**: `1958-June.....`, `Dec.........` (1962_03:3340).
- **Months × calendar-year matrices** (Q17: `Table 6 … Summary by Months and Calendar Years`, 1950_02:369, header misspelt `Calender year`). OCR misreads show up only through the `Total` column.

### 1969_09 – 1982: coded ids `Table FFO-3. - Title` (83% of 1970s tables carry an id)
- **Coded ids start abruptly in 1969_09.** Numbered `Table N. - Title` titles continue through 1969_08: 110-180 per issue in 1968_01–1969_08, and 0 coded ids.
- **AY-1 (Q11/Q18)**:
  - 1970_06:4946-4984 has 4 column groups (Treasury, Aa corporate, municipal) × 3 stacked Jan..Dec blocks, and no year labels at all.
  - The sibling 1970_05:4848 has years in the header (`… > 1959`, `1962`, `1965`, `1968`) and year-label rows shifted by one column (`| 1960 | 1960 | 1960.0 | 1963.0 | 1963 | …`, :4862).
  - Its real column names are in a separate header-only block just above (title :4843, header :4845).
- **PDO-1 1972_03:2076-2125 (Q9)**:
  - two panels;
  - 30+ rows slid one column left (description in the period column, last cell `nan`);
  - garbled year labels (`1976` on 1972 rows);
  - garbled amounts (`(2,506`).
- **MQ-1 1982_03:4363 (Q15)**: header labels sit one column right of the data after a phantom `Unnamed` column. The column labelled `Maturity date` holds percentages.
- **Merged cells**:
  - Two numbers in one cell (`9,999 9,999`) with a trailing `nan`: a column merge.
  - Whole stacked rows, where two table rows are packed into one line: `| 1972-July 13 | Oct. 12 1973-Jan. 11 | 91 182 | 3,524 3,040 |…` (PDO-3, 1975_03:2191). These make up 2.4% of rows in the 1960s and 2.8% in the 1970s.
- **A row falling out of the table**: FFO-5 1981_12:916 (Q88) is a plain-text line, `Net budget outlays 2/1dd,ddd dd,ddd …` (digits masked here: an input cell of Q88). In the same table `r` was OCR'd as `1` (`1-156`, :911).
- **Also**: ditto marks `do`, `-*`, `n.a.`.

### 1983 – 1998 (quarterly): `TABLE FFO-1.--Title` / `Table FFO-3.` + `— Title` on the next line
- **Split titles**: `Table FFO-3.` / `— Budget Outlays by Agency` (1985_06:459/461).
- **Multi-level headers** with parents spanning sub-columns (`Defense Department > Military | … > Civil`).
- **Numeric-default headers**: continuation fragments of balance-sheet tables split by caps sub-headings (`GOVERNMENT EQUITY`, 1984_03:2589). The sweep flagged a missing header on 27% of 1980s tables.
- **1987_09 FFO-3** puts both column bands in ONE pipe table, with the second band's header as in-body rows (:640-663). This caused a real bug in my prototype, now fixed and tested (section 5).
- **Column numbers** `(1)`…`(37)` appear in headers from the 1990s on. They are the most stable column key across issues.

### 1999_12 – 2001_06: syllable-split OCR (7 issues, 95–118 hits each)
- Examples: `TA BLE FD-1.—Sum mary of Fed eral Debt` (2000_09:953), `[In mil lions of dol lars…` (:955), `End of fis cal year`, `Cal en dar year` (2000_12:3043), `Lia bilities` (2001_03:2936).
- Titles, units and header words defeat exact grep and FTS token search. They need space-insensitive matching.

### 2001 – 2025: `TABLE FFO-3.—Title`, `[In millions of dollars. Source: …]`
- **Flags**: prefix flags `r 16,311`, `r-144,112`, `r 1,655,210` (2004_03:730-749). Headers are numbered `(1)…(37)`.
- **Multi-part tables**: FFO-3 is split into 13/12/12-column parts: title :724 with header :728, untitled part :751, and `…, con.` title :780 (all in 2004_03).
- **Period rows**: `2002 - Dec`, `2003 - Jan`, `Feb`, … `Dec` (months after `Sept` are still 2003), then `Fiscal year 2004 to date`, and `2004 - Est` rows.
- **Newest-first order**: OFS-2 2013_03:1855-1866 runs `2012 - Dec, Sept, June, Mar, 2011 - Dec, …`. A naive fill-down labels the `Sept` under `2012 - Dec` as 2013-09.
- **Transposed capital-movement tables** (Q13/Q89):
  - Column headers such as `Calendar Year 2011 r | June r | … | Dec. p` (2013_03:2247) print the month columns without a year, and here the months are 2012.
  - `Cal en dar year > 1999 r | 2000 > May | …` (2000_12:3043) prints the year on the parent level.
  - `1998 | 1999 | 2000 r | May r | June | …` (2001_12:3267) implies 2001 months.
  - Q89's prompt arm read one column right. Its cards arm took `Sept. p` as June.
- **Footnote markers are bare digits**: `Department of Homeland Security 1 (9)`, `Special drawing rights 1`, `(99,999)9`.
- **ESF-1 2012_06:4722**: the units line was absorbed as level 0 of every header cell. Balance-sheet date columns look like `Sept. 30, 2011 | Oct. 1, 2011, through Dec. 31, 2011 | Dec. 31, 2011`. Indentation is lost, so whether `Accounts receivable` belongs to `Foreign exchange and securities:` cannot be recovered. That is exactly the Q39 ambiguity.
- **Table renumbering over time**: e.g. by-country liabilities is CM-I-3 until 2001_03 and CM-I-2 from 2001_06 (see `out/find_tables_examples.txt`).

## 3. Census numbers

Index of 95,053 tables. Titles are classified by `file_tables`, otables.py:295.

| era | tables | coded id | `Table N.-` | plain title | continuation parts | as-of found | TOC |
|---|---|---|---|---|---|---|---|
| 1930s | 947 | 0% | 0% | 84% | 14% | 10% | 0% |
| 1940s | 13,268 | 0% | 43% | 41% | 24% | 19% | 0% |
| 1950s | 12,907 | 0% | 90% | 7% | 22% | 24% | 0% |
| 1960s | 17,549 | 3% | 84% | 7% | 31% | 24% | 0% |
| 1970s | 22,541 | 83% | 0% | 8% | 31% | 23% | 4% |
| 1980s | 11,680 | 73% | 0% | 12% | 36% | 25% | 5% |
| 1990s | 5,363 | 78% | 0% | 10% | 37% | 17% | 5% |
| 2000s | 5,204 | 81% | 0% | 8% | 36% | 14% | 5% |
| 2010s | 3,815 | 74% | 0% | 14% | 31% | 10% | 4% |
| 2020s | 1,779 | 74% | 0% | 12% | 34% | 8% | 5% |

Sweep over the first issue of each year (`out/sweep.txt`, 12,806 tables, 0 crashes):
- Year fill-down was needed on 316–15,214 rows per decade.
- `YEAR_UNKNOWN` rows cluster in the 1940s and 1970s (AY-1 style blocks, maturity schedules).
- A row-total or column identity is found on 18–52% of tables. An identity fails on at least one row in 4–33% of tables (OCR misreads or shifts), most often before 1980.
- Stacked-row and merged-cell repairs are almost entirely in the 1960s and 1970s.

## 4. Prototype: `understand/tables_proto/otables.py` (stdlib only, Python ≥3.8, SQLite FTS5)

CLI: `python3 otables.py build | find | show | printings`. Build takes 25 s over 697 files and writes `cache/tables.sqlite`. The index is 140 MB and can be trimmed: the FTS table is already contentless, and it could drop the header text copy.

**`find_tables(phrase, years=None)`** (otables.py:1327)
- An id-like phrase is matched against the canonical id. `canon_id` (otables.py:86) undoes `FF0`→`FFO`, `1FS`→`IFS`, `CM-11-1`→`CM-II-1` and `FCP-V11-2`→`FCP-VII-2`.
- Other phrases run an AND over FTS tokens in title, subtitle, units, section and header, plus row labels with `--rows`.
- A space-free `LIKE` fallback catches syllable-split issues.
- Hits are grouped per table identity (id or `Table N in SECTION`, plus the title with as-of dates and `con.`/`Preliminary` removed), with issue coverage, e.g. `[FFO-3] … | 4 issues 2013_03..2013_12`.
- Each hit shows `file:line`, part number, width × rows, period range and as-of date.
- TOC tables are demoted.

**`show_table(file, line)`** (otables.py:1481; the analysis itself is `analyze`, otables.py:944)
- **Context**: title, id, subtitle, units, as-of date and section. Continuation parts inherit these. `--stitch` joins `con.` parts; for FFO-3 2004_03 that is 37 columns matched by `(n)` numbers.
- **Header**: cleaned ` > ` paths, with `Unnamed`, duplicate parents and a common banner stripped. When the header is missing, the previous part's header is borrowed. A header-only title block feeds the column names. In-body header rows relabel the rows after them.
- **Segments**:
  - Split at banner and in-body-header rows. Each segment gets its own layout, because a table can change layout between monthly and weekly sections.
  - Side-by-side panels are found from period columns. They are accepted only with a repeating header or equal-width blocks, so a maturity-date column is not mistaken for a panel.
  - Repeated column groups under one stub are found from label periodicity or header year runs (AY-1).
  - Panels are unstacked: all rows of panel 1, then panel 2, and so on.
- **Label realignment per panel**: the period label is dropped, and a slid sub-label (`Period > From 10 to 20 years`) moves to the first value column. Every realignment prints a VERIFY note. When the label count does not match, the labels are marked `?` and "NOT reliable" instead of being guessed.
- **Period fill-down** (`_fill`, otables.py:840):
  - `YYYY-MM[-DD]` from explicit labels.
  - Month rollover is flagged `year+1 inferred`. Newest-first tables are handled.
  - Year-label rows vote per column group. `YYYY` year-group rows and banners with years (`1993-Con.`, `1999 - Dec 2000`) are used.
  - Bare day numbers are read inside dated columns. A blank stub means "same as the row above".
  - Annual rows get an FY/CY prefix from the section banner or stub label. `Fiscal year 2004 to date` is recognised.
  - A missing year becomes `Y?k-MM`, never a guess.
- **Column periods** for transposed tables: `2012-06~`, where `~` means inferred (after annual columns the year is the last annual year + 1). Balance-sheet dates are parsed as dates.
- **Checks printed as NOTES**:
  - sequence breaks (modal-step test; runs of year-end rows are exempt);
  - identities `c = sum(adjacent run)`, found strictly in one segment, then scored in all segments, with the failing lines listed;
  - label/type mismatches (`Maturity date` holding numbers);
  - "last labelled column never holds data" (spurious header label);
  - rows that fell out of the pipe table (label plus 4 or more numbers just below it);
  - empty columns.
- **Repairs, always flagged on the row**:
  - `SHIFT+1`: a row slid left, detected against the type signature of rows whose stub is a period.
  - `SPLIT`: two numbers in one cell with trailing blanks.
  - `ROWSPLIT i/k`: stacked rows.
  - `--raw` shows the original lines.
- **Cells are kept raw**, so `r`, `p` and `4/` stay visible. `parse_cell` (otables.py:200) returns the value plus flags: `r`, `p`, `fn:k/`, `fn:k/?` (a stray digit after a footnote), `lt_half`, `dash`, `na`, `paren`, `pct`, `float0`, `multi:n`, `trail_digit?`, `text`.
- **Output size**:
  - A 9 KB default budget cuts at a row boundary and prints `continue with --from-row N`. This avoids the ~10 KB silent head+tail truncation Codex applied in v8 (FINDINGS §2.5).
  - Other options: `--cols`, `--match`, `--grep`, and `--long`, which prints one line per cell with the row label, column label and column period.

**`printings(row_label, table_hint, period=None, column=None)`** (otables.py:1661)
- Takes every issue that prints the table (id or title phrase) and finds the row by period (`2003-01`, `Jan 2003`, `1995`, `FY1995`) or by label substring. The column is selected by label substring or `(n)` number.
- The default window is the period's year to +3 years for months and to +12 years for years. `--years` overrides it.
- It lists `issue file:line part period label | column = raw [flags]` (capped at 60 lines).
- The SUMMARY gives, per part and column:
  - the distinct values with the issues that print them;
  - first and latest printing;
  - `ISOLATED` for a value printed once between printings that agree, which is more likely OCR or a row shift than a revision.
- Row matching uses per-row labels, so an in-body header band cannot be misattributed.
- Speed: 0.5 s with the window, about 13 s across all FFO-3 printings (860 parts).

## 5. Tests on tables from the failure analysis (`run_cases.py`, output in `out/run_cases.txt`)

The checks are structural only: periods, labels, flags, identities, and which issues print a row.

| Case | Q | Result | What the tool does (and does not) do |
|---|---|---|---|
| FFO-3 2004_03:728 stitched | 81, 16, 86 | PASS | 3 parts → all 37 numbered columns. Periods FY1999..FY2003, `FY2004 est`, 2002-12, 2003-01..2003-12, `FY2004 to date`. `r` flags parsed. |
| printings FFO-3 Jan 2003, GSA column | 81 | PASS | Printed in exactly 2003_06, 2003_09, 2003_12, 2004_03, the same four Opus found by hand. |
| printings FFO-3 Jan 1987, Judiciary | 86 | PASS | 1987_06/09/12 agree and 1988_03 differs, matching Opus. The first run wrongly reported 1987_09's second-band value under "The judiciary" (both bands sit in one pipe table). Fixed by per-row labels. This silent-error class is why the tools need tests. |
| AY-1 1970_06:4946 | 11, 18 | PASS | 4 column groups × 36 months unstacked. Every row is `Y?k-MM` with an explicit note. The group-4 third block ends at May, which is the issue-date anchor. Sibling printings are listed (1970_05, 1970_07). |
| AY-1 1970_05:4848 | 18 | PASS | Header years plus year-label rows anchor all groups: 1959-01..1961-12, 1962.., 1965.., 1968-01..1970-04. Column names come from the header-only title block. |
| Yields 1944_03:4015 | 30 | PASS | Sequence breaks flagged at 4031 (panels 2 and 3) and 4041: the label slide GLM missed. The 2-value panels are marked "labels NOT reliable". Weekly section dates are filled to the day. |
| Yields 1956_07:2603 / 1956_08:3176 | 62 | PASS | 1956_07: slid sub-label reassigned (`From 10 to 20 years`, `…and after`, `Moody's`) with a VERIFY note. 1956_08: in-body header rows give `…20 years and after` per column. |
| FFO-2 windows 2011_03 / 2018_12 / 2011_12 | 14, 130 | PASS | The periods make coverage explicit: 2009-12..2010-12 (all of CY2010), 2017-09..2018-09 (all of FY2018), 2010-09..2011-09 (no Oct–Dec 2011; cards' Q14 mistake). |
| PDO-1 1972_03:2076 | 9 | PARTIAL | 48 rows `SHIFT+1`-repaired. Identity `c2 = c3+c4` then holds on 37/40 rows and names the garbled lines 2085, 2090, 2100. The garbled `1976` year labels are not repaired. |
| MQ-1 1982_03:4363 | 15 | PARTIAL | Warns that `Maturity date`/`Issue date` hold numbers, so the header is shifted. It does not realign; that would need a type-guided alignment of the label list to the non-empty columns. |
| CM-I-3 2000_12, CM-I-2 2001_12, CM-I-1 2013_03 | 89, 13 | PASS | Column periods: 2000-05..2000-09 (explicit); 2001-05..2001-09 (inferred after the annual 1998–2000 columns); 2012-06..2012-12 (inferred after `Calendar Year 2011`). |
| FD-1 2000_09:957 (syllable-split issue) | 101, 8 | PASS | Title and id detected. Identities c1=c2+c3, c4=c5+c6 and c7=c8+c9 on 17/17 rows, the same checks Opus used to prove alignment. Found by phrase search. |
| Months × years 1950_02:369 | 17 | PASS | The row-total identity fails at the 1942 and 1947 expenditure rows (the misreads Opus found) plus 1941 expenditures and two surplus rows. The cell period for long output is row year + column month. |
| TSO Table 3 1963_03:3762 | 43 | PARTIAL | Warns "last labelled column never holds data: spurious extra label, labels right of it shifted". The identity `c1 = sum c2..c11` holds on 65/66 rows. It cannot say which label is spurious. |
| SBN-3 2001_03:2040 | 122 | PASS | Annual rows tagged FY vs CY from the banners. The unlabeled `Mar` under the banner `1999 - Dec 2000` becomes 2000-03 (flagged). `printings` marks the 2001_03 printing of col (8) as ISOLATED: its month block sits one row off against the 2000_06..2001_06 printings. |
| Expenditures by agencies 1956_08:250 | 60 | PASS | 2 column bands stitched. `Defense Department > Military functions / > Civil functions` keeps the parent. |
| FFO-5 1981_12:905 | 88 | PARTIAL | Notes that line 916 is a table row that fell out of the pipe table. `1-156` is typed as text, not a number. It does not decode the fused `2/1dd,ddd` cell (footnote 2/ + OCR'd `r` + value); comparing printings is the check. |
| OFS-2 2013_03:1853 | 105, 44 | PASS | Newest-first: 2012-12, 2012-09, …, 2010-12 with no false sequence flags. A naive fill-down gives 2013-09. |
| OFS-1 2007_09:1797 stitched | 44, 77, 101 | PASS | `2006 - June` row present. Identity marketable + nonmarketable = total on 18/18 rows. Part 2 identity fails on 1 line (1825). |
| ESF-1 2012_06:4722 | 39 | PASS | Banner stripped. Column dates `Sept. 30, 2011`, `Oct. 1, 2011 through Dec. 31, 2011`, `Dec. 31, 2011`. Section path `Assets > U.S. dollars > Held with Treasury`. Group membership after lost indentation is shown as "nearest banner", not asserted. |
| find_tables recall | 109, 101, 16 | PASS | `sales and redemptions` 1982 lists SN-1 next to SB-2 (Q109: both arms never opened SN-1). `judicial branch` finds FFO-3 via its lowercase header (Q16). The syllable-split FD-1 is found. The `Cal en dar year` header is found via the space-free match. |

## 6. Where it fails or can mislead (open risks)

1. **Header realignment without periods or identities** (MQ-1, TSO spurious label, 1939_06:232 level-0 spans) is warned about, not fixed.
   - Next steps: a type-guided monotone alignment of labels to non-empty columns, a difference identity (`excess = receipts − expenditures`), and "borrow a header from a sibling printing with the same width". The SIBLINGS list is already printed; the automatic transfer is not written.
2. **Heuristic repairs can be wrong.**
   - Affected: `SHIFT+1`, `ROWSPLIT` (single-valued cells go to the first sub-row), banner-year inference, column-period inference after annual columns, and `year+1 inferred`.
   - All are flagged on the row, and `--raw` shows the source.
   - A v9 L4 checker should warn when a cited cell sits on a repaired or inferred row and no `printings` or identity check was run.
3. **Lost information cannot be recovered**:
   - indentation (group membership, ESF-1/Q39);
   - lost year labels (AY-1 1970_06): the tool says so and points to siblings;
   - garbled labels (`1976` in PDO-1 1972);
   - fused cells (the `2/1dd,ddd` cell).
4. **Part identity across issues is by part order.** For per-series multi-part tables (SBN-3 parts per series) the order can differ between issues. The key should include the part's banner or series name.
5. **Title and identity gaps**:
   - About 1–2% of tables have no title. Some ids are truncated (`Table SB-` then `- Sales and Redemptions…`, 1982_07:3825-3827); these are merged by title when a twin group exists.
   - Numbered-era ids need the section to be unique.
   - Tables are renumbered over time (CM-I-3 → CM-I-2), so `printings --table CM-I-3` stops at 2001_03. A title phrase is safer across renumberings.
6. **`printings` window**: the default (+3 years for months, +12 for years) can miss late reprints. It is overridable. The summary groups columns by `(n)` number when present, otherwise by space-free label, and label text drifts across decades.
7. **Performance**: `analyze` takes about 4 ms per table and `show` is interactive. Unwindowed `printings` over a whole family takes about 13 s.
8. **Hand-tuned thresholds**: 60% period cells for a stub, 75% label periodicity, 80% identity support, ±1 panel width. These are candidate knobs for the evolver (see below).

## 7. Recommendations for v9

- **L1**: ship `otables.py` as the workspace table CLI.
  - Build the index at prepare time, next to `corpus.sqlite`.
  - Expose three commands: `find`, `show`, `printings`.
  - Make the stitched/long views and `--budget` pagination the default guidance.
- **L2**: every output line already carries `file:line`, part, period and column label. That is exactly what `ledger cell --src file:line …` needs.
  - Recommendation: `show --long` lines should be pasteable as ledger entries.
- **L4 checkers can reuse the parsers**:
  - the cited value exists in the cited row and column;
  - the period label of the cited row matches the question's period, with FY vs CY distinguished by the prefix;
  - the cited row carries `r`/`p`/`fn` or a repair flag, so a disclosure or `printings` check is required;
  - the cited table has a failing identity on that row;
  - a later printing with a different value exists (from `printings`), so the choice must be stated.
- **Evolver admission by offline replay**: `run_cases.py` is the seed of a deterministic structural replay suite. It runs in about 2 s. It is best extended with the tables cited in `per-question/*.json` and `logs/*/worklog.md`.
  - The thresholds and regex lists above are natural candidates for evolver edits.
  - A heuristic change should be admitted only if every case keeps its status and the corpus sweep shows no new crashes and no big jump in `SEQ`/`YEAR_UNKNOWN` counts (`sweep.py`, about 55 s).
- **Environment**: the tool is stdlib-only and runs on `/usr/bin/python3` 3.9 here. The v8 GLM sandbox needs the planned `python3` shim (FINDINGS §2.5). The v8 `officeqa_tools.py` read tool caps at 150 lines / 24,000 chars (officeqa-pro133-v8-plumbing/officeqa_tools.py:92-105), and its search is an FTS5 OR of up to 16 tokens over 100-line chunks (:20-33, :72-90). `find_tables` complements it with table-level AND search and identity grouping.
