#!/usr/bin/env python3
"""Prepare OfficeQA Pro v2 (databricks/officeqa-pro-v2) the way the v1 sample is prepared.

v2 ships parsed JSON documents (one per PDF: ordered elements with a type, a
content string and page-anchored bounding boxes; tables as HTML). The v1 corpus
was already flat TXT with Markdown tables, so this module renders every v2 JSON
into that same shape (<slug>.txt, tables as pipe tables with a numeric header
row, elements separated by blank lines, no page markers) and then prepares the
question set with the v1 machinery: all 90 questions, one fixed seed-defined
order, private answers/provenance, the FTS5 index, and a manifest data.py can
load. The v2 CSV has no difficulty column and its uids are plain integers.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data  # noqa: E402
import officeqa_prepare as legacy  # noqa: E402
import officeqa_tools as tools  # noqa: E402

REPO = "databricks/officeqa-pro-v2"
DATASET_URL = f"https://huggingface.co/datasets/{REPO}"
SEED = "metaworld-officeqa-pro-v2-all90-v1"
QUESTIONS = 90
DOCUMENTS = 1435
REQUIRED_COLUMNS = {"uid", "question", "answer", "source_docs", "source_files"}


class _TableParser(HTMLParser):
    """Collect the cells of an HTML table as a rectangular grid. colspan and
    rowspan keep later cells in their columns; the spanned positions are left
    empty, as they are on the printed page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self._colspan = 1
        self._rowspan = 1
        self._carry: dict[int, int] = {}  # column -> rows still covered by a rowspan above

    def _skip_carried(self):
        row = self.rows[-1]
        while len(row) in self._carry:
            column = len(row)
            row.append("")
            self._carry[column] -= 1
            if self._carry[column] == 0:
                del self._carry[column]

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.rows.append([])
            self._skip_carried()
        elif tag in ("td", "th"):
            if not self.rows:
                self.rows.append([])
            attributes = dict(attrs)
            self._colspan = int(attributes["colspan"]) if str(attributes.get("colspan", "")).isdigit() else 1
            self._rowspan = int(attributes["rowspan"]) if str(attributes.get("rowspan", "")).isdigit() else 1
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            row = self.rows[-1]
            first = len(row)
            row.append(" ".join("".join(self._cell).split()))
            row.extend([""] * (self._colspan - 1))
            if self._rowspan > 1:
                for column in range(first, first + self._colspan):
                    self._carry[column] = self._rowspan - 1
            self._cell = None
            self._skip_carried()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def table_to_markdown(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    if not text.lstrip().startswith("<"):
        return text  # a table the parser left as plain text (or "N/A")
    parser = _TableParser()
    parser.feed(text)
    parser.close()
    rows = [row for row in parser.rows if any(cell for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    cell = lambda value: value.replace("|", "\\|")  # noqa: E731
    lines = ["| " + " | ".join(str(i) for i in range(width)) + " |",
             "| " + " | ".join("---" for _ in range(width)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(cell(value) for value in row + [""] * (width - len(row))) + " |")
    return "\n".join(lines)


def render_document(document: dict) -> str:
    blocks = []
    for element in document["document"]["elements"]:
        content = element.get("content")
        if content is None:
            continue
        content = html.unescape(str(content)) if element.get("type") != "table" else str(content)
        text = table_to_markdown(content) if element.get("type") == "table" else content.strip()
        if text:
            blocks.append(text)
    return "\n\n".join(blocks) + "\n"


def render_corpus(jsons: Path, corpus: Path) -> int:
    corpus.mkdir(parents=True, exist_ok=True)
    files = sorted(jsons.glob("*.json"))
    for path in files:
        text = render_document(json.loads(path.read_text(encoding="utf-8")))
        if not text.strip():
            text = "(the parser produced no text for this document)\n"
        (corpus / f"{path.stem}.txt").write_text(text, encoding="utf-8")
    return len(files)


def prepare(root: Path, *, csv_path: Path, jsons: Path) -> dict:
    """Create root/prepared exactly once: rendered corpus, index, tasks, keys, manifest."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = data._prepared(root)
    if destination.exists():
        raise ValueError("Prepared data already exists; refusing to overwrite")
    with csv_path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not REQUIRED_COLUMNS.issubset(reader.fieldnames or []):
            raise ValueError("CSV is missing the OfficeQA Pro v2 columns")
        rows = list(reader)
    if len(rows) != QUESTIONS or any(not row["question"] or not row["answer"] for row in rows):
        raise ValueError(f"Expected {QUESTIONS} v2 questions with a question and an answer each")
    if legacy.sha256(legacy.REFERENCE / "reward.py") != legacy.REWARD_SHA256:
        raise ValueError("Official reward.py does not match the pinned source hash")
    lock = root / ".data-prepare.lock"
    os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    try:
        with tempfile.TemporaryDirectory(prefix=".data-stage-", dir=root) as temporary:
            staging = Path(temporary) / "prepared"
            private, agent = staging / "private", staging / "agent"
            private.mkdir(parents=True, mode=0o700)
            rendered = render_corpus(jsons, agent / "corpus")
            if rendered != DOCUMENTS:
                raise ValueError(f"Expected {DOCUMENTS} parsed documents; rendered {rendered}")
            selected = legacy.select_rows(rows, QUESTIONS, SEED)
            legacy.write_jsonl(agent / "tasks.jsonl", [{"task_id": r["uid"], "question": r["question"]} for r in selected])
            legacy.write_jsonl(private / "answers.jsonl", [{"task_id": r["uid"], "answer": r["answer"]} for r in selected])
            legacy.write_jsonl(private / "provenance.jsonl", [{"task_id": r["uid"], "source_docs": r["source_docs"], "source_files": r["source_files"]} for r in selected])
            shutil.copyfile(legacy.REFERENCE / "reward.py", private / "reward.py")
            if (legacy.REFERENCE / "LICENSE-APACHE").is_file():
                shutil.copyfile(legacy.REFERENCE / "LICENSE-APACHE", private / "LICENSE-APACHE")
            documents = sorted((agent / "corpus").glob("*.txt"))
            corpus_manifest = []
            for document in documents:
                document.chmod(0o444)
                corpus_manifest.append({"filename": document.name, "sha256": legacy.sha256(document), "bytes": document.stat().st_size})
            legacy.write_json(private / "corpus_manifest.json", corpus_manifest)
            index = agent / "corpus.sqlite"
            index_metadata = tools.build_index(agent / "corpus", index, expected_documents=DOCUMENTS)
            data._write_new(staging / "index-metadata.json", index_metadata)
            manifest = {
                "benchmark": "OfficeQA Pro v2",
                "protocol": "officeqa-pro-v2-all90-online-v1",
                "track": "all 90 questions in one fixed seed-defined order; actual Codex harness; the 1,435 parsed documents rendered to TXT; network-restricted; online adaptation between question blocks",
                "official_headline_comparable": False,
                "reason": "Parsed JSON rendered to flat TXT with Markdown tables, no PDFs, no web, and a fixed custom search tool differ from the official PDF agent-harness setting",
                "repository": REPO, "dataset_url": DATASET_URL,
                "data_verification": "Local authorized download; content hashes recorded here, no upstream receipt",
                "source_csv_sha256": legacy.sha256(csv_path),
                "source_question_count": len(rows), "sample_count": QUESTIONS,
                "selection": {"algorithm": "ascending SHA256(UTF8(seed + NUL + uid)), uid tie-break; all rows", "seed": SEED},
                "task_ids": [r["uid"] for r in selected],
                "ordered_task_ids_sha256": data._ids_hash([r["uid"] for r in selected]),
                "agent_tasks": "agent/tasks.jsonl", "agent_corpus": "agent/corpus", "agent_index": "agent/corpus.sqlite",
                "corpus_rendering": "v2_prepare.render_document: elements in parse order separated by blank lines; HTML tables as Markdown pipe tables with a 0..n-1 header row (the v1 TXT shape); no page markers",
                "corpus_document_count": len(documents), "corpus_bytes": sum(p.stat().st_size for p in documents),
                "corpus_manifest_sha256": legacy.sha256(private / "corpus_manifest.json"),
                "tasks_sha256": legacy.sha256(agent / "tasks.jsonl"), "answers_sha256": legacy.sha256(private / "answers.jsonl"),
                "index_sha256": legacy.sha256(index), "index_metadata_sha256": legacy.sha256(staging / "index-metadata.json"),
                "grader": {"repository": "https://github.com/databricks/officeqa", "commit": legacy.CODE_REVISION, "path": "reward.py", "sha256": legacy.REWARD_SHA256, "default_tolerance": 0.0},
                "preparation_source_sha256": {name: legacy.sha256(Path(__file__).resolve().parent / name) for name in ("v2_prepare.py", "officeqa_prepare.py", "officeqa_tools.py", "data.py")},
                "actor_read_allowlist": ["agent/corpus/", "agent/corpus.sqlite"],
                "task_delivery": "Controller delivers only the current question. agent/tasks.jsonl is controller-only and must not be mounted with its parent directory",
                "hidden_from_agent": ["agent/tasks.jsonl", "answer", "source_docs", "source_files", "private/", "original CSV", "download cache", "other tasks and completed results"],
                "fixture_only": False,
            }
            (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            for relative in ("agent/tasks.jsonl", "manifest.json", "index-metadata.json", "private/answers.jsonl", "private/provenance.jsonl", "private/reward.py"):
                (staging / relative).chmod(0o400 if relative.startswith("private/") else 0o444)
            staging.rename(destination)
        return manifest
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="directory that will hold prepared/")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1] / ".data/officeqa-pro-v2")
    args = parser.parse_args()
    m = prepare(args.root, csv_path=args.source / "officeqa_pro_v2.csv", jsons=args.source / "parsed_corpus/jsons")
    print(json.dumps({"prepared": True, "questions": m["sample_count"], "documents": m["corpus_document_count"],
                      "corpus_bytes": m["corpus_bytes"], "first_ids": m["task_ids"][:5]}, indent=2))
