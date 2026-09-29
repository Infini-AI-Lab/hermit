# OfficeQA solving workspace (Opus 5.5)

The complete permitted text corpus (697 U.S. Treasury Bulletin issues, parsed to TXT,
one file per issue, named treasury_bulletin_YYYY_MM.txt) is read-only at:
/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/opus-officeqa-133/corpus

The corresponding SQLite FTS5 index is read-only at:
/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/opus-officeqa-133/corpus.sqlite

The local `officeqa.py` helper exposes these interfaces:
```python
import sys; sys.path.insert(0, '/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/opus-officeqa-133')
from pathlib import Path
from officeqa import OfficeQACorpus
docs = OfficeQACorpus(Path('/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/opus-officeqa-133/corpus'), Path('/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/opus-officeqa-133/corpus.sqlite'))
docs.search(query="text", limit=8, filename_glob="*.txt")
docs.read(filename="document.txt", start_line=1, num_lines=80)
docs.list_files(pattern="*.txt", limit=100, offset=0)
```
Search returns BM25-ranked text excerpts with filename and line anchors. Read returns
numbered document lines. You may also read the TXT files directly (grep, sed, Python).
Python: /opt/homebrew/bin/python3.11 (numpy available).
