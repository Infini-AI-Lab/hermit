"""A two-issue synthetic Treasury Bulletin corpus with a tables index ($0; no benchmark question, answer or
document). Shared by tests/test_v10_ledger.py (parser modes over the real OfficeQA pack) and
tests/test_v10_orchestration.py (campaign roots). The figures are invented; `TASK_IDS` carries two ids that are
in data.ISSUE_47 so the withheld-reference path is exercised."""
from __future__ import annotations

from pathlib import Path

BULLETIN = '''TREASURY BULLETIN {issue}

Table FFO-1. - Summary of Fiscal Operations
(In millions of dollars)

| Fiscal year | Receipts | Outlays | Surplus or deficit |
|---|---|---|---|
| {y1} | 1,991,082 | 1,862,846 | 128,236 |
| {y2} | 1,853,136 | 2,010,894 | -157,758 |
'''
ISSUES = (('2002_03', (2001, 2002)), ('2003_03', (2002, 2003)))
QUESTIONS = ['What were total federal receipts in fiscal year 2001 in millions of dollars?',
             'What were federal outlays in fiscal year 2002?',
             'By how much did the public debt change between 2001 and 2002?',
             'What was the surplus in fiscal year 2001?',
             'Report the receipts and outlays for fiscal year 2002 as a list.',
             'What share of outlays did receipts cover in 2002, as a percent?',
             'Which fiscal year had a deficit?']
TASK_IDS = ['UID0001', 'UIDT002', 'UIDT003', 'UID0004', 'UIDT005', 'UIDT006', 'UIDT007']   # 2 issue-47 ids


def file_name(issue: str) -> str:
    return f'treasury_bulletin_{issue}.txt'


def make_corpus(corpus: Path) -> Path:
    corpus = Path(corpus)
    corpus.mkdir(parents=True, exist_ok=True)
    for issue, years in ISSUES:
        (corpus / file_name(issue)).write_text(BULLETIN.format(issue=issue, y1=years[0], y2=years[1]))
    return corpus


def build_tables_index(corpus: Path, out: Path) -> Path:
    from packs.officeqa.tools import otables
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    otables.build_index(corpus, out)
    return out


def table_header_line(corpus: Path, issue: str) -> int:
    """1-based line of the pipe-table header row of FFO-1 in one issue (what show_table takes)."""
    lines = (Path(corpus) / file_name(issue)).read_text().split('\n')
    return next(i for i, line in enumerate(lines, 1) if line.startswith('| Fiscal year'))
