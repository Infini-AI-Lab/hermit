#!/usr/bin/env python3
"""Fixed local search/read interface over the complete authorized OfficeQA corpus.

Only corpus text is indexed. No question-to-document hints or answers are used.
The runner must enforce isolation for its separate Python-execution tool.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import sqlite3


def build_index(corpus: Path, index: Path, *, expected_documents: int = 697,
                chunk_lines: int = 100, overlap_lines: int = 15) -> dict:
    if chunk_lines <= overlap_lines or overlap_lines < 0:
        raise ValueError("Chunk size must exceed nonnegative overlap")
    corpus, index = corpus.resolve(), index.resolve()
    if index.exists():
        raise ValueError("Index already exists; use a fresh path")
    files = sorted(corpus.glob("*.txt"))
    if len(files) != expected_documents:
        raise ValueError(f"Expected the complete {expected_documents}-document corpus")
    index.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(index)
    try:
        connection.execute("CREATE VIRTUAL TABLE chunks USING fts5(filename UNINDEXED, start_line UNINDEXED, end_line UNINDEXED, content, tokenize='unicode61 remove_diacritics 2')")
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        manifest = []
        chunks = 0
        for path in files:
            if not path.resolve().is_relative_to(corpus):
                raise ValueError("Corpus file escapes the declared corpus root")
            data = path.read_bytes()
            lines = data.decode("utf-8").splitlines()
            manifest.append({"filename": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            for first in range(0, len(lines), chunk_lines - overlap_lines):
                end = min(first + chunk_lines, len(lines))
                connection.execute("INSERT INTO chunks VALUES (?, ?, ?, ?)", (path.name, first + 1, end, "\n".join(lines[first:end])))
                chunks += 1
                if end == len(lines):
                    break
        metadata = {
            "algorithm": "SQLite FTS5 unicode61; OR of up to 16 query tokens; BM25 order; filename/start-line tie-break",
            "document_count": len(files), "chunk_count": chunks,
            "chunk_lines": chunk_lines, "overlap_lines": overlap_lines,
            "corpus_manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
            "sqlite_version": sqlite3.sqlite_version,
            "uses_question_source_hints": False, "uses_answers": False,
        }
        connection.executemany("INSERT INTO metadata VALUES (?, ?)", [(k, json.dumps(v)) for k, v in metadata.items()])
        connection.commit()
    finally:
        connection.close()
    index.chmod(0o444)
    return metadata


class OfficeQACorpus:
    """Allowlisted methods suitable for a tool-dispatch wrapper; no shell access."""

    def __init__(self, corpus: Path | str, index: Path | str):
        self.corpus = Path(corpus).resolve()
        self.index = Path(index).resolve()

    def search(self, query: str, limit: int = 8, filename_glob: str = "*.txt") -> dict:
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError("Search query must be text of at most 2,000 characters")
        if not 1 <= limit <= 20:
            raise ValueError("Search limit must be between 1 and 20")
        tokens = list(dict.fromkeys(re.findall(r"\w+", query.lower(), flags=re.UNICODE)))[:16]
        if not tokens:
            return {"query": query, "results": [], "query_tokens": []}
        expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        with closing(sqlite3.connect(self.index.as_uri() + "?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                "SELECT filename, start_line, end_line, snippet(chunks,3,'','', ' … ',48), bm25(chunks) "
                "FROM chunks WHERE chunks MATCH ? AND filename GLOB ? "
                "ORDER BY bm25(chunks), filename, CAST(start_line AS INTEGER) LIMIT ?",
                (expression, filename_glob, limit),
            ).fetchall()
        return {"query": query, "query_tokens": tokens, "match_mode": "OR; ranked by SQLite FTS5 BM25",
                "results": [{"filename": r[0], "start_line": int(r[1]), "end_line": int(r[2]),
                             "excerpt": r[3][:6000], "rank": r[4]} for r in rows]}

    def read(self, filename: str, start_line: int = 1, num_lines: int = 80) -> dict:
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".txt"):
            raise ValueError("Read accepts a corpus TXT basename only")
        path = (self.corpus / filename).resolve()
        if not path.is_relative_to(self.corpus) or not path.is_file():
            raise ValueError("File is not in the permitted corpus")
        if start_line < 1 or not 1 <= num_lines <= 150:
            raise ValueError("Use a positive start line and 1–150 lines")
        lines = path.read_text(encoding="utf-8").splitlines()
        selected = lines[start_line - 1:start_line - 1 + num_lines]
        content = "\n".join(f"{start_line + i}: {line}" for i, line in enumerate(selected))
        return {"filename": filename, "start_line": start_line,
                "end_line": start_line + len(selected) - 1, "total_lines": len(lines),
                "content": content[:24000], "truncated": len(content) > 24000}

    def list_files(self, pattern: str = "*.txt", limit: int = 100, offset: int = 0) -> dict:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Use limit 1–100 and nonnegative offset")
        names = [p.name for p in sorted(self.corpus.glob("*.txt")) if fnmatch.fnmatchcase(p.name, pattern)]
        return {"filenames": names[offset:offset + limit], "total_matching": len(names),
                "next_offset": offset + limit if offset + limit < len(names) else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--filename-glob", default="*.txt")
    search.add_argument("--limit", type=int, default=8)
    read = sub.add_parser("read")
    read.add_argument("filename")
    read.add_argument("--start-line", type=int, default=1)
    read.add_argument("--num-lines", type=int, default=80)
    args = parser.parse_args()
    if args.command == "build":
        result = build_index(args.corpus, args.index)
    else:
        tool = OfficeQACorpus(args.corpus, args.index)
        result = tool.search(args.query, args.limit, args.filename_glob) if args.command == "search" else tool.read(args.filename, args.start_line, args.num_lines)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
