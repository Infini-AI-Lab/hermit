"""Frozen OfficeQA data, reference release and evaluator-only grading for v9 single-arm roots.

This module is controller code, never an actor or learner tool. Actors may read only the corpus
directory, its FTS5 index and (arms with L1 tools) the tables index; the task list, private keys,
prediction seals and grades stay outside every model sandbox.

v9 changes against v8 (DESIGN §1, §4; validity review P7):
- One arm per root, named by its contracts.ArmSpec. The manifest records the arm spec, the repeat, the
  learning type and the issue-47 list whose references are withheld from both learners.
- The OfficeQA tables index (L1 tools) is copied into `prepared/agent/tables/tables.sqlite` at
  preparation, and its recorded corpus hash must equal the hash of this root's corpus.
- `release_references` gives the controller the references of one sealed block, minus the withheld ids.
- `official_scorer` is the pinned `reward.py` (sha-checked); campaign.py uses it to grade released blocks
  for the learners (strict and 1%), and `grade` uses it for the final metrics.
- `seal_predictions` / `grade` take one arm; `grade` reports all 133 and the undisputed subset.

Wave 3 (pilot): a root whose campaign spec declares a `subset` (arms.SubsetSpec) prepares the same 133
questions (every hash and the base order unchanged) and records the subset in the manifest; the subset
is what the root seals, grades and releases (`selected_task_ids`), and its hash is checked by
`verify_prepared` / `check_subset`, `seal_predictions` and `grade`. Without a subset nothing changes.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Callable, Mapping, Sequence


from paths import DATA_CACHE, SOURCE, FROZEN, _config
PROJECT = SOURCE
DEFAULT_ROOT = Path(__file__).resolve().parent
SEED = "metaworld-officeqa-pro-codex70-online-v1"
# All 133 OfficeQA Pro questions. The base order (SHA-256 under SEED) is the v3-v8 order; each v9 repeat
# then runs its own seeded permutation of it (arms.question_order).
COUNT = 133
MODEL = "accounts/fireworks/models/glm-5p3-flash"
# databricks/officeqa#47: 15 answer updates + 3 clarification examples. Scored like every other question
# (primary metric on all 133; the same sign is required on the other 115), but their references are
# withheld from both learners (validity review P7).
ISSUE_47 = ('UID0001', 'UID0004', 'UID0007', 'UID0049', 'UID0056', 'UID0065', 'UID0093', 'UID0124',
            'UID0140', 'UID0154', 'UID0155', 'UID0179', 'UID0190', 'UID0194', 'UID0238',
            'UID0175', 'UID0213', 'UID0237')
# The built OfficeQA tables index (S2): .task-paths.json "tables_index" or the frameworks-v9 copy.
TABLES_SOURCE = Path(_config.get('tables_index', FROZEN / 'officeqa-tables-index/tables.sqlite')).absolute()
TABLES_RELATIVE = 'agent/tables/tables.sqlite'
TABLES_GLOB = re.compile(r'^treasury_bulletin_.*\.txt$')


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
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _prepared(root: Path) -> Path:
    return Path(root).resolve() / "prepared"


def _spec_default():
    from experiment import CAMPAIGN
    if CAMPAIGN is None:
        raise ValueError("This directory has no v9 campaign spec (.task-paths.json 'arm')")
    return CAMPAIGN

# ---------------------------------------------------------------------------------------------
# Tables index (L1): copied per root, bound to this root's corpus by hash
# ---------------------------------------------------------------------------------------------


def tables_corpus_hash(corpus_manifest: Sequence[Mapping]) -> str:
    """The corpus hash the tables index records (otables.build_index): sha256 of the sorted
    [{file, bytes, sha256}] of the treasury_bulletin_*.txt files, json.dumps(sort_keys=True)."""
    rows = sorted((r for r in corpus_manifest if TABLES_GLOB.match(r["filename"])), key=lambda r: r["filename"])
    manifest = [{"file": r["filename"], "bytes": r["bytes"], "sha256": r["sha256"]} for r in rows]
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def tables_meta(index: Path) -> dict:
    uri = Path(index).resolve().as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        return {key: json.loads(value) for key, value in connection.execute("SELECT key, value FROM meta")}


def tables_index_version(index: Path) -> str:
    """The `version` a tables index records, refused (ValueError) unless it is the version the pack's tools
    build (`otables.INDEX_VERSION`). The v10 tools open an older index without error and silently lose the
    features its tables carry (v9.1: no narrative section tables, so W0-12 prints nothing), which a campaign
    must never do unnoticed (integration finding 2026-09-29)."""
    from packs.officeqa.tools import otables
    version = tables_meta(index).get("version")
    if version != otables.INDEX_VERSION:
        raise ValueError(f"Tables index version {version!r} at {index}; the tools need {otables.INDEX_VERSION!r}")
    return version


def install_tables_index(prepared: Path, source: Path = TABLES_SOURCE) -> dict:
    """Copy a built tables index into <prepared>/agent/tables/ (0444) after checking that it indexes
    exactly this corpus and carries the tools' index version. Returns the facts the manifest records."""
    prepared = Path(prepared)
    corpus_manifest = _read(prepared / "private/corpus_manifest.json")
    expected = tables_corpus_hash(corpus_manifest)
    meta = tables_meta(source)
    if meta.get("corpus_manifest_sha256") != expected:
        raise ValueError("Tables index was built from a different corpus")
    tables_index_version(source)
    destination = prepared / TABLES_RELATIVE
    if destination.exists():
        raise ValueError("Tables index already installed")
    destination.parent.mkdir(parents=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o444)
    return {"tables_index": TABLES_RELATIVE, "tables_index_sha256": sha256(destination),
            "tables_index_meta": meta, "tables_index_source": str(Path(source).resolve())}


def verify_tables_index(root: Path) -> dict:
    prepared = _prepared(root)
    manifest = _read(prepared / "manifest.json")
    index = prepared / TABLES_RELATIVE
    if index.is_symlink() or not index.is_file():
        raise ValueError("Tables index missing")
    if sha256(index) != manifest["tables_index_sha256"]:
        raise ValueError("Tables index changed after preparation")
    meta = tables_meta(index)
    expected = tables_corpus_hash(_read(prepared / "private/corpus_manifest.json"))
    if meta.get("corpus_manifest_sha256") != expected or meta != manifest["tables_index_meta"]:
        raise ValueError("Tables index does not index this root's corpus")
    return {"tables_index_sha256": manifest["tables_index_sha256"], "tables": meta.get("tables"),
            "files": meta.get("files"), "corpus_manifest_sha256": expected, "version": meta.get("version"),
            "sections": meta.get("sections")}

# ---------------------------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------------------------


def subset_json(spec) -> dict | None:
    """The manifest/seal record of a campaign spec's declared subset (None for a full run)."""
    subset = getattr(spec, "subset", None)
    return subset.to_json() if subset is not None else None


def selected_task_ids(manifest: Mapping) -> list[str]:
    """The ids this root seals and grades, in the base order: the declared subset (its hash re-checked;
    every id must be prepared) or every prepared id."""
    if manifest.get("subset") is None:
        return list(manifest["task_ids"])
    import arms
    return arms.selected_ids(manifest["task_ids"], arms.parse_subset(manifest["subset"]))


def check_subset(root: Path, spec) -> dict | None:
    """The prepared manifest records exactly the campaign spec's subset (or none), and the subset's hash and
    membership hold. Returns {name, sha256, count} or None."""
    manifest = _read(_prepared(root) / "manifest.json")
    if manifest.get("subset") != subset_json(spec):
        raise ValueError("Prepared manifest subset differs from the campaign spec (.task-paths.json 'subset')")
    if manifest.get("subset") is None:
        return None
    ids = selected_task_ids(manifest)
    return {"name": manifest["subset"]["name"], "sha256": manifest["subset"]["sha256"], "count": len(ids)}


def manifest_v9(manifest: dict, spec, *, tables: Mapping, actor_seconds: int) -> dict:
    """The v9 fields of prepared/manifest.json for one arm root."""
    import arms
    arm = spec.arm
    label = f"{arm.name}-r{spec.repeat}"
    withheld = [task for task in manifest["task_ids"] if task in ISSUE_47]
    subset = subset_json(spec)
    selected = arms.selected_ids(manifest["task_ids"], spec.subset) if subset else list(manifest["task_ids"])
    selected_withheld = [task for task in selected if task in ISSUE_47]
    scope = (f"The declared {len(selected)}-question subset {subset['name']} of the 133 OfficeQA Pro questions"
             if subset else "All 133 OfficeQA Pro questions")
    manifest = dict(manifest)
    manifest.update({
        "protocol": f"officeqa-pro133-v9-{label}",
        "track": (f"{scope}; native Codex; v9 arm {arm.name} (repeat {spec.repeat}); "
                  "parsed TXT corpus; network-restricted; question order randomised per repeat"),
        "reason": "Parsed TXT, no web, fixed local tools and (learner arms) post-batch reference-supervised adapter updates differ from the official PDF-and-web headline evaluation",
        "arm": arm.name, "arm_spec": asdict(spec.arm), "architecture": arms.architecture(arm),
        "repeat": spec.repeat, "pack": spec.pack, "order_seed": spec.order_seed,
        "model": MODEL,
        "harness_requirement": "The campaign must use actual Codex; this data preparation does not attest runtime execution",
        "agent_index": "agent/corpus.sqlite",
        "ordered_task_ids_sha256": _ids_hash(manifest["task_ids"]),
        "index_sha256": manifest["index_sha256"],
        **tables,
        "preparation_source_sha256": {
            "data.py": sha256(Path(__file__)),
            "officeqa_prepare.py": sha256(SOURCE / "officeqa_prepare.py"),
            "officeqa_tools.py": sha256(SOURCE / "officeqa_tools.py"),
        },
        "actor_read_allowlist": ["agent/corpus/", "agent/corpus.sqlite"] + (["agent/tables/"] if arm.l1_tools else []),
        "task_delivery": "Controller delivers only the current question. agent/tasks.jsonl is controller-only and must not be mounted with its parent directory",
        "learner_policy": {
            "learner": arm.learner, "learn_every": 5, "feedback_model": MODEL,
            "learning_type": arms.learning_type(arm),
            "includes_gold": arm.learner is not None, "reference_scope": "released_blocks_only" if arm.learner else "none",
            "withheld_reference_ids": withheld,
            "withheld_reason": "databricks/officeqa#47 lists these answers as questionable; scored, never released to a learner",
            "master_answer_key_mounted": False, "source_hints_released": False,
            "completed_batch_grades_released": arm.learner is not None,
            "forbidden": ["master answer key", "unreleased current/future reference answers",
                          "withheld (issue-47) references", "gold source mappings", "grader code",
                          "future questions", "other-arm trajectories", "earlier experiments",
                          "human or Astra task-specific feedback"],
            "first_batch_uses": "W0" if arm.seed != "none" else "no adapter",
            "after_final_batch": "No learning round after the last block; nothing learned revises a sealed prediction",
        },
        "grading_policy": {
            "after": "This arm has sealed one prediction per selected task",
            "primary_tolerance": 0.0, "secondary_tolerance": 0.01,
            "empty_answer_score": 0, "denominator_per_arm": len(selected),
            "subsets": {"all": len(selected), "undisputed": len(selected) - len(selected_withheld)},
            "model_judge": False,
            "send_results_to_learner": "released_blocks_only" if arm.learner else False,
        },
        "selection_compatibility": "Base order = deterministic SHA-256 order under the v3-v8 seed; each repeat runs arms.question_order(base ids, order_seed, repeat); never selected using prior performance",
        "actor_budgets": {"requests": None, "seconds": actor_seconds, "completion_tokens": None, "per_response": None},
        "hidden_from_agent": ["agent/tasks.jsonl", "answer", "source_docs", "source_files", "private/", "original CSV",
                              "download cache", "other tasks and completed results", "private-evaluation/",
                              "adapters/ and evolution/ (served only as rendered AGENTS.md text and broker-executed rules)"],
        "runtime_requirement": "Enforced filesystem isolation: corpus, FTS index and (L1 arms) tables index read-only plus the current task workspace; kit code granted file by file. Deny all other project files, task list, private keys, other workspaces, credentials, and general network. Inference transport may reach only the local broker port.",
    })
    if subset:
        manifest["subset"] = subset
    return manifest


def prepare(root: Path = DEFAULT_ROOT, spec=None, *, tables_source: Path = TABLES_SOURCE) -> dict:
    """Create root/prepared exactly once, verifying the authorized local receipt (reads the official
    download: controller-side only; run once per root by the operator)."""
    cache = DATA_CACHE
    return _prepare_from_sources(
        root, spec or _spec_default(), csv_path=cache / "officeqa_pro.csv",
        corpus=cache / "treasury_bulletins_parsed/transformed",
        receipt=cache / "download_receipt.json", tables_source=tables_source,
    )


def _prepare_from_sources(root: Path, spec, *, csv_path: Path, corpus: Path, receipt: Path | None = None,
                          tables_source: Path = TABLES_SOURCE, expected_questions: int = 133,
                          expected_documents: int = 697) -> dict:
    from experiment import ACTOR_SECONDS
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = _prepared(root)
    if destination.exists():
        raise ValueError("Prepared data already exists; refusing to overwrite")
    lock = root / ".data-prepare.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with tempfile.TemporaryDirectory(prefix=".data-stage-", dir=root) as temporary:
            staging = Path(temporary) / "prepared"
            _legacy.prepare(Path(csv_path), Path(corpus), staging, count=COUNT, seed=SEED,
                            expected_questions=expected_questions, expected_documents=expected_documents,
                            receipt_path=receipt)
            index = staging / "agent/corpus.sqlite"
            index_metadata = _tools.build_index(staging / "agent/corpus", index, expected_documents=expected_documents)
            _write_new(staging / "index-metadata.json", index_metadata)
            manifest = _read(staging / "manifest.json")
            manifest["index_sha256"] = sha256(index)
            manifest["index_metadata_sha256"] = sha256(staging / "index-metadata.json")
            tables = install_tables_index(staging, tables_source)
            manifest = manifest_v9(manifest, spec, tables=tables, actor_seconds=ACTOR_SECONDS)
            (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            for relative in ("agent/tasks.jsonl", "manifest.json", "index-metadata.json", "private/answers.jsonl",
                             "private/provenance.jsonl", "private/reward.py"):
                (staging / relative).chmod(0o400 if relative.startswith("private/") else 0o444)
            staging.rename(destination)
        return manifest
    finally:
        lock.unlink(missing_ok=True)


def load_tasks(root: Path = DEFAULT_ROOT) -> list[dict]:
    """Controller-only sanitized task records, in the frozen base order."""
    prepared = _prepared(root)
    manifest = _read(prepared / "manifest.json")
    tasks_file = prepared / "agent/tasks.jsonl"
    if sha256(tasks_file) != manifest["tasks_sha256"]:
        raise ValueError("Task records changed after preparation")
    tasks = _legacy.load_jsonl(tasks_file)
    count = manifest["sample_count"]
    if len(tasks) != count or any(set(row) != {"task_id", "question"} for row in tasks):
        raise ValueError(f"Task records must contain {count} question-only records")
    if any(not isinstance(row["question"], str) or not row["question"].strip() for row in tasks):
        raise ValueError("Task questions must be nonempty text")
    ids = [row["task_id"] for row in tasks]
    if len(set(ids)) != count or ids != manifest["task_ids"] or _ids_hash(ids) != manifest["ordered_task_ids_sha256"]:
        raise ValueError("Task ID order differs from the frozen manifest")
    return tasks


def verify_prepared(root: Path = DEFAULT_ROOT, *, verify_corpus: bool = True, spec=None) -> dict:
    """Hash and schema validation only; never return questions or gold answers."""
    prepared = _prepared(root)
    manifest = _read(prepared / "manifest.json")
    tasks = load_tasks(root)
    if manifest["selection"]["seed"] != SEED or manifest["sample_count"] != COUNT:
        raise ValueError("Unexpected question selection protocol")
    spec = spec or _spec_default()
    if (manifest.get("arm_spec") != asdict(spec.arm) or manifest.get("repeat") != spec.repeat
            or manifest.get("order_seed") != spec.order_seed or manifest.get("pack") != spec.pack):
        raise ValueError("Prepared manifest belongs to another arm/repeat/order seed")
    if manifest["learner_policy"]["withheld_reference_ids"] != [t for t in manifest["task_ids"] if t in ISSUE_47]:
        raise ValueError("Withheld reference list differs from issue #47")
    subset = check_subset(root, spec)
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
    tables = verify_tables_index(root)
    return {"verified": True, "task_count": len(tasks), "corpus_documents": len(actual),
            "corpus_hashes_checked": verify_corpus, "index_sha256": manifest["index_sha256"],
            "tables": tables, "ordered_task_ids_sha256": manifest["ordered_task_ids_sha256"],
            "withheld_reference_count": len(manifest["learner_policy"]["withheld_reference_ids"]),
            "fixture_only": manifest["fixture_only"], **({"subset": subset} if subset else {})}


def withheld_ids(root: Path = DEFAULT_ROOT) -> list[str]:
    return list(_read(_prepared(root) / "manifest.json")["learner_policy"]["withheld_reference_ids"])

# ---------------------------------------------------------------------------------------------
# Reference release (learner arms) and the official scorer
# ---------------------------------------------------------------------------------------------


def release_references(root: Path, task_ids: Sequence[str]) -> dict[str, str | None]:
    """Controller-only: the references of exactly these (sealed) task ids; None for a withheld id.
    Reads the private key once and keeps only the wanted rows."""
    prepared = _prepared(root)
    manifest = _read(prepared / "manifest.json")
    answers = prepared / "private/answers.jsonl"
    if sha256(answers) != manifest["answers_sha256"]:
        raise ValueError("Answer key changed after preparation")
    withheld = set(manifest["learner_policy"]["withheld_reference_ids"])
    wanted = set(task_ids)
    if len(wanted) != len(task_ids) or not wanted <= set(selected_task_ids(manifest)):
        raise ValueError("Release request names unknown or duplicate task ids")
    keys: dict[str, str | None] = {}
    with answers.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row["task_id"] in wanted:
                if row["task_id"] in keys:
                    raise ValueError("Duplicate reference key")
                keys[row["task_id"]] = None if row["task_id"] in withheld else row["answer"]
    if set(keys) != wanted:
        raise ValueError("Missing reference for a released task")
    return {task: keys[task] for task in task_ids}


def official_scorer(root: Path = DEFAULT_ROOT) -> Callable[..., float]:
    """The pinned official `reward.py` score_answer(reference, predicted, tolerance=...) (sha-checked).
    Controller-side only; never handed to an actor or learner sandbox."""
    return _legacy.load_grader(_prepared(root) / "private/reward.py")


def grade_pair(scorer: Callable[..., float], reference: str, predicted: str) -> dict:
    return {"strict": float(scorer(reference, predicted, tolerance=0.0)),
            "one_percent": float(scorer(reference, predicted, tolerance=0.01))}

# ---------------------------------------------------------------------------------------------
# Final seal and grade (one arm per root)
# ---------------------------------------------------------------------------------------------


def _validated_predictions(path: Path, ids: list[str]) -> list[dict]:
    records = _legacy.load_jsonl(path)
    if any(set(record) != {"task_id", "answer"} for record in records):
        raise ValueError("Predictions must contain task_id and answer only")
    if any(not isinstance(record["answer"], str) for record in records):
        raise ValueError("Every answer must be text; use an empty string for a failed task")
    actual = [record["task_id"] for record in records]
    if len(actual) != len(ids) or len(set(actual)) != len(ids) or set(actual) != set(ids):
        raise ValueError(f"The arm must seal exactly {len(ids)} unique selected task IDs, including explicit empty answers")
    return records


def seal_predictions(root: Path, arm: str, predictions: Path) -> Path:
    """Seal this root's complete prediction file before any evaluator access."""
    root = Path(root).resolve()
    prepared = _prepared(root)
    manifest = _read(prepared / "manifest.json")
    if arm != manifest["arm"]:
        raise ValueError("Prediction arm differs from the prepared arm")
    load_tasks(root)
    path = Path(predictions).resolve()
    if prepared == path or prepared in path.parents:
        raise ValueError("Prediction files must stay outside prepared benchmark inputs")
    selected = selected_task_ids(manifest)
    records = _validated_predictions(path, selected)
    seal = {
        "protocol": manifest["protocol"], "sealed_utc": datetime.now(timezone.utc).isoformat(),
        "arm": arm, "total_predictions": len(records),
        "manifest_sha256": sha256(prepared / "manifest.json"),
        "ordered_task_ids_sha256": manifest["ordered_task_ids_sha256"],
        "arms": {arm: {"path": str(path), "sha256": sha256(path), "count": len(records),
                       "empty_answers": sum(not row["answer"].strip() for row in records)}},
        "note": "Logical content seal validated again before grading; filesystem modes are not a security boundary",
    }
    if manifest.get("subset") is not None:
        seal["subset"] = {"name": manifest["subset"]["name"], "sha256": manifest["subset"]["sha256"],
                          "count": len(selected)}
    evaluation = root / "private-evaluation"
    evaluation.mkdir(mode=0o700, exist_ok=True)
    evaluation.chmod(0o700)
    destination = evaluation / "predictions-seal.json"
    _write_new(destination, seal)
    destination.chmod(0o400)
    path.chmod(0o400)
    return destination


def grade(root: Path = DEFAULT_ROOT, seal_path: Path | None = None, *, scorer: Callable[..., float] | None = None,
          order: Sequence[str] | None = None) -> dict:
    """Evaluator-only official scoring of this root's sealed predictions (strict and 1%), on all tasks
    and on the undisputed subset (issue-47 ids excluded). `scorer` defaults to the pinned reward.py.
    `order` (this repeat's question order) is recorded with each row's position for later pairing."""
    root = Path(root).resolve()
    prepared = _prepared(root)
    evaluation = root / "private-evaluation"
    seal_path = Path(seal_path) if seal_path else evaluation / "predictions-seal.json"
    if not seal_path.is_file():
        raise ValueError("The arm must be sealed before any grading")
    seal = _read(seal_path)
    manifest = _read(prepared / "manifest.json")
    arm = manifest["arm"]
    selected = selected_task_ids(manifest)
    if seal.get("arm") != arm or set(seal.get("arms", {})) != {arm} or seal.get("total_predictions") != len(selected):
        raise ValueError("Prediction seal is incomplete or belongs to another arm")
    if (seal.get("subset") or {}).get("sha256") != (manifest.get("subset") or {}).get("sha256"):
        raise ValueError("Prediction seal and prepared manifest declare different subsets")
    if seal.get("manifest_sha256") != sha256(prepared / "manifest.json") or seal.get("ordered_task_ids_sha256") != manifest["ordered_task_ids_sha256"]:
        raise ValueError("Prepared manifest changed after sealing")
    if (evaluation / "paired-metrics.json").exists() or (evaluation / f"{arm}-grades.json").exists():
        raise ValueError("Grade outputs already exist; refusing to overwrite")
    entry = seal["arms"][arm]
    predictions_path = Path(entry["path"])
    if sha256(predictions_path) != entry["sha256"]:
        raise ValueError("Predictions changed after sealing")
    predictions = {row["task_id"]: row["answer"] for row in _validated_predictions(predictions_path, selected)}
    answers_path = prepared / "private/answers.jsonl"
    if sha256(answers_path) != manifest["answers_sha256"]:
        raise ValueError("Answer key changed after preparation")
    answers = _legacy.load_jsonl(answers_path)
    if [r["task_id"] for r in answers] != manifest["task_ids"]:
        raise ValueError("Answer IDs differ from the locked task manifest")
    wanted = set(selected)
    answers = [r for r in answers if r["task_id"] in wanted]      # the declared subset only (full run: all)
    if scorer is None:
        verify_prepared(root, verify_corpus=True, spec=_spec_from_manifest(manifest))
        scorer = official_scorer(root)
    withheld = set(manifest["learner_policy"]["withheld_reference_ids"])
    position = {task: i + 1 for i, task in enumerate(order)} if order is not None else {}
    rows = []
    for row in answers:
        predicted = predictions.get(row["task_id"], "")
        scores = grade_pair(scorer, row["answer"], predicted)
        rows.append({"task_id": row["task_id"], "position": position.get(row["task_id"]),
                     "issue_47": row["task_id"] in withheld, "empty": not predicted.strip(),
                     "strict_score": scores["strict"], "one_percent_score": scores["one_percent"]})

    def summary(subset):
        n = len(subset)
        return {"n": n, "correct": sum(r["strict_score"] for r in subset),
                "strict_accuracy": sum(r["strict_score"] for r in subset) / n if n else None,
                "one_percent_correct": sum(r["one_percent_score"] for r in subset),
                "one_percent_accuracy": sum(r["one_percent_score"] for r in subset) / n if n else None,
                "empty_answers": sum(r["empty"] for r in subset)}
    grades = {"benchmark": "OfficeQA Pro", "protocol": manifest["protocol"], "arm": arm,
              "grader": manifest["grader"], "predictions_sha256": entry["sha256"], "results": rows,
              "all": summary(rows), "undisputed": summary([r for r in rows if not r["issue_47"]])}
    _write_new(evaluation / f"{arm}-grades.json", grades)
    metrics = {
        "benchmark": "OfficeQA Pro", "protocol": manifest["protocol"], "fixture_only": manifest["fixture_only"],
        "official_headline_comparable": False, "graded_utc": datetime.now(timezone.utc).isoformat(),
        "seal_sha256": sha256(seal_path), "arm": arm, "arm_spec": manifest["arm_spec"], "repeat": manifest["repeat"],
        "denominator": len(rows), "primary_tolerance": 0.0, "secondary_tolerance": 0.01,
        "arms": {arm: grades["all"]}, "undisputed": grades["undisputed"],
        "results": [{"task_id": r["task_id"], "position": r["position"], "issue_47": r["issue_47"],
                     arm: r["strict_score"], f"{arm}@1%": r["one_percent_score"]} for r in rows],
        "pairing": "Pair roots by task_id across arms of the same repeat (identical question order within a repeat).",
        "feedback_policy": "Final scores are evaluator-only, never learner inputs.",
    }
    if seal.get("subset") is not None:
        metrics["subset"] = seal["subset"]
    _write_new(evaluation / "paired-metrics.json", metrics)
    return metrics


def _spec_from_manifest(manifest: Mapping):
    """The campaign spec as the manifest recorded it (enough for verify_prepared)."""
    import arms
    arm = arms.parse_arm(dict(manifest["arm_spec"]), pack=manifest["pack"])
    return arms.CampaignSpec(arm=arm, repeat=manifest["repeat"], pack=manifest["pack"],
                             order_seed=manifest["order_seed"], root="grading", barrier=None,
                             subset=arms.parse_subset(manifest.get("subset")))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "verify"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.root)
        result = {"prepared": True, "task_count": result["sample_count"], "corpus_documents": result["corpus_document_count"],
                  "fixture_only": result["fixture_only"], "protocol": result["protocol"]}
    else:
        result = verify_prepared(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
