#!/usr/bin/env python3
"""Prepare and grade a fixed OfficeQA Pro sample without giving answers to the agent.

Preparation and grading use the standard library. Only the explicit fetch command
requires huggingface_hub and an already-authorized local Hugging Face client.
This program does not request/accept dataset access or make model calls.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sys
import urllib.error
import urllib.request


from paths import VENDOR
REFERENCE = VENDOR
REPO = "databricks/officeqa"
DATASET_URL = f"https://huggingface.co/datasets/{REPO}"
DATA_REVISION = "763a8366abf2a3605c381d53586d844dc60fa756"
CODE_REVISION = "7b9a3c154ef9fb40215bb67934afc43e6799de16"
REWARD_SHA256 = "0d91698c87df6d889339aac36f63ae0966607f169890b0bf8b472b26bfe8138f"
DEFAULT_SEED = "metaworld-officeqa-pro-baseline-50-v1"
CORPUS_RELATIVE = Path("treasury_bulletins_parsed/transformed")
REQUIRED_COLUMNS = {"uid", "question", "answer", "source_docs", "source_files", "difficulty"}
ACCESS_ACTION = (
    "Sign in at https://huggingface.co/datasets/databricks/officeqa, personally "
    "review the contact-sharing and answer-key conditions and obtain access. "
    "Then authenticate the local Hugging Face client for that authorized account. "
    "Do not paste a token into chat. This script does not accept terms."
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Each JSONL record must be an object")
            rows.append(row)
    return rows


def inspect_public_access() -> dict:
    """No token lookup: public metadata and an anonymous HEAD request only."""
    with urllib.request.urlopen(f"https://huggingface.co/api/datasets/{REPO}", timeout=30) as r:
        metadata = json.load(r)
    revision = metadata["sha"]
    url = f"{DATASET_URL}/resolve/{revision}/officeqa_pro.csv"
    result = {
        "repository": REPO, "observed_revision": revision,
        "pinned_revision": DATA_REVISION, "gated": metadata.get("gated"),
        "last_modified": metadata.get("lastModified"),
        "method": "Anonymous public metadata and HEAD; no credentials inspected",
        "files_downloaded": 0, "user_action": ACCESS_ACTION,
    }
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as r:
            result["anonymous_file_status"] = r.status
    except urllib.error.HTTPError as error:
        result["anonymous_file_status"] = error.code
        result["error_code"] = error.headers.get("X-Error-Code")
    return result


def fetch_official(destination: Path, authorized_auth: bool) -> dict:
    if not authorized_auth:
        raise ValueError("fetch requires --authorized-auth after access was granted. " + ACCESS_ACTION)
    vendor = Path(__file__).resolve().parent / "vendor"
    if vendor.is_dir():
        sys.path.insert(0, str(vendor))
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise RuntimeError("The fetch command requires huggingface_hub in the baseline environment") from error
    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    # token=True asks the official client to use existing authentication. No token
    # value is read or logged by this adapter. No alternate mirrors are used.
    try:
        snapshot_download(
            repo_id=REPO, repo_type="dataset", revision=DATA_REVISION,
            allow_patterns=["officeqa_pro.csv", f"{CORPUS_RELATIVE.as_posix()}/*.txt"],
            local_dir=str(destination), token=True,
        )
    except Exception as error:
        # Hub errors can contain URLs or request details; print only the class.
        raise RuntimeError(f"Official dataset download failed ({type(error).__name__}). {ACCESS_ACTION}") from None
    files = [destination / "officeqa_pro.csv", *sorted((destination / CORPUS_RELATIVE).glob("*.txt"))]
    if not files[0].is_file() or len(files) != 698:
        raise ValueError("Official download is incomplete: expected one CSV and 697 transformed TXT files")
    receipt = {
        "repository": REPO, "revision": DATA_REVISION,
        "download_method": "huggingface_hub.snapshot_download; official repository; authorized authentication",
        "allow_patterns": ["officeqa_pro.csv", f"{CORPUS_RELATIVE.as_posix()}/*.txt"],
        "files": [{"path": p.relative_to(destination).as_posix(), "sha256": sha256(p), "bytes": p.stat().st_size} for p in files],
    }
    write_json(destination / "download_receipt.json", receipt)
    return {"status": "downloaded", "revision": DATA_REVISION, "csv_files": 1, "corpus_files": 697}


def select_rows(rows: list[dict], count: int, seed: str) -> list[dict]:
    if count < 1 or count > len(rows):
        raise ValueError("Requested sample size is outside the available question count")
    ids = [row["uid"] for row in rows]
    if len(set(ids)) != len(ids) or any(not uid.strip() for uid in ids):
        raise ValueError("Question IDs must be unique and nonempty")
    # Selection is independent of CSV order, answers, document hints and difficulty.
    return sorted(rows, key=lambda row: (hashlib.sha256((seed + "\0" + row["uid"]).encode()).hexdigest(), row["uid"]))[:count]


def prepare(csv_path: Path, corpus: Path, output: Path, *, count: int = 50,
            seed: str = DEFAULT_SEED, expected_questions: int = 133,
            expected_documents: int = 697, receipt_path: Path | None = None,
            reward_path: Path = REFERENCE / "reward.py") -> dict:
    if output.exists():
        raise ValueError("Output already exists; use a fresh preparation directory")
    csv_path, corpus, output = csv_path.resolve(), corpus.resolve(), output.resolve()
    if output == corpus or corpus in output.parents:
        raise ValueError("Output must be outside the input corpus")
    with csv_path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not REQUIRED_COLUMNS.issubset(reader.fieldnames or []):
            raise ValueError("CSV is missing official OfficeQA Pro columns")
        rows = list(reader)
    if len(rows) != expected_questions:
        raise ValueError(f"Expected {expected_questions} Pro questions; received {len(rows)}")
    if any(not row["question"] or not row["answer"] or row["difficulty"].strip().lower() != "hard" for row in rows):
        raise ValueError("Pro rows require a question, answer, and hard difficulty")
    selected = select_rows(rows, count, seed)
    documents = sorted(corpus.glob("*.txt"))
    if len(documents) != expected_documents or any(not p.is_file() or p.stat().st_size == 0 for p in documents):
        raise ValueError(f"Expected {expected_documents} nonempty corpus TXT files")
    if sha256(reward_path) != REWARD_SHA256:
        raise ValueError("Official reward.py does not match the pinned source hash")
    verification = "Local user-supplied files; declared revision plus content hashes, no upstream receipt"
    if receipt_path:
        receipt = json.loads(receipt_path.read_text())
        if receipt.get("repository") != REPO or receipt.get("revision") != DATA_REVISION:
            raise ValueError("Download receipt repository/revision does not match the selected dataset")
        expected = {row["path"]: row["sha256"] for row in receipt["files"]}
        for relative, path in [("officeqa_pro.csv", csv_path), *[(f"{CORPUS_RELATIVE.as_posix()}/{p.name}", p) for p in documents]]:
            if expected.get(relative) != sha256(path):
                raise ValueError("Input file does not match the official download receipt")
        verification = "All input hashes match this adapter's pinned official-download receipt"
    private, agent = output / "private", output / "agent"
    private.mkdir(parents=True, mode=0o700)
    (agent / "corpus").mkdir(parents=True)
    os.chmod(private, 0o700)
    write_jsonl(agent / "tasks.jsonl", [{"task_id": r["uid"], "question": r["question"]} for r in selected])
    write_jsonl(private / "answers.jsonl", [{"task_id": r["uid"], "answer": r["answer"]} for r in selected])
    write_jsonl(private / "provenance.jsonl", [{"task_id": r["uid"], "source_docs": r["source_docs"], "source_files": r["source_files"]} for r in selected])
    shutil.copyfile(reward_path, private / "reward.py")
    license_path = reward_path.parent / "LICENSE-APACHE"
    if license_path.is_file():
        shutil.copyfile(license_path, private / "LICENSE-APACHE")
    corpus_manifest = []
    for document in documents:
        destination = agent / "corpus" / document.name
        shutil.copyfile(document, destination)
        destination.chmod(0o444)
        corpus_manifest.append({"filename": document.name, "sha256": sha256(destination), "bytes": destination.stat().st_size})
    write_json(private / "corpus_manifest.json", corpus_manifest)
    if receipt_path:
        shutil.copyfile(receipt_path, private / "download_receipt.json")
    manifest = {
        "benchmark": "OfficeQA Pro", "track": "custom parsed-corpus search + Python; no web; fresh per-task context; no W",
        "official_headline_comparable": False,
        "reason": "A deterministic 50-question sample with parsed TXT and fixed custom tools differs from the full 133-question PDF+web agent-harness setting",
        "repository": REPO, "dataset_url": DATASET_URL, "dataset_revision": DATA_REVISION,
        "data_verification": verification, "source_csv_sha256": sha256(csv_path),
        "source_question_count": len(rows), "sample_count": count,
        "selection": {"algorithm": "ascending SHA256(UTF8(seed + NUL + uid)), uid tie-break; first count", "seed": seed},
        "task_ids": [r["uid"] for r in selected],
        "agent_tasks": "agent/tasks.jsonl", "agent_corpus": "agent/corpus",
        "corpus_document_count": len(documents), "corpus_bytes": sum(p.stat().st_size for p in documents),
        "corpus_manifest_sha256": sha256(private / "corpus_manifest.json"),
        "tasks_sha256": sha256(agent / "tasks.jsonl"), "answers_sha256": sha256(private / "answers.jsonl"),
        "grader": {"repository": "https://github.com/databricks/officeqa", "commit": CODE_REVISION, "path": "reward.py", "sha256": REWARD_SHA256, "default_tolerance": 0.0},
        "hidden_from_agent": ["answer", "source_docs", "source_files", "private/", "original CSV", "download cache", "other tasks and completed results"],
        "runtime_requirement": "Mount/read only agent/corpus and the current question; run Python in an isolated per-task workspace with no access to private files, parent directories, credentials, network, or other task workspaces. Directory permissions alone do not isolate code running as the same user.",
        "fixture_only": expected_questions != 133 or expected_documents != 697,
    }
    write_json(output / "manifest.json", manifest)
    return {"status": "prepared", "sample_count": count, "corpus_documents": len(documents), "manifest": str(output / "manifest.json"), "fixture_only": manifest["fixture_only"]}


def load_grader(reward_path: Path):
    if sha256(reward_path) != REWARD_SHA256:
        raise ValueError("Grader does not match the pinned official source")
    spec = importlib.util.spec_from_file_location("officeqa_pinned_reward", reward_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.score_answer


def grade(prepared: Path, predictions: Path, output: Path, tolerance: float = 0.0) -> dict:
    if not math.isfinite(tolerance) or tolerance < 0 or tolerance > 1:
        raise ValueError("Tolerance must be a finite fraction between 0 and 1")
    manifest = json.loads((prepared / "manifest.json").read_text())
    answer_path = prepared / "private/answers.jsonl"
    if sha256(answer_path) != manifest["answers_sha256"]:
        raise ValueError("Answer key changed after preparation")
    answers = load_jsonl(answer_path)
    if [r["task_id"] for r in answers] != manifest["task_ids"]:
        raise ValueError("Answer IDs differ from the locked task manifest")
    by_id = {}
    for row in load_jsonl(predictions):
        task_id = row.get("task_id")
        if task_id not in manifest["task_ids"] or task_id in by_id:
            raise ValueError("Predictions contain unknown or duplicate task IDs")
        if not isinstance(row.get("answer"), str):
            raise ValueError("Prediction answer must be a string")
        by_id[task_id] = row["answer"]
    scorer = load_grader(prepared / "private/reward.py")
    results = []
    for row in answers:
        task_id = row["task_id"]
        predicted = by_id.get(task_id, "")
        results.append({"task_id": task_id, "submitted": task_id in by_id,
                        "score": scorer(row["answer"], predicted, tolerance=tolerance),
                        "strict_score": scorer(row["answer"], predicted, tolerance=0.0),
                        "one_percent_score": scorer(row["answer"], predicted, tolerance=0.01)})
    result = {
        "benchmark": "OfficeQA Pro", "track": manifest["track"],
        "dataset_revision": manifest["dataset_revision"], "grader": manifest["grader"],
        "primary_tolerance": tolerance, "denominator": len(answers), "submitted": len(by_id),
        "missing": len(answers) - len(by_id), "missing_policy": "count as zero; never drop from denominator",
        "correct": sum(r["score"] for r in results),
        "accuracy": sum(r["score"] for r in results) / len(answers),
        "strict_accuracy": sum(r["strict_score"] for r in results) / len(answers),
        "one_percent_accuracy": sum(r["one_percent_score"] for r in results) / len(answers),
        "predictions_sha256": sha256(predictions), "fixture_only": manifest["fixture_only"],
        "results": results,
    }
    write_json(output, result)
    return {key: result[key] for key in ("denominator", "submitted", "missing", "accuracy", "strict_accuracy", "one_percent_accuracy", "fixture_only")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    access = commands.add_parser("check-access", help="Anonymous access check; does not inspect credentials")
    access.add_argument("--output", type=Path, required=True)
    fetch = commands.add_parser("fetch", help="Download official pinned data using already-authorized authentication")
    fetch.add_argument("--output", type=Path, required=True)
    fetch.add_argument("--authorized-auth", action="store_true")
    prep = commands.add_parser("prepare", help="Separate local authorized questions/corpus from private grading data")
    prep.add_argument("--csv", type=Path, required=True)
    prep.add_argument("--corpus", type=Path, required=True)
    prep.add_argument("--receipt", type=Path)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--count", type=int, default=50)
    prep.add_argument("--seed", default=DEFAULT_SEED)
    scoring = commands.add_parser("grade", help="Score only after predictions are saved, outside the agent process")
    scoring.add_argument("--prepared", type=Path, required=True)
    scoring.add_argument("--predictions", type=Path, required=True)
    scoring.add_argument("--output", type=Path, required=True)
    scoring.add_argument("--tolerance", type=float, default=0.0)
    args = parser.parse_args()
    try:
        if args.command == "check-access":
            result = inspect_public_access()
            write_json(args.output, result)
        elif args.command == "fetch":
            result = fetch_official(args.output, args.authorized_auth)
        elif args.command == "prepare":
            result = prepare(args.csv, args.corpus, args.output, count=args.count, seed=args.seed, receipt_path=args.receipt)
        else:
            result = grade(args.prepared, args.predictions, args.output, args.tolerance)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        # No questions, answers, tokens or download response bodies are printed.
        print(json.dumps({"status": "error", "message": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
