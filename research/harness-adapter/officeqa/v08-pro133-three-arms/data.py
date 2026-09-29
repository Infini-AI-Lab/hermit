"""Frozen OfficeQA data and evaluator-only grading for the 133-task (full OfficeQA Pro) v8 runs.

This module is controller code, never an actor or learner tool. Only the corpus
directory and its SQLite index may be exposed to actors. The task list, private
keys, prediction seal, and final grading outputs stay outside every model sandbox. A controller may release
only the just-completed batch reference answers (and, in full profiles, that
batch's strict/1% grader verdicts) to its learner after sealing that batch; the
private master answer key and source mappings are never mounted.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Mapping


from paths import DATA_CACHE, SOURCE
PROJECT = SOURCE
DEFAULT_ROOT = Path(__file__).resolve().parent
SEED = "metaworld-officeqa-pro-codex70-online-v1"
# v8: all 133 OfficeQA Pro questions. Selection is a SHA-256 sort under the same seed,
# so the first 50 (and 70) are exactly the order used by v3-v7.
COUNT = 133
from experiment import ARMS


def _module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_legacy = _module("officeqa50_gold_reference_prepare", "officeqa_prepare.py")
_tools = _module("officeqa50_gold_reference_tools", "officeqa_tools.py")


def sha256(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _ids_hash(ids: list[str]) -> str:
    return hashlib.sha256(json.dumps(ids, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _write_new(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _prepared(root: Path) -> Path:
    return Path(root).resolve() / "prepared"


def prepare(root: Path = DEFAULT_ROOT) -> dict:
    """Create root/prepared exactly once, verifying the authorized local receipt."""
    cache = DATA_CACHE
    return _prepare_from_sources(
        root, csv_path=cache / "officeqa_pro.csv",
        corpus=cache / "treasury_bulletins_parsed/transformed",
        receipt=cache / "download_receipt.json",
    )


def _prepare_from_sources(root: Path, *, csv_path: Path, corpus: Path,
                          receipt: Path | None = None,
                          expected_questions: int = 133,
                          expected_documents: int = 697) -> dict:
    """Shared implementation; alternate sizes exist only for synthetic tests."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = _prepared(root)
    if destination.exists():
        raise ValueError("Prepared data already exists; refusing to overwrite")
    lock = root / ".data-prepare.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        if destination.exists():
            raise ValueError("Prepared data already exists; refusing to overwrite")
        with tempfile.TemporaryDirectory(prefix=".data-stage-", dir=root) as temporary:
            staging = Path(temporary) / "prepared"
            _legacy.prepare(
                Path(csv_path), Path(corpus), staging, count=COUNT, seed=SEED,
                expected_questions=expected_questions,
                expected_documents=expected_documents, receipt_path=receipt,
            )
            index = staging / "agent/corpus.sqlite"
            index_metadata = _tools.build_index(
                staging / "agent/corpus", index, expected_documents=expected_documents,
            )
            _write_new(staging / "index-metadata.json", index_metadata)
            manifest = _read(staging / "manifest.json")
            manifest.update({
                "protocol": "officeqa-pro133-v8",
                "track": "All 133 OfficeQA Pro questions; actual Codex harness; full provided parsed TXT corpus; network-restricted; completed-batch reference-answer supervised GLM-only online adaptation",
                "reason": "Parsed TXT, no web, and post-batch gold-supervised adapter updates differ from the official PDF-and-web headline evaluation",
                "arms": list(ARMS),
                "model": "accounts/fireworks/models/glm-5p3-flash",
                "harness_requirement": "The campaign must use actual Codex for both arms; this data preparation does not attest runtime execution",
                "agent_index": "agent/corpus.sqlite",
                "ordered_task_ids_sha256": _ids_hash(manifest["task_ids"]),
                "index_sha256": sha256(index),
                "index_metadata_sha256": sha256(staging / "index-metadata.json"),
                "preparation_source_sha256": {
                    "data.py": sha256(Path(__file__)),
                    "officeqa_prepare.py": sha256(SOURCE / "officeqa_prepare.py"),
                    "officeqa_tools.py": sha256(SOURCE / "officeqa_tools.py"),
                },
                "actor_read_allowlist": ["agent/corpus/", "agent/corpus.sqlite"],
                "task_delivery": "Controller delivers only the current question. agent/tasks.jsonl is controller-only and must not be mounted with its parent directory",
                "learner_policy": {
                    "learn_every": 5, "feedback_model": "accounts/fireworks/models/glm-5p3-flash",
                    "source": "The current five adapted-arm questions, own predictions/public tool trajectories and officially keyed reference answers released only after that batch is sealed",
                    "includes_gold": True, "reference_scope": "completed_batch_only",
                    "master_answer_key_mounted": False, "source_hints_released": False,
                    "learning_type": "Gold-supervised online Markdown-memory adaptation; model weights unchanged",
                    "forbidden": ["master answer key", "unreleased current/future reference answers", "gold source mappings", "grader scores and rationales", "future questions", "baseline-arm trajectories", "earlier experiments", "human or Astra task-specific feedback"],
                    "first_batch_uses": "W0",
                    "last_scored_batch_uses": "Latest mechanically admitted W from rounds 1–9; prior W retained after rejected updates",
                    "after_final_batch": "No learning round after the last block; nothing learned revises a sealed prediction",
                },
                "grading_policy": {
                    "after": "Every configured arm has sealed one prediction per selected task",
                    "primary_tolerance": 0.0, "secondary_tolerance": 0.01,
                    "empty_answer_score": 0, "denominator_per_arm": COUNT,
                    "model_judge": False, "send_results_to_learner": False,
                },
                "selection_compatibility": "Deterministic order under the v3-v7 seed; its first 50 are the v4-v7 questions in the same order; never selected using prior performance",
                "actor_budgets": {"requests": None, "seconds": 1800, "completion_tokens": 65536, "per_response": 65536},
                "hidden_from_agent": ["agent/tasks.jsonl", "answer", "source_docs", "source_files", "private/", "original CSV", "download cache", "other tasks and completed results", "private-evaluation/"],
                "runtime_requirement": "Enforced filesystem isolation: corpus and index read-only plus current task workspace. Deny all other project files, task list, private keys, other workspaces, credentials, and general network. Inference transport may reach only the fixed Fireworks endpoint. A folder alone is not isolation.",
            })
            from experiment import FULL, ARCHITECTURE, ACTOR_SECONDS
            if FULL:
                label = 'none' if ARMS == ('baseline',) else ARCHITECTURE
                manifest.update(protocol=f'officeqa-pro133-v8-{label}', arms=list(ARMS),
                    track=f'All 133 ordered tasks; native Codex; arms {"+".join(ARMS)}; {ARCHITECTURE} architecture for an adapted arm; unbounded inference; '
                          + ('completed-batch GLM card proposals verified by actor re-runs' if ARCHITECTURE == 'cards'
                             else 'completed-batch GLM postmortem with released grades'),
                    actor_budgets={'requests':None,'seconds':ACTOR_SECONDS,'completion_tokens':None,'per_response':None})
                policy=manifest['learner_policy']
                policy['learning_type']={'mwm':'Full native MWM artifacts, corpus investigation, calculations, tested programs and roles; model weights unchanged',
                    'prompt_only':'ADAPTER.md text (procedure, conventions, lessons with controller-replayed status lines) served verbatim inside AGENTS.md; corpus investigation and calculations; model weights unchanged',
                    'cards':'A deck of cards (scope, claim, unless) compiled into AGENTS.md; a card is admitted only after the actor is re-run with it on its support and control questions; model weights unchanged'}[ARCHITECTURE]
                policy['forbidden']=['grader scores for unreleased batches' if x=='grader scores and rationales' else x for x in policy['forbidden']]
                policy.update(completed_batch_grades_released=True, reference_shelf='extracted reference text, learner only')
                manifest['grading_policy']['send_results_to_learner']='completed_batch_only'
            (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            for relative in ("agent/tasks.jsonl", "manifest.json", "index-metadata.json", "private/answers.jsonl", "private/provenance.jsonl", "private/reward.py"):
                (staging / relative).chmod(0o400 if relative.startswith("private/") else 0o444)
            staging.rename(destination)
        return manifest
    finally:
        lock.unlink(missing_ok=True)


def load_tasks(root: Path = DEFAULT_ROOT) -> list[dict]:
    """Controller-only sanitized task records, in the frozen experiment order."""
    prepared = _prepared(root)
    manifest = _read(prepared / "manifest.json")
    tasks_file = prepared / "agent/tasks.jsonl"
    if sha256(tasks_file) != manifest["tasks_sha256"]:
        raise ValueError("Task records changed after preparation")
    tasks = _legacy.load_jsonl(tasks_file)
    if len(tasks) != COUNT or any(set(row) != {"task_id", "question"} for row in tasks):
        raise ValueError(f"Task records must contain {COUNT} question-only records")
    if any(not isinstance(row["question"], str) or not row["question"].strip() for row in tasks):
        raise ValueError("Task questions must be nonempty text")
    ids = [row["task_id"] for row in tasks]
    if len(set(ids)) != COUNT or ids != manifest["task_ids"] or _ids_hash(ids) != manifest["ordered_task_ids_sha256"]:
        raise ValueError("Task ID order differs from the frozen manifest")
    return tasks


def verify_prepared(root: Path = DEFAULT_ROOT, *, verify_corpus: bool = True) -> dict:
    """Hash and schema validation only; never return questions or gold answers."""
    prepared = _prepared(root)
    manifest = _read(prepared / "manifest.json")
    tasks = load_tasks(root)
    if manifest["selection"]["seed"] != SEED or manifest["sample_count"] != COUNT:
        raise ValueError("Unexpected question selection protocol")
    fixed_hashes = {
        "private/answers.jsonl": "answers_sha256",
        "private/corpus_manifest.json": "corpus_manifest_sha256",
        "agent/corpus.sqlite": "index_sha256",
        "index-metadata.json": "index_metadata_sha256",
    }
    for filename, key in fixed_hashes.items():
        if sha256(prepared / filename) != manifest[key]:
            raise ValueError(f"Prepared input changed: {filename}")
    if sha256(prepared / "private/reward.py") != _legacy.REWARD_SHA256:
        raise ValueError("Official scoring code changed")
    corpus_manifest = _read(prepared / "private/corpus_manifest.json")
    actual = sorted(path.name for path in (prepared / "agent/corpus").glob("*.txt"))
    if actual != sorted(row["filename"] for row in corpus_manifest) or len(actual) != manifest["corpus_document_count"]:
        raise ValueError("Corpus file membership changed")
    if verify_corpus:
        for row in corpus_manifest:
            path = prepared / "agent/corpus" / row["filename"]
            if path.is_symlink() or path.stat().st_size != row["bytes"] or sha256(path) != row["sha256"]:
                raise ValueError("Corpus document changed")
    with sqlite3.connect((prepared / "agent/corpus.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        metadata = {key: json.loads(value) for key, value in connection.execute("SELECT key, value FROM metadata")}
        document_count = connection.execute("SELECT COUNT(DISTINCT filename) FROM chunks").fetchone()[0]
    if metadata != _read(prepared / "index-metadata.json") or document_count != len(actual):
        raise ValueError("Corpus index metadata changed")
    if metadata["uses_answers"] or metadata["uses_question_source_hints"]:
        raise ValueError("Index declares prohibited answer-side inputs")
    return {"verified": True, "task_count": len(tasks), "corpus_documents": len(actual),
            "corpus_hashes_checked": verify_corpus, "index_sha256": manifest["index_sha256"],
            "ordered_task_ids_sha256": manifest["ordered_task_ids_sha256"],
            "fixture_only": manifest["fixture_only"]}


def _validated_predictions(path: Path, ids: list[str]) -> list[dict]:
    records = _legacy.load_jsonl(path)
    if any(set(record) != {"task_id", "answer"} for record in records):
        raise ValueError("Predictions must contain task_id and answer only")
    if any(not isinstance(record["answer"], str) for record in records):
        raise ValueError("Every answer must be text; use an empty string for a failed task")
    actual = [record["task_id"] for record in records]
    if len(actual) != COUNT or len(set(actual)) != COUNT or set(actual) != set(ids):
        raise ValueError(f"Each arm must seal exactly {COUNT} unique selected task IDs, including explicit empty answers")
    return records


def seal_predictions(root: Path, predictions: Mapping[str, Path]) -> Path:
    """Seal every configured arm's complete prediction file together, before evaluator access."""
    root = Path(root).resolve()
    if set(predictions) != set(ARMS):
        raise ValueError("Prediction arms must be exactly the configured arms")
    prepared = _prepared(root)
    manifest = _read(prepared / "manifest.json")
    load_tasks(root)
    paths = {arm: Path(predictions[arm]).resolve() for arm in ARMS}
    if len(ARMS) == 2 and (paths[ARMS[0]] == paths[ARMS[1]] or os.path.samefile(paths[ARMS[0]], paths[ARMS[1]])):
        raise ValueError("The arms must have distinct prediction files")
    records = {arm: _validated_predictions(path, manifest["task_ids"]) for arm, path in paths.items()}
    for path in paths.values():
        if prepared == path or prepared in path.parents:
            raise ValueError("Prediction files must stay outside prepared benchmark inputs")
    seal = {
        "protocol": manifest["protocol"], "sealed_utc": datetime.now(timezone.utc).isoformat(),
        "total_predictions": COUNT * len(ARMS),
        "manifest_sha256": sha256(prepared / "manifest.json"),
        "ordered_task_ids_sha256": manifest["ordered_task_ids_sha256"],
        "arms": {arm: {"path": str(paths[arm]), "sha256": sha256(paths[arm]),
                       "count": len(records[arm]), "empty_answers": sum(not row["answer"].strip() for row in records[arm])}
                 for arm in ARMS},
        "note": "Logical content seal validated again before grading; filesystem modes are not a security boundary",
    }
    evaluation = root / "private-evaluation"
    evaluation.mkdir(mode=0o700, exist_ok=True)
    evaluation.chmod(0o700)
    destination = evaluation / "predictions-seal.json"
    _write_new(destination, seal)
    destination.chmod(0o400)
    for path in paths.values():
        path.chmod(0o400)
    return destination


def grade(root: Path = DEFAULT_ROOT, seal_path: Path | None = None) -> dict:
    """Evaluator-only official scoring. Requires a valid completed seal of every configured arm."""
    root = Path(root).resolve()
    prepared = _prepared(root)
    evaluation = root / "private-evaluation"
    seal_path = Path(seal_path) if seal_path else evaluation / "predictions-seal.json"
    if not seal_path.is_file():
        raise ValueError("Both arms must be sealed before any grading")
    seal = _read(seal_path)
    manifest = _read(prepared / "manifest.json")
    if seal.get("total_predictions") != COUNT * len(ARMS) or set(seal.get("arms", {})) != set(ARMS):
        raise ValueError("Prediction seal is incomplete")
    if seal.get("manifest_sha256") != sha256(prepared / "manifest.json") or seal.get("ordered_task_ids_sha256") != manifest["ordered_task_ids_sha256"]:
        raise ValueError("Prepared manifest changed after sealing")
    if (evaluation / "paired-metrics.json").exists() or any((evaluation / f"{arm}-grades.json").exists() for arm in ARMS):
        raise ValueError("Grade outputs already exist; refusing to overwrite")
    for arm in ARMS:
        entry = seal["arms"][arm]
        path = Path(entry["path"])
        if sha256(path) != entry["sha256"]:
            raise ValueError("Predictions changed after sealing")
        _validated_predictions(path, manifest["task_ids"])
    verify_prepared(root, verify_corpus=True)
    results = {}
    for arm in ARMS:
        destination = evaluation / f"{arm}-grades.json"
        _legacy.grade(prepared, Path(seal["arms"][arm]["path"]), destination, tolerance=0.0)
        results[arm] = _read(destination)
    by_arm = {arm: {row["task_id"]: row for row in results[arm]["results"]} for arm in ARMS}
    pairs = [{"task_id": task_id, **{arm: by_arm[arm][task_id]["strict_score"] for arm in ARMS}}
             for task_id in manifest["task_ids"]]
    metrics = {
        "benchmark": "OfficeQA Pro", "protocol": manifest["protocol"],
        "fixture_only": manifest["fixture_only"], "official_headline_comparable": False,
        "graded_utc": datetime.now(timezone.utc).isoformat(), "seal_sha256": sha256(seal_path),
        "denominator_per_arm": COUNT, "primary_tolerance": 0.0, "secondary_tolerance": 0.01,
        "arms": {arm: {key: results[arm][key] for key in ("correct", "strict_accuracy", "one_percent_accuracy", "submitted", "missing")}
                 for arm in ARMS},
        "results": pairs,
        "feedback_policy": "Final scores are evaluator-only, not learner inputs. Official reference answers for each completed batch are separately controller-released after its prediction seal; this is gold-supervised online memory adaptation.",
    }
    if len(ARMS) == 2:
        metrics["strict_delta_percentage_points"] = 100 * (results["adapted"]["strict_accuracy"] - results["baseline"]["strict_accuracy"])
        metrics["paired_strict"] = {
            "both_correct": sum(row["baseline"] == row["adapted"] == 1 for row in pairs),
            "both_wrong": sum(row["baseline"] == row["adapted"] == 0 for row in pairs),
            "adapted_only": sum(row["baseline"] == 0 and row["adapted"] == 1 for row in pairs),
            "baseline_only": sum(row["baseline"] == 1 and row["adapted"] == 0 for row in pairs),
        }
    _write_new(evaluation / "paired-metrics.json", metrics)
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "verify", "grade"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    result = {"prepare": prepare, "verify": verify_prepared, "grade": grade}[args.command](args.root)
    if args.command == "prepare":
        result = {"prepared": True, "task_count": result["sample_count"], "corpus_documents": result["corpus_document_count"], "fixture_only": result["fixture_only"]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
