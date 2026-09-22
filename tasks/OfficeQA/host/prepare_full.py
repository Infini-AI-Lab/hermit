#!/usr/bin/env python3
"""Prepare the full OfficeQA Pro v1 set (133 questions) in the same seed order
as the 50-question sample: the selection sorts every uid by the seeded hash,
so the first 50 of this order are exactly the sample's 50, in the sample's
order, followed by the other 83. A 50-question run can therefore be resumed
against this root with --questions 133."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data  # noqa: E402
from paths import DATA_CACHE, SOURCE  # noqa: E402

COUNT = 133


def prepare(root: Path) -> dict:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = data._prepared(root)
    if destination.exists():
        raise ValueError("Prepared data already exists; refusing to overwrite")
    cache = DATA_CACHE
    with tempfile.TemporaryDirectory(prefix=".data-stage-", dir=root) as temporary:
        staging = Path(temporary) / "prepared"
        data._legacy.prepare(cache / "officeqa_pro.csv", cache / "treasury_bulletins_parsed/transformed", staging,
                             count=COUNT, seed=data.SEED, expected_questions=133, expected_documents=697,
                             receipt_path=cache / "download_receipt.json")
        index = staging / "agent/corpus.sqlite"
        index_metadata = data._tools.build_index(staging / "agent/corpus", index, expected_documents=697)
        data._write_new(staging / "index-metadata.json", index_metadata)
        manifest = data._read(staging / "manifest.json")
        manifest.update({
            "protocol": "officeqa-pro-v1-all133-online-v1",
            "track": "all 133 questions in the sample's seed order (the 50-question sample is its prefix); actual Codex harness; full parsed TXT corpus; network-restricted; online adaptation between question blocks",
            "agent_index": "agent/corpus.sqlite",
            "ordered_task_ids_sha256": data._ids_hash(manifest["task_ids"]),
            "index_sha256": data.sha256(index),
            "index_metadata_sha256": data.sha256(staging / "index-metadata.json"),
            "preparation_source_sha256": {name: data.sha256(SOURCE / name) for name in ("prepare_full.py", "data.py", "officeqa_prepare.py", "officeqa_tools.py")},
            "actor_read_allowlist": ["agent/corpus/", "agent/corpus.sqlite"],
            "hidden_from_agent": ["agent/tasks.jsonl", "answer", "source_docs", "source_files", "private/", "original CSV", "download cache", "other tasks and completed results"],
        })
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for relative in ("agent/tasks.jsonl", "manifest.json", "index-metadata.json", "private/answers.jsonl", "private/provenance.jsonl", "private/reward.py"):
            (staging / relative).chmod(0o400 if relative.startswith("private/") else 0o444)
        staging.rename(destination)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    m = prepare(parser.parse_args().root)
    print(json.dumps({"prepared": True, "questions": m["sample_count"], "documents": m["corpus_document_count"], "first_ids": m["task_ids"][:5]}))
